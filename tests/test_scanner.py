"""End-to-end scanner tests.

Each test wires up a temporary library directory, builds CBZs in it,
configures ``library_paths`` in the test DB, and runs ``scan_all_libraries``
against the engine fixture. The scanner-level branches we want to assert on:

- New file → INSERT files + file_locations rows; match_file enqueued.
- Fast-path skip on unchanged (path + mtime + size) file.
- Content changed at the same path → re-point the location row.
- Clean move (delete A, create B with same content) → single row updated.
- Duplicate (same content at A and B) → one files row, two locations.
- Missing reconciliation → missing_since populated.
- ``excluded_from_matching`` is honored → no match_file enqueued.
- Corrupt archive is logged and skipped without crashing the scan.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import ComicInfoStatus, File, FileLocation
from app.scanner import scan_all_libraries
from app.services.settings import set_cv_api_key, set_library_paths
from tests.fixtures import (
    build_cbz,
    build_comicinfo_full,
    build_comicinfo_partial,
    make_image_bytes,
)

pytestmark = pytest.mark.asyncio


# ---- Helpers ------------------------------------------------------------


class _MatchRecorder:
    """Stand-in for ``enqueue_match_file`` that just records call args."""

    def __init__(self) -> None:
        self.called_with: list[uuid.UUID] = []

    def __call__(self, file_id: uuid.UUID) -> None:
        self.called_with.append(file_id)


async def _run_scan(engine, library_root: Path, *, cv_key: str | None = "test-key"):
    """Configure library_paths to point at ``library_root`` and run a scan.

    Sets a placeholder CV API key by default. The scanner itself no
    longer gates match-enqueue on the CV key — it always enqueues, and
    ``match_file_job`` holds (reschedules every
    ``NO_KEY_RESCHEDULE_SECONDS``) when the key is missing. The cv_key
    parameter is preserved to test both "key set" and "key absent"
    library-state arrangements, but the recorder will see the same
    enqueue call in either case."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        await set_library_paths(db, [str(library_root)])
        if cv_key is not None:
            await set_cv_api_key(db, cv_key)
        await db.commit()
    rec = _MatchRecorder()
    result = await scan_all_libraries(session_factory=sm, enqueue_match=rec)
    return result, rec


async def _all_files(engine) -> list[File]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        rows = (await db.execute(select(File))).scalars().all()
        return list(rows)


async def _all_locations(engine) -> list[FileLocation]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        rows = (await db.execute(select(FileLocation))).scalars().all()
        return list(rows)


# ---- Fresh-scan happy path ---------------------------------------------


async def test_scan_inserts_files_and_locations(engine, db_session, tmp_path: Path):
    build_cbz(tmp_path / "comic.cbz", page_count=3, page_payload=b"A")
    result, rec = await _run_scan(engine, tmp_path)
    assert result.new_files == 1
    assert result.new_locations_for_existing_files == 0
    assert result.match_jobs_enqueued == 1

    files = await _all_files(engine)
    locs = await _all_locations(engine)
    assert len(files) == 1
    assert files[0].page_count == 3
    assert files[0].archive_format == "cbz"
    assert files[0].comicinfo_status == ComicInfoStatus.NONE
    assert len(locs) == 1
    assert locs[0].path.endswith("comic.cbz")
    assert locs[0].missing_since is None
    assert len(rec.called_with) == 1


async def test_scan_records_comicinfo_status_partial(engine, db_session, tmp_path: Path):
    xml = build_comicinfo_partial(series="Saga", number="1", year=2012)
    build_cbz(tmp_path / "saga.cbz", comicinfo=xml)
    await _run_scan(engine, tmp_path)
    files = await _all_files(engine)
    assert files[0].comicinfo_status == ComicInfoStatus.PARTIAL


async def test_scan_records_comicinfo_status_full(engine, db_session, tmp_path: Path):
    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=999)
    build_cbz(tmp_path / "saga.cbz", comicinfo=xml)
    await _run_scan(engine, tmp_path)
    files = await _all_files(engine)
    assert files[0].comicinfo_status == ComicInfoStatus.FULL_WITH_CVID


# ---- Cover-image inspection --------------------------------------------


async def test_scan_records_cover_geometry(engine, db_session, tmp_path: Path):
    """A normal portrait cover → dimensions stored, not flagged wraparound."""
    build_cbz(
        tmp_path / "normal.cbz",
        page_count=2,
        page_payload=make_image_bytes(400, 600),
    )
    await _run_scan(engine, tmp_path)
    files = await _all_files(engine)
    assert len(files) == 1
    assert files[0].cover_width == 400
    assert files[0].cover_height == 600
    assert files[0].cover_is_wraparound is False


