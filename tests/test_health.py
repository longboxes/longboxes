"""Tests for the library health report aggregations."""

from datetime import UTC, datetime

import pytest

from app.models import (
    ComicInfoStatus,
    CvIssue,
    File,
    FileLocation,
    FileMatch,
    MatchSource,
    MatchStatus,
)
from app.services.health import compute_health

pytestmark = pytest.mark.asyncio


def _file(sha: str, **kwargs) -> File:
    return File(
        sha256=sha,
        size_bytes=kwargs.pop("size_bytes", 1024),
        archive_format=kwargs.pop("archive_format", "cbz"),
        page_count=kwargs.pop("page_count", 20),
        comicinfo_status=kwargs.pop("comicinfo_status", ComicInfoStatus.NONE),
        excluded_from_matching=kwargs.pop("excluded_from_matching", False),
        first_scanned_at=datetime.now(tz=UTC),
        **kwargs,
    )


def _loc(file_id, path: str, *, missing: bool = False) -> FileLocation:
    return FileLocation(
        file_id=file_id,
        path=path,
        mtime=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
        missing_since=datetime.now(tz=UTC) if missing else None,
    )


def _match(file_id, status: MatchStatus, issue_cv_id: int | None = None) -> FileMatch:
    return FileMatch(
        file_id=file_id,
        issue_cv_id=issue_cv_id,
        confidence=None,
        status=status,
        source=MatchSource.FILENAME,
        candidates=None,
        matched_at=datetime.now(tz=UTC),
    )


def _stub_issue(cv_id: int) -> CvIssue:
    """Minimal cv_issues row to satisfy the file_matches.issue_cv_id FK in
    health tests where we don't actually care about the issue's contents."""
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=None,
        issue_number=str(cv_id),
        cover_date=None,
        name=f"stub-{cv_id}",
        raw_payload=None,
        fetched_at=None,
    )


async def test_empty_library_returns_zeros(db_session):
    report = await compute_health(db_session)
    assert report.total_files == 0
    assert report.total_locations == 0
    assert report.projected_auto_rate == 0.0
    assert report.observed_auto_rate == 0.0


