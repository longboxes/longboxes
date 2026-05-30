"""Two-phase scanner implementing §9 v0.5 of the design doc.

**Phase 1 — walk and upsert.** For each file on disk:

1. Look up ``file_locations`` by path. If a row exists and ``(mtime, size)``
   are unchanged, refresh ``last_seen_at`` and skip — the cheap fast path,
   no archive open, no hashing.
2. Otherwise the path is new or its content changed. Compute sha256.
3. Parse ComicInfo.xml, determine ``comicinfo_status``, count pages.
4. Look up ``files`` by sha256:
   - **Known content**: re-point this path's location at the existing files
     row (covers "the file at this path now holds different content but we
     already have this content elsewhere"), or INSERT a new location row.
   - **New content**: INSERT a new files row, then INSERT/UPDATE the location.
5. If the file is new or its match is unresolved AND
   ``excluded_from_matching`` is false, enqueue a ``match_file`` job.

**Phase 2 — reconcile disappearances.** For each location whose path
*wasn't* visited:

- If exactly one new location was inserted for the same sha256 in Phase 1
  AND no other current locations exist for that file, treat as a clean
  move: update the old row's path in place, delete the duplicate new row.
- Otherwise mark ``missing_since``.

Files with all locations missing (or all re-pointed to other files rows)
are surfaced via the orphan-cleanup admin UI; rows are never auto-deleted.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.archives import open_archive, parse_comicinfo
from app.archives.base import (
    ArchiveError,
    ArchiveReader,
    UnsupportedArchiveError,
    detect_archive_format,
    resolve_cover_page_name,
)
from app.archives.cover_image import CoverInspection, inspect_cover
from app.db import SessionLocal
from app.models import File, FileErrorKind, FileLocation
from app.scanner.hashing import sha256_file
from app.scanner.walker import iter_archive_paths
from app.services.file_errors import clear_errors_for_path, record_error
from app.services.settings import (
    get_archive_backend,
    get_library_paths,
)

logger = logging.getLogger("longboxes.scanner")


class _ComicInfoParseError(Exception):
    """Local sentinel so ``record_error`` captures a useful class name
    (``ComicInfoParseError``) and the original ET.ParseError message.

    We don't propagate the live ``ET.ParseError`` through the call
    chain because ``parse_comicinfo`` deliberately swallows it — the
    matcher / classifier downstream treat malformed and missing XML
    identically. Surfacing it here is only for the inspector page.
    """

EnqueueMatchFn = Callable[[uuid.UUID], None]


class _AttachResult(Enum):
    """What ``_attach_location`` did. Lets the caller bump the right counter
    without ``_attach_location`` needing to know whether the files row was
    brand-new or pre-existing."""

    INSERTED = auto()  # net-new file_locations row
    REPOINTED = auto()  # existing row, file_id changed (content at path changed)
    REFRESHED = auto()  # existing row, same file_id, just mtime/last_seen bumped


@dataclass
class ScanResult:
    """Counters returned from a scan. Used by the admin UI and tests."""

    libraries_walked: int = 0
    paths_visited: int = 0
    fast_path_skips: int = 0
    new_files: int = 0
    new_locations_for_existing_files: int = 0
    locations_repointed: int = 0  # path now holds different content
    moves_collapsed: int = 0
    marked_missing: int = 0
    errors: int = 0
    match_jobs_enqueued: int = 0
    skipped_excluded: int = 0

    # Internal — used to feed Phase 2; not part of the public report.
    _visited_paths: set[str] = field(default_factory=set, repr=False)
    _new_locations_by_file_id: dict[uuid.UUID, list[uuid.UUID]] = field(
        default_factory=dict, repr=False
    )

    def report(self) -> dict[str, int]:
        """Public counters, suitable for logging or admin UI."""
        return {
            "libraries_walked": self.libraries_walked,
            "paths_visited": self.paths_visited,
            "fast_path_skips": self.fast_path_skips,
            "new_files": self.new_files,
            "new_locations_for_existing_files": self.new_locations_for_existing_files,
            "locations_repointed": self.locations_repointed,
            "moves_collapsed": self.moves_collapsed,
            "marked_missing": self.marked_missing,
            "errors": self.errors,
            "match_jobs_enqueued": self.match_jobs_enqueued,
            "skipped_excluded": self.skipped_excluded,
        }


# ---- Public entry point -------------------------------------------------


async def scan_all_libraries(
    session_factory=SessionLocal,
    enqueue_match: EnqueueMatchFn | None = None,
) -> ScanResult:
    """Run a full scan across every configured library path.

    ``session_factory`` and ``enqueue_match`` are injected to keep tests
    hermetic — production calls pass nothing and gets the real
    ``SessionLocal`` plus the real RQ enqueue function. In tests we swap
    in a fakeredis-backed enqueue or a no-op.
    """
    if enqueue_match is None:
        # Late import: app.jobs depends on app.scanner indirectly, so we
        # don't want a top-level import cycle.
        from app.jobs.match_file import enqueue_match_file

        enqueue_match = enqueue_match_file

    scan_start = datetime.now(tz=UTC)
    result = ScanResult()

    async with session_factory() as db:
        library_paths = await get_library_paths(db)
        # Fetch the archive backend setting once per scan; passed
        # down to each ``_process_one_file`` call so the per-file
        # path doesn't have to re-query. Defaults to the global
        # default if the setting is unset / bogus.
        archive_backend = await get_archive_backend(db)

    if not library_paths:
        logger.info("No library_paths configured; scanner has nothing to do.")
        return result

    for lib_str in library_paths:
        lib_root = Path(lib_str)
        if not lib_root.exists():
            logger.warning("Library path does not exist, skipping: %s", lib_root)
            continue
        if not lib_root.is_dir():
            logger.warning("Library path is not a directory, skipping: %s", lib_root)
            continue
        result.libraries_walked += 1
        async with session_factory() as db:
            await _phase_1_walk_library(
                db, lib_root, scan_start, result, enqueue_match,
                archive_backend,
            )

    async with session_factory() as db:
        await _phase_2_reconcile(db, library_paths, scan_start, result)

    logger.info("Scan complete: %s", result.report())
    return result


# ---- Phase 1 ------------------------------------------------------------


async def _phase_1_walk_library(
    db: AsyncSession,
    lib_root: Path,
    scan_start: datetime,
    result: ScanResult,
    enqueue_match: EnqueueMatchFn,
    archive_backend: str,
) -> None:
    """Walk one library root, processing each archive file once."""
    for archive_path in iter_archive_paths(lib_root):
        result.paths_visited += 1
        path_str = str(archive_path)
        try:
            await _process_one_file(
                db, archive_path, scan_start, result, enqueue_match,
                archive_backend,
            )
        except OSError as e:
            # IO error — file disappeared between walk and read, permissions
            # issue, etc. Don't poison the whole scan.
            result.errors += 1
            logger.warning("OSError on %s: %s", archive_path, e)
            # Record under ARCHIVE_OPEN — same triage bucket as a
            # corrupt archive; the admin sees "couldn't read this
            # path" and the retry button re-checks the same thing.
            await record_error(
                db, path=path_str, kind=FileErrorKind.ARCHIVE_OPEN, exc=e,
            )
        except ArchiveError as e:
            # Corrupt / password-protected / unreadable archive. Logged, skipped.
            result.errors += 1
            logger.warning("Archive error on %s: %s", archive_path, e)
            await record_error(
                db, path=path_str, kind=FileErrorKind.ARCHIVE_OPEN, exc=e,
            )
        except UnsupportedArchiveError as e:
            # Shouldn't normally happen because the walker filters by extension,
            # but PDF/CB7 would land here.
            result.errors += 1
            logger.warning("Unsupported archive %s: %s", archive_path, e)
            await record_error(
                db, path=path_str, kind=FileErrorKind.ARCHIVE_OPEN, exc=e,
            )
        else:
            # Clean pass on this path — every kind that *could* have
            # fired for it (archive_open, cover_extraction,
            # comicinfo_parse) has just been re-tried inside
            # ``_process_one_file`` and didn't raise. So a stale
            # error row from a previous scan is now wrong; drop it.
            # Cheap when the table is empty (the common case).
            await clear_errors_for_path(db, path_str)


def _inspect_cover(
    reader: ArchiveReader, pages: list[str]
) -> CoverInspection | None:
    """Best-effort cover-image inspection for a scanned archive.

    Resolves the cover page, extracts it, and reads its geometry so
    the scanner can persist double-wide / wraparound detection on the
    ``files`` row. Never raises — any failure (no pages, an unreadable
    entry, a non-image first page) yields None and the file's cover
    columns are left null.
    """
    try:
        cover_name = resolve_cover_page_name(reader, pages=pages)
        if cover_name is None:
            return None
        data = reader.extract_page(cover_name)
    except ArchiveError:
        return None
    return inspect_cover(data)


# Page index sampled by ``_inspect_interior`` when the archive is long
# enough to have one. Picked so the sample lands on real interior art:
# - past the obligatory title-page / credits page on page 2,
# - past any front-matter ads that occasionally precede the first
#   story page,
# - and well short of the back-cover ads / pinup gallery that some
#   trades stack at the end.
# Smaller archives clamp into range (see ``_inspect_interior``); a
# 3-page sketch variant samples its last page rather than overshooting,
# and a single-page fragment is given up on entirely (the cover IS
# the only page).
_INTERIOR_SAMPLE_INDEX = 5


def _inspect_interior(
    reader: ArchiveReader, pages: list[str]
) -> CoverInspection | None:
    """Best-effort interior-page geometry — the duplicates scorer's
    preferred resolution signal.

    Samples a mid-archive page (``_INTERIOR_SAMPLE_INDEX``, clamped
    into range) and reads its dimensions. The cover alone is a poor
    proxy when a re-encode shrinks the cover but leaves interior art
    intact, or when page 1 is a title card rather than the full-sized
    art. An interior sample sidesteps both.

    Returns ``None`` for archives with fewer than two pages (the
    cover already represents everything available), for un-decodable
    sample pages, or for archive-layer errors — same swallow-and-log
    contract as ``_inspect_cover`` so the scanner's per-file try /
    except doesn't need to know about a new failure mode. The shared
    ``CoverInspection`` dataclass is reused purely for the width /
    height pair; the wraparound flag is irrelevant for interior pages
    and isn't persisted.
    """
    n = len(pages)
    if n < 2:
        # A one-page archive has no separate interior — bail rather
        # than re-inspect the cover under a misleading name.
        return None
    idx = min(_INTERIOR_SAMPLE_INDEX, n - 1)
    try:
        data = reader.extract_page(pages[idx])
    except ArchiveError:
        return None
    return inspect_cover(data)


async def _process_one_file(
    db: AsyncSession,
    archive_path: Path,
    scan_start: datetime,
    result: ScanResult,
    enqueue_match: EnqueueMatchFn,
    archive_backend: str,
) -> None:
    """Apply the §9 Phase 1 algorithm to a single file."""
    path_str = str(archive_path)
    stat = archive_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    size = stat.st_size

    # Track that we visited this path so Phase 2 doesn't mark it missing.
    result._visited_paths.add(path_str)

    # Step 1: fast-path lookup by path.
    location = await _get_location_by_path(db, path_str)
    if location is not None and location.missing_since is None:
        existing_file = await db.get(File, location.file_id)
        if (
            existing_file is not None
            and existing_file.size_bytes == size
            and location.mtime == mtime
        ):
            location.last_seen_at = scan_start
            await db.commit()
            result.fast_path_skips += 1
            return

    # Step 2: compute sha256 — the file is new or has changed.
    # I/O-bound work (large file reads); off the event loop so other
    # coroutines on the same loop stay responsive. The scanner runs
    # inside an RQ-worker-owned loop today, but this is the pattern
    # the Phase 6 reader endpoint will need too.
    sha = await asyncio.to_thread(sha256_file, archive_path)

    # Steps 3 + 4: parse ComicInfo, count pages. Both come from opening the
    # archive once. If either fails, the archive layer raises ArchiveError
    # which propagates out and gets logged at the per-file try/except above.
    # Archive reads can shell out to ``unar`` (CBR) or do heavy parsing
    # (PDF via pymupdf), so we always hand them to a thread.
    # Cover + interior inspection happens in the same thread block as
    # the page + ComicInfo reads. It costs a couple of extra archive
    # opens on the comicbox backend (cover_filename + two
    # extract_page calls), but only for new / changed files —
    # fast-path skips never reach here — and the sha256 full-file
    # read above already dominates per-file cost. The interior sample
    # feeds the duplicates scorer's resolution tier; see
    # ``_inspect_interior`` for the page-index choice.
    def _read_archive() -> tuple[
        list[str],
        bytes | None,
        CoverInspection | None,
        CoverInspection | None,
    ]:
        reader = open_archive(archive_path, backend=archive_backend)
        pages = reader.list_pages()
        comicinfo_bytes = reader.read_comicinfo()
        return (
            pages,
            comicinfo_bytes,
            _inspect_cover(reader, pages),
            _inspect_interior(reader, pages),
        )
    (
        pages,
        comicinfo_bytes,
        cover_inspection,
        interior_inspection,
    ) = await asyncio.to_thread(_read_archive)
    comicinfo = parse_comicinfo(comicinfo_bytes)
    # Bytes were present in the archive but ElementTree refused them
    # (malformed XML, mid-file truncation, encoding declared wrong).
    # The matcher / classifier treats this as ``comicinfo_status=none``
    # — same as a file with no XML at all — but the inspector wants
    # to know the file *had* metadata that's now lost. record_error
    # uses (path, kind) UPSERT so a re-scan after the user fixes
    # the XML clears the row.
    if comicinfo.parse_error is not None:
        await record_error(
            db,
            path=path_str,
            kind=FileErrorKind.COMICINFO_PARSE,
            exc=_ComicInfoParseError(comicinfo.parse_error),
        )

    archive_format = detect_archive_format(archive_path)
    archive_format_value = archive_format.value if archive_format is not None else None

    # Step 5: look up files by sha256.
    existing = await _get_file_by_sha(db, sha)
    if existing is not None:
        # Known content — increment "new location for existing file" only
        # when ``_attach_location`` actually inserts a new row (the duplicate
        # case). A repoint or refresh is its own counter.
        attached = await _attach_location(
            db,
            existing,
            path_str,
            mtime,
            scan_start,
            location,
            result,
        )
        if attached == _AttachResult.INSERTED:
            result.new_locations_for_existing_files += 1
        file_row = existing
        # Opportunistic cover-metadata backfill. A files row created
        # before cover inspection existed has null cover columns; we
        # already paid for ``cover_inspection`` above (same archive
        # open), so fill it in now rather than waiting for the cover
        # endpoint's lazy backfill. Null-guarded — never overwrites.
        if existing.cover_width is None and cover_inspection is not None:
            existing.cover_width = cover_inspection.width
            existing.cover_height = cover_inspection.height
            existing.cover_is_wraparound = cover_inspection.is_wraparound
        # Same null-guarded backfill for interior dimensions — files
        # scanned before this column existed get filled in lazily on
        # any subsequent rescan that re-opens the archive.
        if existing.interior_width is None and interior_inspection is not None:
            existing.interior_width = interior_inspection.width
            existing.interior_height = interior_inspection.height
        # Existing content: don't enqueue a fresh match job unless it was
        # never matched (which the matcher pipeline tracks separately).
        # Phase 4 will own that decision; for now we enqueue only on new
        # files, matching the doc's "if the files row is new or its match
        # is unresolved" wording — the "unresolved" case lands when the
        # matcher exists.
        enqueue_for_match = False
    else:
        # New content — the new location belongs to a new files row, so it's
        # counted under ``new_files``, not ``new_locations_for_existing_files``.
        file_row = File(
            sha256=sha,
            size_bytes=size,
            archive_format=archive_format_value,
            page_count=len(pages),
            comicinfo_status=comicinfo.status,
            excluded_from_matching=False,
            first_scanned_at=scan_start,
            cover_width=cover_inspection.width if cover_inspection else None,
            cover_height=cover_inspection.height if cover_inspection else None,
            cover_is_wraparound=(
                cover_inspection.is_wraparound if cover_inspection else None
            ),
            interior_width=(
                interior_inspection.width if interior_inspection else None
            ),
            interior_height=(
                interior_inspection.height if interior_inspection else None
            ),
        )
        db.add(file_row)
        await db.flush()  # need file_row.id for the location
        result.new_files += 1
        await _attach_location(
            db,
            file_row,
            path_str,
            mtime,
            scan_start,
            location,
            result,
        )
        enqueue_for_match = True

    await db.commit()

    # Step 6: enqueue match_file. We enqueue unconditionally (modulo
    # the per-file exclusion) — when no ComicVine key is configured,
    # the match worker holds each job (reschedules itself for a short
    # delay) until the key arrives. That removes the race in the old
    # model: a scan + first-time-CV-key-save running concurrently used
    # to leave any file scanned after the wizard fired its match-all
    # pass permanently stranded.
    if enqueue_for_match:
        if file_row.excluded_from_matching:
            result.skipped_excluded += 1
        else:
            enqueue_match(file_row.id)
            result.match_jobs_enqueued += 1


async def _attach_location(
    db: AsyncSession,
    file_row: File,
    path_str: str,
    mtime: datetime,
    scan_start: datetime,
    existing_location: FileLocation | None,
    result: ScanResult,
) -> _AttachResult:
    """Insert or update a file_locations row pointing at ``file_row``.

    Returns which branch ran so the caller can bump the right counter.
    ``_attach_location`` itself only owns the ``locations_repointed`` counter
    and the per-file new-location bookkeeping (used by Phase 2 move detection);
    the "new file vs new location for existing file" distinction belongs to
    the caller, which knows whether ``file_row`` was brand-new or pre-existing.
    """
    if existing_location is None:
        new_loc = FileLocation(
            file_id=file_row.id,
            path=path_str,
            mtime=mtime,
            last_seen_at=scan_start,
            missing_since=None,
        )
        db.add(new_loc)
        await db.flush()  # need new_loc.id for the move-detection bookkeeping
        result._new_locations_by_file_id.setdefault(file_row.id, []).append(new_loc.id)
        return _AttachResult.INSERTED

    if existing_location.file_id != file_row.id:
        existing_location.file_id = file_row.id
        existing_location.mtime = mtime
        existing_location.last_seen_at = scan_start
        existing_location.missing_since = None
        result.locations_repointed += 1
        return _AttachResult.REPOINTED

    # Same file, mtime/size superficially changed (rare; touch without edit).
    existing_location.mtime = mtime
    existing_location.last_seen_at = scan_start
    existing_location.missing_since = None
    return _AttachResult.REFRESHED


# ---- Phase 2 ------------------------------------------------------------


async def _phase_2_reconcile(
    db: AsyncSession,
    library_paths: list[str],
    scan_start: datetime,
    result: ScanResult,
) -> None:
    """Reconcile locations under the walked libraries that we didn't visit.

    Limiting to "under the walked libraries" means temporarily unconfiguring
    a library doesn't cause its locations to be marked missing on the next
    scan. The library is simply out of scope.
    """
    if not library_paths:
        return

    # Build a per-library prefix filter so we only consider locations under
    # libraries we actually walked.
    prefix_filters = []
    for lib in library_paths:
        prefix = lib.rstrip("/") + "/"
        # Escape SQL LIKE metacharacters in the prefix.
        prefix_escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        prefix_filters.append(FileLocation.path.like(prefix_escaped + "%", escape="\\"))

    stmt = select(FileLocation).where(
        FileLocation.last_seen_at < scan_start,
        FileLocation.missing_since.is_(None),
        or_(*prefix_filters),
    )
    not_visited = (await db.execute(stmt)).scalars().all()

    # Group stales by file_id up-front so we can short-circuit move-detection
    # when multiple stales exist for the same content (ambiguous: we can't
    # tell which stale "became" the new location). This snapshot is taken
    # before we mutate any rows, so it's independent of iteration order.
    stale_count_per_file: dict[uuid.UUID, int] = {}
    for loc in not_visited:
        stale_count_per_file[loc.file_id] = stale_count_per_file.get(loc.file_id, 0) + 1

    for stale_loc in not_visited:
        # Move-detection: did Phase 1 insert exactly one new location for the
        # same files row, AND are there no other current (non-missing) locations
        # besides this stale one and that new one?
        new_loc_ids = result._new_locations_by_file_id.get(stale_loc.file_id, [])
        moved = False
        # Skip move-detection if more than one stale exists for this file_id —
        # the "one-to-one move" semantics don't apply when multiple paths
        # disappeared simultaneously for the same content.
        if len(new_loc_ids) == 1 and stale_count_per_file[stale_loc.file_id] == 1:
            current_locs = (
                (
                    await db.execute(
                        select(FileLocation).where(
                            FileLocation.file_id == stale_loc.file_id,
                            FileLocation.missing_since.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Expect exactly two: the stale (about-to-be-moved) and the new one.
            if (
                len(current_locs) == 2
                and any(loc.id == new_loc_ids[0] for loc in current_locs)
                and any(loc.id == stale_loc.id for loc in current_locs)
            ):
                new_loc = next(loc for loc in current_locs if loc.id == new_loc_ids[0])
                # Snapshot values BEFORE deleting; we'll write them onto the
                # stale row in a moment.
                new_path = new_loc.path
                new_mtime = new_loc.mtime
                new_last_seen = new_loc.last_seen_at
                # DELETE the new row and flush so the unique constraint on
                # ``path`` is released before we re-assign that path onto the
                # stale row. Without the explicit flush, SQLAlchemy's UoW may
                # emit the UPDATE before the DELETE and trip the constraint.
                await db.delete(new_loc)
                await db.flush()
                stale_loc.path = new_path
                stale_loc.mtime = new_mtime
                stale_loc.last_seen_at = new_last_seen
                stale_loc.missing_since = None
                result.moves_collapsed += 1
                # The collapsed move was double-counted: a "new_locations_for_
                # existing_files" in Phase 1, but logically it's a move. Adjust
                # the user-visible counter so the report reads correctly.
                result.new_locations_for_existing_files -= 1
                moved = True

        if not moved:
            stale_loc.missing_since = scan_start
            result.marked_missing += 1

    await db.commit()


# ---- Small helpers ------------------------------------------------------


async def _get_location_by_path(
    db: AsyncSession, path: str
) -> FileLocation | None:
    result = await db.execute(select(FileLocation).where(FileLocation.path == path))
    return result.scalar_one_or_none()


async def _get_file_by_sha(db: AsyncSession, sha: str) -> File | None:
    result = await db.execute(select(File).where(File.sha256 == sha))
    return result.scalar_one_or_none()
