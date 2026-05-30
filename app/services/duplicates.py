"""Duplicate-file inspector — surfaces two distinct duplicate kinds
and picks a quality-ranked keeper for each group.

Two kinds the operator wants to triage:

**Hash duplicates** — one ``files`` row with multiple current
``file_locations`` rows. Same exact content (same sha256) at multiple
paths on disk. Action: pick a canonical path; the rest are safe to
delete on disk.

**Issue duplicates** — multiple ``files`` rows (different sha256) all
matched to the same ``cv_issues.cv_id``. Two different rips / scans /
re-encodes of the same comic. Action: pick the highest-quality rip;
the others are candidates to delete or exclude from matching.

Both share a ``_score`` heuristic so the listing can highlight a
recommended keeper. The score is a tuple of comparable values
compared lexicographically rather than a weighted sum — that way a
later tweak to one tier never silently demotes another.

The top tier is **page-count plausibility**: a 3-page archive is
almost certainly a sketch / promo / variant cover gallery, NOT the
real comic, so it can't win against a 25-pager regardless of any
other signal. Concrete order, top first:

1. Page-count bucket (>=15 plausible single issue; 5-14 suspect;
   <5 fragment).
2. ComicInfo coverage (full_with_cvid > partial > none).
3. Page resolution — interior-sample pixel area preferred, cover
   area as fallback when the file predates the interior sample
   (a 1920x2951 scan beats a 1280x1960 one).
4. File size (proxy for content density + scan quality once
   resolution ties).
5. Format (CBZ > CBR as a minor tiebreaker — preference for
   GPL-clean archive opens, not a quality signal).
6. Raw page count (e.g. 29 beats 28 once everything above ties).
7. First-scanned timestamp — final deterministic tiebreaker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ComicInfoStatus,
    CvIssue,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    MatchStatus,
)
from app.services.cv_helpers import cv_image_url

# ---- Quality scoring ---------------------------------------------------


# Page-count thresholds for the top-tier bucket. A typical single
# issue runs 20-30 pages; collected editions and TPBs sit much higher;
# anything below ~5 pages is overwhelmingly a sketch / variant cover
# gallery / promo, not the actual comic. So we bin into three:
#   2 = plausibly the real issue (>=15 pages)
#   1 = suspect — partial scan, two-pager preview, etc. (5-14 pages)
#   0 = fragment — almost certainly not the issue (<5 pages)
# A fragment can never beat a plausible issue, even with full
# ComicInfo + huge cover dimensions, because every higher-quality
# signal on a 3-page file is just describing a higher-quality
# fragment. The thresholds are coarse on purpose — fine page-count
# differences fall to tier 6 (raw page count).
_PAGE_PLAUSIBLE = 15
_PAGE_SUSPECT = 5


def _page_bucket(page_count: int | None) -> int:
    n = page_count or 0
    if n >= _PAGE_PLAUSIBLE:
        return 2
    if n >= _PAGE_SUSPECT:
        return 1
    return 0


# Higher is better. A file with a CV ID embedded in its ComicInfo
# took the Stage-1 fast path and didn't depend on filename parsing,
# which is the most trustworthy outcome we have.
_COMICINFO_RANK = {
    ComicInfoStatus.FULL_WITH_CVID.value: 3,
    ComicInfoStatus.PARTIAL.value: 2,
    ComicInfoStatus.NONE.value: 1,
}


# Higher is better, but this is a *minor* tiebreaker — CBZ over CBR
# is a preference for GPL-clean opens, not a quality signal. CBZ
# wins only after everything above (plausibility, ComicInfo, cover,
# size) has tied. CB7 / PDF rank below because the matcher and cover
# paths weren't tuned for them.
_FORMAT_RANK = {
    "cbz": 4,
    "cbr": 3,
    "cb7": 2,
    "pdf": 1,
}


def _resolution_area(f: File) -> int:
    """Best available pixel-area signal for the file's pages.

    The interior sample (a mid-archive page captured at scan time)
    is the preferred resolution signal — see the scanner's
    ``_inspect_interior`` for why the cover alone is misleading in
    the cases the duplicates inspector exists to surface (re-encodes
    that shrink the cover, title-card "covers", wraparound captures
    that inflate the cover area without the interior following).
    When the interior columns are populated we use them; otherwise
    we fall back to the cover area so files scanned before the
    interior sample existed still compare against a real signal.

    ``None`` everywhere sorts to 0, so an uninspected file doesn't
    win on a missing dimension.
    """
    iw = f.interior_width or 0
    ih = f.interior_height or 0
    if iw and ih:
        return iw * ih
    cw = f.cover_width or 0
    ch = f.cover_height or 0
    return cw * ch


def _score(f: File) -> tuple:
    """Quality tuple — bigger compares as 'better'.

    Layered ordinal compare, top tier first. See the module docstring
    for the full ranking; in summary: page-count plausibility →
    ComicInfo coverage → cover resolution → file size → format → raw
    page count → recency. The top tier exists so a 3-page sketch
    variant can't beat a 25-page real comic on any combination of
    lower-tier signals.
    """
    ci_rank = _COMICINFO_RANK.get(f.comicinfo_status or "", 0)
    fmt_rank = _FORMAT_RANK.get((f.archive_format or "").lower(), 0)
    return (
        _page_bucket(f.page_count),
        ci_rank,
        _resolution_area(f),
        f.size_bytes or 0,
        fmt_rank,
        f.page_count or 0,
        # ISO timestamp of first scan as the final, deterministic
        # tiebreaker — most recent wins on exact ties. We use string
        # comparison rather than datetime so two callers that built
        # rows in the same wall-clock millisecond still order
        # consistently.
        (f.first_scanned_at.isoformat() if f.first_scanned_at else ""),
    )


# ---- Hash duplicates --------------------------------------------------


@dataclass
class HashLocation:
    """One current path under a hash-duplicate group."""

    location_id: uuid.UUID
    path: str
    mtime: datetime | None
    last_seen_at: datetime


@dataclass
class HashGroup:
    """All current paths a single ``files`` row points to.

    Hash duplicates are the cheapest kind to triage: the file content
    is byte-identical, so ``_score`` doesn't matter — pick whichever
    path you want to keep, the others can be deleted on disk without
    any quality loss. We sort paths lexicographically so the listing
    is stable across renders.
    """

    file_id: uuid.UUID
    sha256: str
    size_bytes: int | None
    archive_format: str | None
    page_count: int | None
    locations: list[HashLocation]
    excluded_from_matching: bool


async def list_hash_duplicates(
    db: AsyncSession, *, limit: int = 200
) -> list[HashGroup]:
    """Files whose ``file_locations`` includes more than one current
    (i.e., not ``missing_since``-set) row.

    Capped at ``limit`` groups so a degenerate library (every file
    duplicated to a backup folder) doesn't render a 50k-row table.
    Sorted by location-count descending so the most-duplicated files
    surface first — the easiest wins.
    """
    # Identify file_ids that have >1 current location. We do this in a
    # subquery and then join back to grab the metadata + every location,
    # to keep the round-trip down to one statement.
    multi_loc_stmt = (
        select(
            FileLocation.file_id,
            func.count(FileLocation.id).label("n_locations"),
        )
        .where(FileLocation.missing_since.is_(None))
        .group_by(FileLocation.file_id)
        .having(func.count(FileLocation.id) > 1)
        .order_by(func.count(FileLocation.id).desc())
        .limit(limit)
    )
    multi_rows = (await db.execute(multi_loc_stmt)).all()
    if not multi_rows:
        return []

    file_ids = [r.file_id for r in multi_rows]

    # Pull the file rows + their current locations in two batched queries.
    files_by_id: dict[uuid.UUID, File] = {
        f.id: f
        for f in (
            await db.execute(
                select(File).where(File.id.in_(file_ids))
            )
        ).scalars().all()
    }
    locations_by_file: dict[uuid.UUID, list[FileLocation]] = {}
    locs = (
        await db.execute(
            select(FileLocation)
            .where(FileLocation.file_id.in_(file_ids))
            .where(FileLocation.missing_since.is_(None))
            .order_by(FileLocation.path)
        )
    ).scalars().all()
    for loc in locs:
        locations_by_file.setdefault(loc.file_id, []).append(loc)

    groups: list[HashGroup] = []
    for r in multi_rows:
        f = files_by_id.get(r.file_id)
        if f is None:  # race: file deleted between subquery and lookup
            continue
        groups.append(
            HashGroup(
                file_id=f.id,
                sha256=f.sha256,
                size_bytes=f.size_bytes,
                archive_format=f.archive_format,
                page_count=f.page_count,
                excluded_from_matching=f.excluded_from_matching,
                locations=[
                    HashLocation(
                        location_id=loc.id,
                        path=loc.path,
                        mtime=loc.mtime,
                        last_seen_at=loc.last_seen_at,
                    )
                    for loc in locations_by_file.get(f.id, [])
                ],
            )
        )
    return groups


# ---- Issue duplicates -------------------------------------------------


@dataclass
class IssueFile:
    """One file row competing under an issue-duplicate group.

    ``path`` is whichever current location was picked first
    lexicographically — purely a display convenience so the operator
    can find the file on disk. If a file has multiple locations
    (hash-duplicate case), the hash-duplicate section above handles
    that separately. ``is_winner`` is server-side flagged so the
    template doesn't need to re-rank.
    """

    file_id: uuid.UUID
    path: str | None
    sha256: str
    archive_format: str | None
    page_count: int | None
    size_bytes: int | None
    cover_width: int | None
    cover_height: int | None
    # Interior-sample dimensions, captured by the scanner from a
    # mid-archive page. Preferred by the scorer when populated; the
    # template renders these alongside the cover dimensions so the
    # operator can see which signal won.
    interior_width: int | None
    interior_height: int | None
    comicinfo_status: str
    excluded_from_matching: bool
    first_scanned_at: datetime
    score: tuple  # for debugging / tests; template doesn't render it
    is_winner: bool


@dataclass
class IssueGroup:
    """Multiple files all matched to the same CV issue.

    ``volume_name`` / ``issue_number`` come from the cached cv_*
    tables for display. They might be NULL when the volume / issue
    is itself a stub — the inspector still surfaces the group with
    placeholders so the operator can recognise it by path.

    ``cover_url`` is the canonical ComicVine cover for this issue.
    Always populated for groups that reach the listing — the service
    filters out groups whose CV issue hasn't been hydrated yet (no
    resolvable image URL), since the whole point of showing the CV
    cover here is to compare it against the file thumbnails. A
    file-cover stand-in would defeat that comparison: the
    "reference" would just be one of the files we're trying to
    evaluate.
    """

    issue_cv_id: int
    volume_cv_id: int | None
    volume_name: str | None
    issue_number: str | None
    issue_name: str | None
    cover_date: datetime | None
    cover_url: str
    files: list[IssueFile]


@dataclass
class IssueDuplicateListing:
    """Result envelope from ``list_issue_duplicates``.

    ``groups`` are the ready-to-render groups whose CV issue has a
    resolvable cover image. ``deferred_volume_cv_ids`` are the
    distinct volumes that own one or more groups whose issues
    *haven't* been hydrated yet — the route uses these to fire bulk
    hydration jobs so the next page load can show those groups.
    ``deferred_count`` is the count of suppressed groups; the
    template surfaces it as a banner so the operator isn't
    surprised when a refresh produces new rows.
    """

    groups: list[IssueGroup]
    deferred_count: int
    deferred_volume_cv_ids: list[int]


# Match statuses that count as "this file is claimed by this issue."
# Pending / unmatched / rejected files don't enter the duplicate
# inventory — they're someone else's problem (the review queue or the
# matcher's retry loop). LOCAL / SUPPLEMENT don't have an issue_cv_id
# anyway so the WHERE clause filters them out implicitly.
_RESOLVED_STATUSES = (
    MatchStatus.AUTO.value,
    MatchStatus.CONFIRMED.value,
)


async def list_issue_duplicates(
    db: AsyncSession, *, limit: int = 200
) -> IssueDuplicateListing:
    """Issues that have more than one resolved file claiming them.

    Capped at ``limit`` groups, sorted by file-count descending so the
    worst offenders surface first. Resolved means ``status`` is in the
    set above — pending / unmatched / rejected files aren't counted
    against an issue (they're handled by the review queue, not here).

    **Mid-hydration suppression.** A group's CV cover is the operator's
    reference image — they're comparing it against the file thumbnails
    looking for mismatches. If the CV issue's ``raw_payload`` doesn't
    yet carry image data (still a stub, or bulk-hydration in flight),
    showing the group would defeat the comparison. We skip those
    groups from the listing and report their volumes via
    ``deferred_volume_cv_ids`` so the route can re-enqueue
    bulk-hydration. The next page load includes them once the
    payload lands.
    """
    # First pass: discover issues with >1 resolved file.
    multi_stmt = (
        select(
            FileMatch.issue_cv_id,
            func.count(FileMatch.file_id).label("n_files"),
        )
        .where(FileMatch.issue_cv_id.is_not(None))
        .where(FileMatch.status.in_(_RESOLVED_STATUSES))
        .group_by(FileMatch.issue_cv_id)
        .having(func.count(FileMatch.file_id) > 1)
        .order_by(func.count(FileMatch.file_id).desc())
        .limit(limit)
    )
    multi_rows = (await db.execute(multi_stmt)).all()
    if not multi_rows:
        return IssueDuplicateListing(
            groups=[], deferred_count=0, deferred_volume_cv_ids=[]
        )

    issue_cv_ids = [r.issue_cv_id for r in multi_rows]

    # Pull every (file, location) for these issues in one statement
    # then partition Python-side. Joining files + matches + the
    # earliest-by-path location is the second round-trip we want to
    # keep this query bounded.
    match_rows_stmt = (
        select(
            FileMatch.issue_cv_id,
            File,
        )
        .join(File, File.id == FileMatch.file_id)
        .where(FileMatch.issue_cv_id.in_(issue_cv_ids))
        .where(FileMatch.status.in_(_RESOLVED_STATUSES))
    )
    matches = (await db.execute(match_rows_stmt)).all()

    # Map file_id → a representative current path (lex-first, mirrors
    # the hash-duplicate display). One round-trip for every relevant
    # location.
    all_file_ids = [m.File.id for m in matches]
    locs_stmt = (
        select(FileLocation.file_id, FileLocation.path)
        .where(FileLocation.file_id.in_(all_file_ids))
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.path)
    )
    paths_by_file: dict[uuid.UUID, str] = {}
    for fid, path in (await db.execute(locs_stmt)).all():
        # First (lex-smallest) wins; later rows for the same file_id
        # don't overwrite — gives a stable representative path.
        paths_by_file.setdefault(fid, path)

    # Issue + volume context for display.
    issues_by_id: dict[int, CvIssue] = {
        i.cv_id: i
        for i in (
            await db.execute(
                select(CvIssue).where(CvIssue.cv_id.in_(issue_cv_ids))
            )
        ).scalars().all()
    }
    volume_ids = {
        i.volume_cv_id
        for i in issues_by_id.values()
        if i.volume_cv_id is not None
    }
    volumes_by_id: dict[int, CvVolume] = {}
    if volume_ids:
        volumes_by_id = {
            v.cv_id: v
            for v in (
                await db.execute(
                    select(CvVolume).where(CvVolume.cv_id.in_(list(volume_ids)))
                )
            ).scalars().all()
        }

    # Bucket files by issue, then hand each bucket off to the shared
    # per-group builder — the same builder ``get_issue_duplicate_group``
    # uses for its single-issue path. Keeping the loop body in one
    # function means a scoring tweak or a cover-URL rule change
    # applies to both surfaces without drift.
    by_issue: dict[int, list[File]] = {}
    for row in matches:
        by_issue.setdefault(row.issue_cv_id, []).append(row.File)

    groups: list[IssueGroup] = []
    # Volumes whose groups we suppressed because the CV issue lacks
    # cover data. ``dict`` rather than ``set`` so we preserve order
    # — useful when the operator looks at the banner and we want
    # the most-pressing volumes first (worst-offender order is
    # preserved by ``issue_cv_ids``).
    deferred_volume_cv_ids: dict[int, None] = {}
    deferred_count = 0
    for issue_cv_id in issue_cv_ids:
        files = by_issue.get(issue_cv_id, [])
        issue = issues_by_id.get(issue_cv_id)
        volume = (
            volumes_by_id.get(issue.volume_cv_id)
            if issue and issue.volume_cv_id is not None
            else None
        )
        group, deferred_volume = _build_issue_group(
            issue_cv_id=issue_cv_id,
            files=files,
            paths_by_file=paths_by_file,
            issue=issue,
            volume=volume,
        )
        if group is not None:
            groups.append(group)
        elif deferred_volume is not None:
            # Hard requirement check inside the builder failed (no CV
            # cover); remember the volume so the caller can fire its
            # bulk-hydration job.
            deferred_count += 1
            deferred_volume_cv_ids.setdefault(deferred_volume, None)
        # else: degenerate group (<2 files post-race) — silently skipped.
    return IssueDuplicateListing(
        groups=groups,
        deferred_count=deferred_count,
        deferred_volume_cv_ids=list(deferred_volume_cv_ids.keys()),
    )


def _build_issue_group(
    *,
    issue_cv_id: int,
    files: list[File],
    paths_by_file: dict[uuid.UUID, str],
    issue: CvIssue | None,
    volume: CvVolume | None,
) -> tuple[IssueGroup | None, int | None]:
    """Score, rank, and package a single CV issue's files.

    Returns ``(group, None)`` when the group is ready to render.

    Returns ``(None, deferred_volume_cv_id)`` when the group has ≥2
    files but the CV issue's cover hasn't been hydrated yet — the
    second element is the volume cv_id the caller should revalidate
    so the next page load includes the group. The CV cover is the
    operator's reference image (everything else is compared against
    it); without it the comparison is pointless.

    Returns ``(None, None)`` when the bucket has fewer than 2 files
    — a race-tolerance guard: a concurrent re-match might have
    demoted a row out of resolved-status between the count and the
    fetch.
    """
    if len(files) < 2:
        return None, None
    scored: list[tuple[tuple, File]] = sorted(
        ((_score(f), f) for f in files),
        key=lambda t: t[0],
        reverse=True,
    )
    winner_id = scored[0][1].id
    rendered: list[IssueFile] = [
        IssueFile(
            file_id=f.id,
            path=paths_by_file.get(f.id),
            sha256=f.sha256,
            archive_format=f.archive_format,
            page_count=f.page_count,
            size_bytes=f.size_bytes,
            cover_width=f.cover_width,
            cover_height=f.cover_height,
            interior_width=f.interior_width,
            interior_height=f.interior_height,
            comicinfo_status=f.comicinfo_status,
            excluded_from_matching=f.excluded_from_matching,
            first_scanned_at=f.first_scanned_at,
            score=s,
            is_winner=(f.id == winner_id),
        )
        for s, f in scored
    ]
    cover_url = (
        cv_image_url(issue.raw_payload, "thumb")
        if issue is not None
        else None
    )
    if cover_url is None:
        deferred_vol = (
            issue.volume_cv_id
            if issue is not None and issue.volume_cv_id is not None
            else None
        )
        return None, deferred_vol
    return (
        IssueGroup(
            issue_cv_id=issue_cv_id,
            volume_cv_id=(issue.volume_cv_id if issue else None),
            volume_name=(volume.name if volume else None),
            issue_number=(issue.issue_number if issue else None),
            issue_name=(issue.name if issue else None),
            cover_date=(issue.cover_date if issue else None),
            cover_url=cover_url,
            files=rendered,
        ),
        None,
    )


async def get_issue_duplicate_group(
    db: AsyncSession, issue_cv_id: int
) -> IssueGroup | None:
    """The single-issue counterpart to ``list_issue_duplicates``.

    Returns the ``IssueGroup`` for one specific CV issue when it has
    more than one resolved file and its CV cover is hydrated;
    otherwise None. Used by the per-issue Compare page on
    ``/issue/{cv_id}/compare``.

    Falls back to None — *not* a deferred-marker — when the CV cover
    is missing. The per-issue page is a deliberate action a reviewer
    takes; a temporarily-missing cover should send them back to the
    issue page (which has its own bulk-hydration nudge) rather than
    show an empty compare view. The list-side surface handles
    deferred groups via the banner because it's the catalog overview;
    the single-issue surface has no comparable use for one.
    """
    # Pull every resolved-status file claiming this issue. The hot
    # path here is one issue + a few files, so we don't share the
    # listing's bulk-query optimisations — separate, much smaller
    # round-trips read more clearly.
    match_rows_stmt = (
        select(File)
        .join(FileMatch, FileMatch.file_id == File.id)
        .where(FileMatch.issue_cv_id == issue_cv_id)
        .where(FileMatch.status.in_(_RESOLVED_STATUSES))
    )
    files = (await db.execute(match_rows_stmt)).scalars().all()
    if len(files) < 2:
        return None

    # Representative path per file — same lex-first convention as the
    # listing.
    file_ids = [f.id for f in files]
    locs_stmt = (
        select(FileLocation.file_id, FileLocation.path)
        .where(FileLocation.file_id.in_(file_ids))
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.path)
    )
    paths_by_file: dict[uuid.UUID, str] = {}
    for fid, path in (await db.execute(locs_stmt)).all():
        paths_by_file.setdefault(fid, path)

    # Issue + volume context for the group header.
    issue = await db.get(CvIssue, issue_cv_id)
    volume = (
        await db.get(CvVolume, issue.volume_cv_id)
        if issue is not None and issue.volume_cv_id is not None
        else None
    )

    group, _deferred = _build_issue_group(
        issue_cv_id=issue_cv_id,
        files=list(files),
        paths_by_file=paths_by_file,
        issue=issue,
        volume=volume,
    )
    return group


# ---- Admin actions ----------------------------------------------------


async def mark_file_excluded(
    db: AsyncSession, file_id: uuid.UUID
) -> bool:
    """Flip ``files.excluded_from_matching`` to True.

    The matcher's existing guard at the top of ``match_file`` returns
    UNMATCHED for excluded files; the scanner also skips re-enqueue.
    So this is the "stop touching this duplicate, I'll deal with it
    out-of-band" lever — surfaces in the duplicates inspector
    alongside the path.

    Returns False when the file no longer exists (race with a manual
    delete or a TRUNCATE).
    """
    file_row = await db.get(File, file_id)
    if file_row is None:
        return False
    file_row.excluded_from_matching = True
    await db.commit()
    return True


# Total-count helpers for the admin Health page stat. Counted as
# *groups*, not files — "you have N issues with multiple files
# claiming them" is more useful than "you have 2N files involved."


async def count_hash_duplicate_groups(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(
            select(FileLocation.file_id)
            .where(FileLocation.missing_since.is_(None))
            .group_by(FileLocation.file_id)
            .having(func.count(FileLocation.id) > 1)
            .subquery()
        )
    )
    return (await db.execute(stmt)).scalar_one() or 0


async def count_issue_duplicate_groups(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(
            select(FileMatch.issue_cv_id)
            .where(FileMatch.issue_cv_id.is_not(None))
            .where(FileMatch.status.in_(_RESOLVED_STATUSES))
            .group_by(FileMatch.issue_cv_id)
            .having(func.count(FileMatch.file_id) > 1)
            .subquery()
        )
    )
    return (await db.execute(stmt)).scalar_one() or 0


__all__ = [
    "HashGroup",
    "HashLocation",
    "IssueDuplicateListing",
    "IssueFile",
    "IssueGroup",
    "count_hash_duplicate_groups",
    "count_issue_duplicate_groups",
    "list_hash_duplicates",
    "list_issue_duplicates",
    "mark_file_excluded",
]