async def test_counts_basic_files_and_locations(db_session):
    f1 = _file("a" * 64)
    f2 = _file("b" * 64)
    db_session.add_all([f1, f2])
    await db_session.flush()
    db_session.add_all(
        [
            _loc(f1.id, "/library/a.cbz"),
            _loc(f2.id, "/library/b.cbz"),
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.total_files == 2
    assert report.total_locations == 2
    assert report.duplicate_files_count == 0
    assert report.duplicate_bytes == 0


async def test_duplicate_content_counted_with_redundant_bytes(db_session):
    """One files row with 3 current locations → (3-1)*size_bytes redundant."""
    f = _file("a" * 64, size_bytes=5000)
    db_session.add(f)
    await db_session.flush()
    db_session.add_all(
        [
            _loc(f.id, "/library/a.cbz"),
            _loc(f.id, "/library/copy.cbz"),
            _loc(f.id, "/library/backup.cbz"),
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.total_files == 1
    assert report.total_locations == 3
    assert report.duplicate_files_count == 1
    assert report.duplicate_bytes == 2 * 5000


async def test_missing_locations_not_counted_in_current(db_session):
    f = _file("a" * 64)
    db_session.add(f)
    await db_session.flush()
    db_session.add_all(
        [
            _loc(f.id, "/library/a.cbz"),
            _loc(f.id, "/library/old.cbz", missing=True),
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.total_locations == 1  # missing one excluded
    assert report.duplicate_files_count == 0  # only one current location


async def test_comicinfo_coverage_breakdown(db_session):
    f1 = _file("a" * 64, comicinfo_status=ComicInfoStatus.FULL_WITH_CVID)
    f2 = _file("b" * 64, comicinfo_status=ComicInfoStatus.PARTIAL)
    f3 = _file("c" * 64, comicinfo_status=ComicInfoStatus.NONE)
    f4 = _file("d" * 64, comicinfo_status=ComicInfoStatus.NONE)
    db_session.add_all([f1, f2, f3, f4])
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.comicinfo.full_with_cvid == 1
    assert report.comicinfo.partial == 1
    assert report.comicinfo.none == 2


async def test_match_status_breakdown(db_session):
    db_session.add(_stub_issue(1))  # FK target for the auto match below
    files = [_file(chr(ord("a") + i) * 64) for i in range(4)]
    db_session.add_all(files)
    await db_session.flush()
    db_session.add_all(
        [
            _match(files[0].id, MatchStatus.AUTO, 1),
            _match(files[1].id, MatchStatus.PENDING),
            _match(files[2].id, MatchStatus.UNMATCHED),
            # files[3] has no FileMatch row → "not yet attempted"
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.matches.auto == 1
    assert report.matches.pending == 1
    assert report.matches.unmatched == 1
    assert report.matches.no_match_row == 1


async def test_projected_auto_rate_pre_matcher(db_session):
    """4 full_with_cvid + 4 partial + 4 none. Expected projection:
    (4*1.0 + 4*0.8 + 4*0.45) / 12 = (4 + 3.2 + 1.8) / 12 = 0.75
    """
    for status in (
        [ComicInfoStatus.FULL_WITH_CVID] * 4
        + [ComicInfoStatus.PARTIAL] * 4
        + [ComicInfoStatus.NONE] * 4
    ):
        db_session.add(
            _file(f"{status.value}{id(status)}".ljust(64, "x")[:64], comicinfo_status=status)
        )
    # Sha collisions could occur with this naive scheme; force unique shas.
    await db_session.rollback()
    seen_shas = set()
    for i, status in enumerate(
        [ComicInfoStatus.FULL_WITH_CVID] * 4
        + [ComicInfoStatus.PARTIAL] * 4
        + [ComicInfoStatus.NONE] * 4
    ):
        sha = f"{i:064x}"
        seen_shas.add(sha)
        db_session.add(_file(sha, comicinfo_status=status))
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.total_files == 12
    assert abs(report.projected_auto_rate - 0.75) < 0.01
    # Range covers (4*1 + 4*0.7 + 4*0.3)/12 = 0.667 → (4 + 4*0.9 + 4*0.6)/12 = 0.833
    assert report.projected_auto_rate_low < report.projected_auto_rate
    assert report.projected_auto_rate < report.projected_auto_rate_high


async def test_observed_rate_uses_auto_plus_confirmed(db_session):
    """Observed rate = (auto + confirmed) / total."""
    db_session.add_all([_stub_issue(1), _stub_issue(2)])  # FK targets
    files = [_file(f"{i:064x}") for i in range(10)]
    db_session.add_all(files)
    await db_session.flush()
    for f in files[:5]:
        db_session.add(_match(f.id, MatchStatus.AUTO, 1))
    for f in files[5:7]:
        db_session.add(_match(f.id, MatchStatus.CONFIRMED, 2))
    for f in files[7:]:
        db_session.add(_match(f.id, MatchStatus.UNMATCHED))
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.observed_auto_rate == pytest.approx(0.7)


async def test_observed_rate_excludes_not_yet_attempted(db_session):
    """Files the matcher hasn't reached yet (no file_matches row) must
    not drag the observed rate down — it reflects only files actually
    processed. This matters during a big initial run, when most files
    are still queued.

    4 of 10 files matched: 3 auto + 1 unmatched. observed = 3 / 4 = 0.75,
    NOT 3 / 10 — the 6 not-yet-attempted files are out of the denominator.
    """
    db_session.add(_stub_issue(1))  # FK target for the auto matches
    files = [_file(f"{i:064x}") for i in range(10)]
    db_session.add_all(files)
    await db_session.flush()
    db_session.add_all(
        [
            _match(files[0].id, MatchStatus.AUTO, 1),
            _match(files[1].id, MatchStatus.AUTO, 1),
            _match(files[2].id, MatchStatus.AUTO, 1),
            _match(files[3].id, MatchStatus.UNMATCHED),
            # files[4:] have no FileMatch row — still in the queue.
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.matches.no_match_row == 6
    assert report.observed_auto_rate == pytest.approx(0.75)


async def test_excluded_files_counted(db_session):
    db_session.add_all(
        [
            _file("a" * 64, excluded_from_matching=True),
            _file("b" * 64),
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.excluded_count == 1
    assert report.total_files == 2


# ---- Phase 11C: LOCAL / SUPPLEMENT in the health report ----------------


async def test_match_status_counts_local_and_supplement(db_session):
    """LOCAL and SUPPLEMENT (Phase 11) get their own breakdown rows, and
    must not be double-counted into ``no_match_row`` — they have a
    file_matches row like any other resolved status."""
    files = [_file(f"{i:064x}") for i in range(5)]
    db_session.add_all(files)
    await db_session.flush()
    db_session.add_all(
        [
            _match(files[0].id, MatchStatus.LOCAL),
            _match(files[1].id, MatchStatus.LOCAL),
            _match(files[2].id, MatchStatus.SUPPLEMENT),
            _match(files[3].id, MatchStatus.UNMATCHED),
            # files[4] has no FileMatch row → "not yet attempted".
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.matches.local == 2
    assert report.matches.supplement == 1
    assert report.matches.unmatched == 1
    assert report.matches.no_match_row == 1
    # Every file is accounted for exactly once across the breakdown.
    assert report.matches.total == 5


async def test_observed_rate_excludes_local_and_supplement(db_session):
    """LOCAL / SUPPLEMENT files are catalogued by hand, not by the auto-
    matcher, so they're excluded from the observed-rate denominator —
    cataloguing a book locally must not drag the reported rate down.

    3 auto + 1 confirmed = 4 resolved; 10 total - 3 local - 2 supplement
    = 5 matchable; observed = 4 / 5 = 0.8.
    """
    db_session.add(_stub_issue(1))  # FK target for the auto/confirmed matches
    files = [_file(f"{i:064x}") for i in range(10)]
    db_session.add_all(files)
    await db_session.flush()
    db_session.add_all(
        [
            _match(files[0].id, MatchStatus.AUTO, 1),
            _match(files[1].id, MatchStatus.AUTO, 1),
            _match(files[2].id, MatchStatus.AUTO, 1),
            _match(files[3].id, MatchStatus.CONFIRMED, 1),
            _match(files[4].id, MatchStatus.UNMATCHED),
            _match(files[5].id, MatchStatus.LOCAL),
            _match(files[6].id, MatchStatus.LOCAL),
            _match(files[7].id, MatchStatus.LOCAL),
            _match(files[8].id, MatchStatus.SUPPLEMENT),
            _match(files[9].id, MatchStatus.SUPPLEMENT),
        ]
    )
    await db_session.commit()
    report = await compute_health(db_session)
    assert report.matches.local == 3
    assert report.matches.supplement == 2
    assert report.observed_auto_rate == pytest.approx(0.8)