async def test_scan_flags_double_wide_cover(engine, db_session, tmp_path: Path):
    """A double-wide wraparound cover page → cover_is_wraparound True."""
    build_cbz(
        tmp_path / "wrap.cbz",
        page_count=2,
        # Ratio 1.95 — a near-2:1 image, over WRAPAROUND_ASPECT_THRESHOLD.
        page_payload=make_image_bytes(1950, 1000),
    )
    await _run_scan(engine, tmp_path)
    files = await _all_files(engine)
    assert len(files) == 1
    assert files[0].cover_is_wraparound is True
    assert files[0].cover_width == 1950


async def test_scan_leaves_cover_null_for_non_image_pages(
    engine, db_session, tmp_path: Path
):
    """A CBZ whose 'pages' aren't decodable images → cover columns
    stay null. Cover inspection is best-effort; it never fails a scan."""
    build_cbz(tmp_path / "bogus.cbz", page_count=2, page_payload=b"not-an-image")
    result, _ = await _run_scan(engine, tmp_path)
    assert result.errors == 0
    files = await _all_files(engine)
    assert len(files) == 1
    assert files[0].cover_is_wraparound is None
    assert files[0].cover_width is None


# ---- Fast-path skip -----------------------------------------------------


async def test_second_scan_takes_fast_path_for_unchanged_file(
    engine, db_session, tmp_path: Path
):
    build_cbz(tmp_path / "comic.cbz", page_payload=b"A")
    first, _ = await _run_scan(engine, tmp_path)
    assert first.new_files == 1
    assert first.fast_path_skips == 0

    second, rec = await _run_scan(engine, tmp_path)
    assert second.new_files == 0
    assert second.fast_path_skips == 1
    # No re-enqueue on unchanged file
    assert second.match_jobs_enqueued == 0
    assert rec.called_with == []


# ---- Content changed at same path --------------------------------------


async def test_content_changed_at_same_path_repoints_location(
    engine, db_session, tmp_path: Path
):
    build_cbz(tmp_path / "comic.cbz", page_payload=b"A")
    await _run_scan(engine, tmp_path)
    files_before = await _all_files(engine)
    locs_before = await _all_locations(engine)
    original_file_id = files_before[0].id
    original_loc_id = locs_before[0].id

    # Replace content; bump mtime so the fast-path doesn't skip.
    time.sleep(0.05)
    build_cbz(tmp_path / "comic.cbz", page_payload=b"B")

    result, _ = await _run_scan(engine, tmp_path)
    assert result.new_files == 1  # the new content
    assert result.locations_repointed == 1
    files_after = await _all_files(engine)
    locs_after = await _all_locations(engine)
    assert len(files_after) == 2  # old + new content rows; old becomes orphan
    assert len(locs_after) == 1  # still one location
    assert locs_after[0].id == original_loc_id  # same row, re-pointed
    assert locs_after[0].file_id != original_file_id


# ---- Clean move (delete A, create B with same content) -----------------


async def test_clean_move_collapses_to_single_location(
    engine, db_session, tmp_path: Path
):
    src = tmp_path / "a.cbz"
    dst = tmp_path / "b.cbz"
    build_cbz(src, page_payload=b"MOVE-ME")
    await _run_scan(engine, tmp_path)
    files_after_first = await _all_files(engine)
    locs_after_first = await _all_locations(engine)
    assert len(files_after_first) == 1
    assert len(locs_after_first) == 1
    original_file_id = files_after_first[0].id

    # Move: delete src, create dst with identical bytes.
    src.rename(dst)
    result, _ = await _run_scan(engine, tmp_path)
    assert result.moves_collapsed == 1
    assert result.marked_missing == 0

    files_after_move = await _all_files(engine)
    locs_after_move = await _all_locations(engine)
    assert len(files_after_move) == 1
    assert files_after_move[0].id == original_file_id  # same content row
    assert len(locs_after_move) == 1  # collapsed
    assert locs_after_move[0].path.endswith("b.cbz")
    assert locs_after_move[0].missing_since is None


# ---- Duplicate (same content at two paths simultaneously) --------------


async def test_duplicate_content_produces_one_file_two_locations(
    engine, db_session, tmp_path: Path
):
    build_cbz(tmp_path / "a.cbz", page_payload=b"DUP")
    build_cbz(tmp_path / "b.cbz", page_payload=b"DUP")
    result, rec = await _run_scan(engine, tmp_path)
    assert result.new_files == 1
    assert result.new_locations_for_existing_files == 1  # one was attached to existing
    files = await _all_files(engine)
    locs = await _all_locations(engine)
    assert len(files) == 1
    assert len(locs) == 2
    assert {loc.path.split("/")[-1] for loc in locs} == {"a.cbz", "b.cbz"}
    # match_file enqueued exactly once (new content), not twice
    assert len(rec.called_with) == 1


