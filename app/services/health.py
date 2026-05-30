"""Library health report aggregations — §9 of the design doc.

One async function ``compute_health(db)`` returns a typed ``HealthReport``
covering match-readiness, duplicates, and projected vs observed match rate.
Used by ``/admin/health``. All queries are aggregate (COUNT / SUM); for
libraries up to ~100k files this stays well under a second.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ComicInfoStatus, File, FileLocation, FileMatch, MatchStatus

# Projected auto-match rates per ComicInfo coverage bucket (§9).
# Conservative midpoints of the doc's documented ranges. Tunable later.
PROJECTION = {
    ComicInfoStatus.FULL_WITH_CVID: 1.00,
    ComicInfoStatus.PARTIAL: 0.80,
    ComicInfoStatus.NONE: 0.45,
}
# Same as above, but for displaying the range (low / high) in the UI.
PROJECTION_RANGE = {
    ComicInfoStatus.FULL_WITH_CVID: (1.00, 1.00),
    ComicInfoStatus.PARTIAL: (0.70, 0.90),
    ComicInfoStatus.NONE: (0.30, 0.60),
}


@dataclass
class CountByStatus:
    auto: int = 0
    confirmed: int = 0
    pending: int = 0
    rejected: int = 0
    unmatched: int = 0
    # Phase 11 — resolved by hand, not by the auto-matcher: LOCAL is a
    # user-authored entry for a comic not in ComicVine, SUPPLEMENT is
    # non-issue content attached to a CV volume. Both are catalogued
    # library content — NOT part of the unmatched deficit, and excluded
    # from the observed-rate denominator below.
    local: int = 0
    supplement: int = 0
    # Files with no file_matches row at all yet (scanner hasn't run
    # match_file, or it crashed). Counted as "not yet matched."
    no_match_row: int = 0

    @property
    def total(self) -> int:
        return (
            self.auto
            + self.confirmed
            + self.pending
            + self.rejected
            + self.unmatched
            + self.local
            + self.supplement
            + self.no_match_row
        )

    @property
    def resolved(self) -> int:
        """auto + confirmed (the rate denominator's numerator)."""
        return self.auto + self.confirmed


@dataclass
class CountByComicInfo:
    full_with_cvid: int = 0
    partial: int = 0
    none: int = 0

    @property
    def total(self) -> int:
        return self.full_with_cvid + self.partial + self.none


@dataclass
class HealthReport:
    # Library size + duplicates
    total_files: int = 0
    total_locations: int = 0
    duplicate_files_count: int = 0  # files with >1 current location
    duplicate_bytes: int = 0  # sum((N-1) * size_bytes) over those files
    excluded_count: int = 0

    # ComicInfo coverage
    comicinfo: CountByComicInfo = field(default_factory=CountByComicInfo)

    # Match-status breakdown (current state)
    matches: CountByStatus = field(default_factory=CountByStatus)

    # Projected (pre-matcher) auto-match rate, derived from ComicInfo coverage.
    # Range = optimistic / pessimistic bounds.
    projected_auto_rate: float = 0.0
    projected_auto_rate_low: float = 0.0
    projected_auto_rate_high: float = 0.0

    # Observed (post-matcher) rate: (auto + confirmed) over the files
    # the matcher has actually processed — total_files minus the
    # hand-resolved LOCAL / SUPPLEMENT entries (Phase 11) and the
    # not-yet-attempted files (no file_matches row). Excluding the
    # latter keeps a long matcher run from reading as a near-zero rate
    # while the queue is still draining.
    observed_auto_rate: float = 0.0


async def compute_health(db: AsyncSession) -> HealthReport:
    report = HealthReport()

    # ---- Basic counts -------------------------------------------------

    report.total_files = await _scalar(db, select(func.count()).select_from(File))
    report.total_locations = await _scalar(
        db,
        select(func.count()).select_from(FileLocation).where(FileLocation.missing_since.is_(None)),
    )
    report.excluded_count = await _scalar(
        db,
        select(func.count()).select_from(File).where(File.excluded_from_matching.is_(True)),
    )

    # ---- Duplicate footprint -----------------------------------------
    # For each files row, count current (non-missing) locations. Files with
    # > 1 contribute (N-1) * size_bytes to the redundant total.
    dup_stmt = (
        select(
            File.id,
            File.size_bytes,
            func.count(FileLocation.id).label("loc_count"),
        )
        .join(FileLocation, FileLocation.file_id == File.id)
        .where(FileLocation.missing_since.is_(None))
        .group_by(File.id, File.size_bytes)
        .having(func.count(FileLocation.id) > 1)
    )
    for _file_id, size_bytes, loc_count in (await db.execute(dup_stmt)).all():
        report.duplicate_files_count += 1
        if size_bytes is not None:
            report.duplicate_bytes += (loc_count - 1) * size_bytes

    # ---- ComicInfo coverage breakdown --------------------------------
    ci_stmt = select(File.comicinfo_status, func.count()).group_by(File.comicinfo_status)
    for status_value, count in (await db.execute(ci_stmt)).all():
        if status_value == ComicInfoStatus.FULL_WITH_CVID:
            report.comicinfo.full_with_cvid = count
        elif status_value == ComicInfoStatus.PARTIAL:
            report.comicinfo.partial = count
        elif status_value == ComicInfoStatus.NONE:
            report.comicinfo.none = count

    # ---- Match status breakdown --------------------------------------
    match_stmt = select(FileMatch.status, func.count()).group_by(FileMatch.status)
    for status_value, count in (await db.execute(match_stmt)).all():
        if status_value == MatchStatus.AUTO:
            report.matches.auto = count
        elif status_value == MatchStatus.CONFIRMED:
            report.matches.confirmed = count
        elif status_value == MatchStatus.PENDING:
            report.matches.pending = count
        elif status_value == MatchStatus.REJECTED:
            report.matches.rejected = count
        elif status_value == MatchStatus.UNMATCHED:
            report.matches.unmatched = count
        elif status_value == MatchStatus.LOCAL:
            report.matches.local = count
        elif status_value == MatchStatus.SUPPLEMENT:
            report.matches.supplement = count
    # Files with no file_matches row yet = total - (sum of above). LOCAL
    # and SUPPLEMENT have a row, so they belong in ``summed`` — leaving
    # them out would double-count them into ``no_match_row``.
    summed = (
        report.matches.auto
        + report.matches.confirmed
        + report.matches.pending
        + report.matches.rejected
        + report.matches.unmatched
        + report.matches.local
        + report.matches.supplement
    )
    report.matches.no_match_row = max(0, report.total_files - summed)

    # ---- Rates --------------------------------------------------------
    if report.total_files > 0:
        # Projected: weighted average of per-bucket projections.
        projected_count = (
            report.comicinfo.full_with_cvid * PROJECTION[ComicInfoStatus.FULL_WITH_CVID]
            + report.comicinfo.partial * PROJECTION[ComicInfoStatus.PARTIAL]
            + report.comicinfo.none * PROJECTION[ComicInfoStatus.NONE]
        )
        report.projected_auto_rate = projected_count / report.total_files

        low = (
            report.comicinfo.full_with_cvid * PROJECTION_RANGE[ComicInfoStatus.FULL_WITH_CVID][0]
            + report.comicinfo.partial * PROJECTION_RANGE[ComicInfoStatus.PARTIAL][0]
            + report.comicinfo.none * PROJECTION_RANGE[ComicInfoStatus.NONE][0]
        )
        high = (
            report.comicinfo.full_with_cvid * PROJECTION_RANGE[ComicInfoStatus.FULL_WITH_CVID][1]
            + report.comicinfo.partial * PROJECTION_RANGE[ComicInfoStatus.PARTIAL][1]
            + report.comicinfo.none * PROJECTION_RANGE[ComicInfoStatus.NONE][1]
        )
        report.projected_auto_rate_low = low / report.total_files
        report.projected_auto_rate_high = high / report.total_files

        # Observed: of the files the matcher has actually processed,
        # how many landed as auto or confirmed. The denominator is
        # total_files minus three exclusions:
        #   * LOCAL / SUPPLEMENT — resolved by hand, not by the matcher;
        #     excluding them keeps cataloguing a book locally from
        #     paradoxically dragging the matcher's reported rate down.
        #   * no_match_row — files the matcher hasn't reached yet (no
        #     file_matches row). Including these would read a long
        #     initial run as a near-zero rate while the queue drains,
        #     even when the matcher is doing fine on what it has seen.
        # What's left is the attempted, matcher-owned set:
        # auto + confirmed + pending + rejected + unmatched.
        matchable = (
            report.total_files
            - report.matches.local
            - report.matches.supplement
            - report.matches.no_match_row
        )
        if matchable > 0:
            report.observed_auto_rate = report.matches.resolved / matchable

    return report


async def _scalar(db: AsyncSession, stmt) -> int:
    """Run a COUNT(*) statement and return the integer result."""
    return (await db.execute(stmt)).scalar_one() or 0