# ---- Missing reconciliation --------------------------------------------


async def test_disappeared_file_is_marked_missing(engine, db_session, tmp_path: Path):
    target = tmp_path / "ephemeral.cbz"
    build_cbz(target, page_payload=b"poof")
    await _run_scan(engine, tmp_path)
    locs_before = await _all_locations(engine)
    assert locs_before[0].missing_since is None

    target.unlink()
    result, _ = await _run_scan(engine, tmp_path)
    assert result.marked_missing == 1

    locs_after = await _all_locations(engine)
    assert len(locs_after) == 1  # not deleted, just flagged
    assert locs_after[0].missing_since is not None


# ---- excluded_from_matching is honored ---------------------------------


async def test_existing_files_are_not_re_enqueued_on_rescan(
    engine, db_session, tmp_path: Path
):
    """The scanner enqueues ``match_file`` only for *new* files.

    Existing files — whether excluded_from_matching or not — are never
    re-enqueued by the scanner alone. Phase 4 will own the "match is
    unresolved → re-enqueue" decision once the matcher exists.

    This test also confirms the ``excluded_from_matching`` flag isn't
    accidentally ignored when set: we mark a file excluded and run another
    scan; no enqueue should occur from either side of the gate.
    """
    build_cbz(tmp_path / "x.cbz", page_payload=b"X")
    _, rec_first = await _run_scan(engine, tmp_path)
    assert len(rec_first.called_with) == 1  # new file → enqueued

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        files = (await db.execute(select(File))).scalars().all()
        for f in files:
            f.excluded_from_matching = True
        await db.commit()

    # Bump mtime so the fast-path doesn't skip; sha256 is unchanged so we
    # take the "Known content" branch. No enqueue either way.
    time.sleep(0.05)
    (tmp_path / "x.cbz").touch()
    _, rec_second = await _run_scan(engine, tmp_path)
    assert rec_second.called_with == []


# ---- Corrupt archive is logged, scan continues -------------------------


async def test_corrupt_archive_is_skipped(engine, db_session, tmp_path: Path):
    # A "CBZ" that is not actually a valid zip file.
    bad = tmp_path / "broken.cbz"
    bad.write_bytes(b"this is not a zip file")
    # A valid CBZ alongside, to prove the scan didn't bail out.
    build_cbz(tmp_path / "ok.cbz", page_payload=b"ok")

    result, _ = await _run_scan(engine, tmp_path)
    assert result.errors == 1
    assert result.new_files == 1  # the good one was inserted

    files = await _all_files(engine)
    assert len(files) == 1


# ---- Empty library / unconfigured paths --------------------------------


async def test_scan_with_no_library_paths_is_a_noop(engine, db_session, tmp_path: Path):
    # Don't configure library_paths.
    sm = async_sessionmaker(engine, expire_on_commit=False)
    rec = _MatchRecorder()
    result = await scan_all_libraries(session_factory=sm, enqueue_match=rec)
    assert result.libraries_walked == 0
    assert result.paths_visited == 0


async def test_scan_with_missing_library_root_warns_and_continues(
    engine, db_session, tmp_path: Path
):
    sm = async_sessionmaker(engine, expire_on_commit=False)
    bogus = tmp_path / "does-not-exist"
    async with sm() as db:
        await set_library_paths(db, [str(bogus)])
        await db.commit()
    rec = _MatchRecorder()
    result = await scan_all_libraries(session_factory=sm, enqueue_match=rec)
    assert result.libraries_walked == 0
    assert result.errors == 0


# ---- CV-key gate --------------------------------------------------------


async def test_scan_enqueues_match_jobs_even_without_cv_key(
    engine, db_session, tmp_path: Path
):
    """Scanner always enqueues match jobs regardless of CV key state.

    The match worker itself is the place that knows whether the
    matcher can run — when no key is set, the worker holds each job
    by rescheduling it on a short cadence (see
    ``app/jobs/match_file.NO_KEY_RESCHEDULE_SECONDS``). Once the
    admin pastes a key, the held jobs fire and start matching with
    no explicit "match all" pass needed.

    This removes the old race: a scan running concurrently with the
    setup wizard's match-all pass used to leave any file scanned
    after the pass permanently un-enqueued."""
    build_cbz(tmp_path / "a.cbz", page_count=3, page_payload=b"A")
    build_cbz(tmp_path / "b.cbz", page_count=3, page_payload=b"B")
    result, rec = await _run_scan(engine, tmp_path, cv_key=None)

    # Files were registered.
    assert result.new_files == 2
    # Match jobs fired — the worker will hold them until the key
    # arrives.
    assert result.match_jobs_enqueued == 2
    assert len(rec.called_with) == 2
