"""Review-queue service.

Powers the ``/review`` admin UI. Each public function returns plain
dataclasses with everything the templates need — paths, candidate
issue metadata pulled from the persisted ``FileMatch.candidates``
blob, optional cover URLs harvested from the cached ``cv_issues``
rows.

Design choices worth calling out:

  * Candidates come from ``FileMatch.candidates`` (set by the matcher
    pipeline when it ran). No fresh CV calls on review-page load —
    those would burn rate budget every time an admin glanced at the
    queue. Files whose matcher output predates the ``candidates``
    column show up with an empty candidate list and a "rematch"
    affordance in the UI.
  * The service joins ``cv_issues`` and ``cv_volumes`` for the
    candidates' display data (cover thumb, volume name, year). Issues
    we haven't hydrated render with whatever the matcher persisted
    (volume_name, volume_year, issue_number) and a "load cover"
    placeholder.
  * Filtering / sorting / pagination live here so the route stays a
    thin shim. The matcher's confidence thresholds (PENDING band
    0.50-0.85) bracket the default queue; admins can widen via
    filters when needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.archives.comicinfo import ComicInfoStatus
from app.matcher.filename import parse_filename
from app.matcher.pipeline import find_issue_by_number
from app.models import (
    CvIssue,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    MatchSource,
    MatchStatus,
)
from app.services.cv_helpers import (
    classify_cv_volume,
    classify_file_format,
    cv_image_url,
    sort_key_issue_number,
)

# ---- Dataclasses -------------------------------------------------------


@dataclass
class CandidateRow:
    """One scored candidate the matcher considered for a pending file.

    The fields mirror ``FileMatch.candidates`` JSON entries plus
    enriched display data harvested from ``cv_issues`` /
    ``cv_volumes`` so the template can render without further DB
    work."""

    issue_cv_id: int
    volume_cv_id: int | None
    volume_name: str | None
    volume_year: int | None
    issue_number: str | None
    confidence: float
    # Cover thumbnail URL from the cached issue row. ``None`` when the
    # issue is a stub (no ``raw_payload``) or has no image — the
    # template falls back to a "no cover" placeholder.
    cover_url: str | None = None
    # Optional issue name (e.g. "Wedding of the Century"). CV often
    # leaves this null for serial issues; the volume name + issue
    # number identify the candidate anyway.
    issue_name: str | None = None
    # The candidate's parent volume classified as ongoing / limited /
    # one_shot / collection. ``None`` when the volume isn't cached
    # locally (nothing to classify from).
    format: str | None = None
    # The parent volume's ComicVine description (wiki HTML). Surfaced
    # as a hover popover on the matching screens. ``None`` when the
    # volume isn't cached or CV has no description.
    volume_description: str | None = None
    # The parent volume's publisher name. ``None`` for a stub volume.
    volume_publisher: str | None = None


@dataclass
class VolumeSuggestion:
    """One CV volume aggregated across a group's candidates.

    Built by ``list_pending_groups``: count how many files in a
    series-group list this volume as a candidate, sort by count
    descending, take the top N. Drives the volume-confirm UI -- the
    volume that the most files in the group agree on is the most
    likely "the matcher was right about the volume, just couldn't
    nail the confidence" case.
    """

    volume_cv_id: int
    volume_name: str | None
    volume_year: int | None
    count_in_group: int
    cover_url: str | None = None
    # ongoing / limited / one_shot / collection — see
    # ``classify_volume_format``. ``None`` when the volume isn't cached.
    format: str | None = None
    # The volume's ComicVine description (wiki HTML) for the matching
    # screens' hover popover. ``None`` when not cached / no description.
    description: str | None = None
    # The volume's publisher name. ``None`` for a stub volume.
    publisher: str | None = None


@dataclass
class PendingRow:
    """One row in the ``/review`` queue.

    The file's pending match plus its top candidate + a few signals
    the queue page needs (filename, path, hash prefix, current
    confidence). The full per-file review page reads more, but this
    shape keeps the queue cheap to render."""

    file_id: Any  # uuid.UUID
    path: str
    filename: str
    size_bytes: int | None
    sha256_short: str  # first 12 chars for display
    comicinfo_status: str
    confidence: float | None
    source: str
    # MatchStatus value — ``pending`` (matcher has a guess) or
    # ``unmatched`` (matcher found nothing usable). Both surface in
    # the queue; the row badges which is which.
    status: str
    # Coarse single-issue vs collected-edition guess from the file's
    # page count — ``issue`` / ``collection`` / ``unknown``. Lets the
    # queue flag a TPB sitting in a run of single issues.
    format_guess: str
    # ``top_candidate`` is ``candidates[0]`` — kept as its own field
    # because most of the queue UI only needs the strongest match.
    # ``candidates`` carries the matcher's full ranked shortlist
    # (up to 5) so the review row can expand to show the runners-up;
    # the right volume often sits at rank 2-3 when the top score was
    # a near-tie. ``candidate_count`` is ``len(candidates)``.
    top_candidate: CandidateRow | None
    candidates: list[CandidateRow]
    candidate_count: int
    # Filename-parse output — the matcher's input signals from the
    # basename alone. Surfaced on the queue so a reviewer can see
    # at a glance whether a PENDING row is bad because the parser
    # failed (no series → nothing to search) or because scoring
    # was inconclusive. ``parsed_volume_year`` is the explicit
    # ``Volume YYYY`` suffix some scanlators tag with; ``parsed_year``
    # is the issue's cover year. ``parsed_long_series`` captures the
    # full pre-issue-marker prefix the matcher uses as a fallback
    # search term — see ``_extract_long_series`` in
    # ``app.matcher.filename``. Equal to ``parsed_series`` when no
    # subtitle was present, in which case the template hides it to
    # avoid noise.
    parsed_series: str | None
    parsed_issue_number: str | None
    parsed_year: int | None
    parsed_volume_year: int | None
    parsed_long_series: str | None


@dataclass
class PendingGroup:
    """A bundle of PENDING files that share a parsed series.

    Surfaces on ``/review`` as one card per group instead of per-file
    rows. The reviewer picks the right CV volume for the whole group
    in one action; the system walks each file and confirms by
    issue_number match. Files for which the chosen volume doesn't
    have a matching issue stay PENDING and surface individually.

    ``series_key`` is the grouping key (long_series when different
    from parsed_series, else parsed_series, else None for files
    whose name didn't parse). ``rows`` is ordered by confidence
    desc so the strongest candidates surface first when the group
    expands.
    """

    series_key: str | None
    file_count: int
    year_min: int | None
    year_max: int | None
    top_volume_suggestions: list[VolumeSuggestion]
    rows: list[PendingRow]


# ---- Public functions --------------------------------------------------

# The match statuses the review queue surfaces. PENDING is "the matcher
# has a guess but isn't confident"; UNMATCHED is "the matcher couldn't
# find anything usable" — both need a human, so both belong in the
# queue. (The ``pending`` in the function names below predates UNMATCHED
# being included; kept to avoid a wide rename.)
_REVIEWABLE_STATUSES = (
    MatchStatus.PENDING.value,
    MatchStatus.UNMATCHED.value,
)


async def count_pending_matches(db: AsyncSession) -> int:
    """Cheap COUNT(*) of files needing review (PENDING + UNMATCHED).

    Used by the home-page "files need review" callout and the queue
    page's "Showing N of M" line. Files with
    ``excluded_from_matching = True`` are dropped — the bulk-exclude
    action on the review surfaces sets that flag, and an excluded
    file shouldn't keep counting against the queue.
    """
    stmt = (
        select(func.count(FileMatch.file_id))
        .join(File, File.id == FileMatch.file_id)
        .where(FileMatch.status.in_(_REVIEWABLE_STATUSES))
        .where(File.excluded_from_matching.is_(False))
    )
    return int((await db.execute(stmt)).scalar_one())


async def list_pending_matches(
    db: AsyncSession,
    *,
    limit: int = 30,
    offset: int = 0,
    file_ids: list[Any] | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
) -> tuple[list[PendingRow], int]:
    """Return one page of pending-review files.

    Joined query: ``file_matches`` JOIN ``files`` JOIN one current
    ``file_locations`` row JOIN cached ``cv_issues`` + ``cv_volumes``
    rows for the candidates' display data.

    Ordering: confidence descending, so the strongest "almost-auto"
    matches surface first — those are the ones an admin can knock
    out fastest. Ties broken by ``matched_at`` ascending so older
    pending rows don't get permanently buried under fresh ones.

    ``file_ids``: when provided, restricts the fetch to exactly that
    set (``WHERE file_id IN (...)``) and skips ``limit`` / ``offset``
    — the IN clause is the bound. Used by ``list_pending_groups``'
    enrichment pass and the single-group preview callers, which both
    pre-select file ids via the cheap path-only helper. ``None``
    (the default) is the unbounded paged-query behaviour the queue
    pagination still uses.

    Returns ``(rows, total_unfiltered_count)`` — the unfiltered total
    drives the queue page's pagination + the home-page callout when
    the user hasn't applied filters.
    """
    total = await count_pending_matches(db)

    # Build the base query. We pull one location per file via a
    # correlated subquery — ``FileLocation`` is many-to-one with
    # ``files`` (a sha256 can sit at multiple paths), and the
    # queue cares about *a* path, not all of them. ``missing_since
    # IS NULL`` filters to currently-present locations only.
    loc_subq = (
        select(FileLocation.path)
        .where(FileLocation.file_id == File.id)
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.last_seen_at.desc())
        .limit(1)
        .scalar_subquery()
        .label("path")
    )

    stmt = (
        select(
            FileMatch.file_id,
            FileMatch.confidence,
            FileMatch.source,
            FileMatch.status,
            FileMatch.candidates,
            File.sha256,
            File.size_bytes,
            File.comicinfo_status,
            File.page_count,
            loc_subq,
        )
        .join(File, File.id == FileMatch.file_id)
        .where(FileMatch.status.in_(_REVIEWABLE_STATUSES))
        # Files marked excluded_from_matching shouldn't reappear in the
        # queue — the bulk-exclude action on the volume-confirm /
        # volume-search pages sets this flag explicitly to say "stop
        # surfacing this for review."
        .where(File.excluded_from_matching.is_(False))
    )
    if file_ids is not None:
        # Empty list → empty result, intentionally short-circuit the IN.
        if not file_ids:
            return [], total
        stmt = stmt.where(FileMatch.file_id.in_(file_ids))
    if min_confidence is not None:
        stmt = stmt.where(FileMatch.confidence >= Decimal(str(min_confidence)))
    if max_confidence is not None:
        stmt = stmt.where(FileMatch.confidence <= Decimal(str(max_confidence)))
    stmt = stmt.order_by(FileMatch.confidence.desc(), FileMatch.matched_at.asc())
    if file_ids is None:
        # Page-mode: bound the fetch with ``limit`` / ``offset``.
        # When ``file_ids`` was provided, the IN clause is the bound.
        stmt = stmt.limit(limit).offset(offset)

    raw_rows = (await db.execute(stmt)).all()

    # Collect both candidate issue IDs AND their volume IDs so we can
    # enrich in two batch queries instead of N. The matcher persists
    # up to 5 candidates per file; for a 30-row page that's ~150
    # issues + their (much smaller, ~5-50 distinct) volume rows.
    issue_ids: set[int] = set()
    volume_ids: set[int] = set()
    for row in raw_rows:
        for c in row.candidates or []:
            cid = c.get("issue_cv_id")
            if isinstance(cid, int):
                issue_ids.add(cid)
            vid = c.get("volume_cv_id")
            if isinstance(vid, int):
                volume_ids.add(vid)

    issue_by_id: dict[int, CvIssue] = {}
    if issue_ids:
        issue_stmt = select(CvIssue).where(CvIssue.cv_id.in_(issue_ids))
        for issue in (await db.execute(issue_stmt)).scalars():
            issue_by_id[issue.cv_id] = issue

    # Volume rows are reliably hydrated because the matcher fetches
    # ``/volume/{id}/`` for each candidate's parent — that response
    # always carries the volume's image dict. We use the volume cover
    # as the candidate thumbnail's fallback when the issue itself is
    # a stub (CV's volume payload doesn't include image data for
    # nested issues, so freshly-matched candidates always start out
    # without an issue-level cover URL).
    volume_by_id: dict[int, CvVolume] = {}
    if volume_ids:
        volume_stmt = select(CvVolume).where(CvVolume.cv_id.in_(volume_ids))
        for volume in (await db.execute(volume_stmt)).scalars():
            volume_by_id[volume.cv_id] = volume

    pending_rows: list[PendingRow] = []
    for row in raw_rows:
        candidates_blob = row.candidates or []
        # Build every persisted candidate, not just the first.
        # ``_build_candidate`` returns None for blobs missing a valid
        # ``issue_cv_id`` — drop those so the list only holds rows the
        # template can actually render. Order is preserved: the
        # matcher persists candidates already sorted by score.
        candidates = [
            built
            for c in candidates_blob
            if (built := _build_candidate(c, issue_by_id, volume_by_id)) is not None
        ]
        top = candidates[0] if candidates else None
        path = row.path or "(no current location)"
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        # Filename parse is cheap (regex via comicfn2dict) so we run
        # it inline rather than persist. Re-parsing on each queue load
        # also picks up parser improvements automatically — without
        # this, a row matched under an older parser would forever
        # display its old hint values.
        parsed = parse_filename(filename)
        pending_rows.append(
            PendingRow(
                file_id=row.file_id,
                path=path,
                filename=filename,
                size_bytes=row.size_bytes,
                sha256_short=(row.sha256 or "")[:12],
                comicinfo_status=row.comicinfo_status or ComicInfoStatus.NONE.value,
                confidence=float(row.confidence) if row.confidence is not None else None,
                source=row.source,
                status=row.status,
                format_guess=classify_file_format(row.page_count),
                top_candidate=top,
                candidates=candidates,
                candidate_count=len(candidates),
                parsed_series=parsed.series,
                parsed_issue_number=parsed.issue_number,
                parsed_year=parsed.year,
                parsed_volume_year=parsed.volume_year,
                parsed_long_series=parsed.long_series,
            )
        )

    return pending_rows, total


# Cap on rows fetched into a single grouping pass. Realistic libraries
# rarely have more than a few hundred PENDING matches at once; this
# guard keeps an out-of-control matcher run from pulling tens of
# thousands of rows into memory just to render the queue. When the
# cap kicks in, ``list_pending_groups`` returns a truthy
# ``hit_row_cap`` flag so the template can surface "showing the
# first N pending files, refine the filter to see the rest."
PENDING_GROUP_ROW_CAP = 500

# How many volume candidates per group to surface as confirm-target
# suggestions. The matcher persists up to 5 candidates per file, and
# realistic groups tend to converge on 1-3 distinct volumes; 5 is
# generous without overwhelming the card.
TOP_VOLUME_SUGGESTIONS = 5


def _group_key_from_parsed(parsed_series: str | None, parsed_long_series: str | None) -> str | None:
    """The grouping rule, in terms of the two parser outputs.

    Prefers ``parsed_long_series`` when it differs from
    ``parsed_series`` — those are the cases where the matcher's
    second search term is the better identifier (the
    ``"Avengers - No More Bullying"`` story). Falls back to the
    short ``parsed_series`` otherwise, and to ``None`` for files
    whose name parser couldn't pull a series at all (those go into
    an "(unparsed)" bucket so they're still reviewable).

    Split out so the cheap path-only pre-pass (which has the parsed
    fields but no PendingRow yet) and the enriched-row code path can
    use the exact same logic.
    """
    if parsed_long_series and parsed_series and parsed_long_series.lower() != parsed_series.lower():
        return parsed_long_series
    return parsed_series or parsed_long_series


def _group_key(row: PendingRow) -> str | None:
    """Convenience wrapper over ``_group_key_from_parsed`` for enriched
    rows."""
    return _group_key_from_parsed(row.parsed_series, row.parsed_long_series)


@dataclass
class _CheapPendingFile:
    """A reviewable file as seen by the cheap pre-pass.

    Path-only fetch — no candidate enrichment, no volume hydration —
    plus the filename parse so the row can be bucketed by group key
    before we decide which buckets to fully enrich. Used by
    ``list_pending_groups`` to make the row cap *group-atomic*
    (groups either fully included or fully excluded, never split),
    and by the single-group callers (volume-confirm preview, local-
    group preview, group-reference summary) to find every file in
    one group without being chopped by that same cap.
    """

    file_id: Any  # uuid.UUID
    path: str
    filename: str
    confidence: float | None
    parsed_issue_number: str | None
    parsed_year: int | None
    parsed_volume_year: int | None


async def _list_pending_files_by_group(
    db: AsyncSession,
    *,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
) -> dict[str | None, list[_CheapPendingFile]]:
    """Cheap pre-pass: every reviewable file_matches row, bucketed by
    parsed-series group key.

    Selects only the columns we need to compute group keys — file_id,
    one current path, confidence, matched_at — and parses each
    filename in Python. No JOIN to ``cv_issues`` / ``cv_volumes``, no
    walk of the ``FileMatch.candidates`` JSONB blob. Even on a 5k-
    pending library this is sub-second; the row cap exists for the
    *enrichment* pass, not this one.

    Buckets within each group are ordered by confidence DESC, then
    matched_at ASC — same ordering the enriched fetch uses — so the
    strongest-confidence row in each group still surfaces first when
    the group's row list is rendered.
    """
    # One current location per file via correlated subquery — same
    # pattern ``list_pending_matches`` uses.
    loc_subq = (
        select(FileLocation.path)
        .where(FileLocation.file_id == File.id)
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.last_seen_at.desc())
        .limit(1)
        .scalar_subquery()
        .label("path")
    )

    stmt = (
        select(
            FileMatch.file_id,
            FileMatch.confidence,
            FileMatch.matched_at,
            loc_subq,
        )
        .join(File, File.id == FileMatch.file_id)
        .where(FileMatch.status.in_(_REVIEWABLE_STATUSES))
        # Skip files the operator has explicitly excluded — same
        # filter the enriched ``list_pending_matches`` uses, so the
        # cheap pre-pass and the enriched pass agree on visibility.
        .where(File.excluded_from_matching.is_(False))
    )
    if min_confidence is not None:
        stmt = stmt.where(FileMatch.confidence >= Decimal(str(min_confidence)))
    if max_confidence is not None:
        stmt = stmt.where(FileMatch.confidence <= Decimal(str(max_confidence)))
    stmt = stmt.order_by(FileMatch.confidence.desc(), FileMatch.matched_at.asc())

    cheap_rows = (await db.execute(stmt)).all()

    buckets: dict[str | None, list[_CheapPendingFile]] = {}
    for r in cheap_rows:
        path = r.path or "(no current location)"
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        parsed = parse_filename(filename)
        key = _group_key_from_parsed(parsed.series, parsed.long_series)
        buckets.setdefault(key, []).append(
            _CheapPendingFile(
                file_id=r.file_id,
                path=path,
                filename=filename,
                confidence=(float(r.confidence) if r.confidence is not None else None),
                parsed_issue_number=parsed.issue_number,
                parsed_year=parsed.year,
                parsed_volume_year=parsed.volume_year,
            )
        )
    return buckets


async def list_pending_groups(
    db: AsyncSession,
    *,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
) -> tuple[list[PendingGroup], int, bool]:
    """Group PENDING file_matches by parsed series.

    Returns ``(groups, total_files_seen, hit_row_cap)``:
      * ``groups`` — list of ``PendingGroup``, sorted by file_count
        descending (biggest groups first; that's where the
        volume-confirm payoff is largest).
      * ``total_files_seen`` — sum of file counts across all groups.
        Drives the queue header's "Showing N files across M groups"
        line.
      * ``hit_row_cap`` — True when one or more groups were dropped
        because including them would have crossed the row cap;
        signals the reviewer that they're seeing a partial view.

    Filtering by confidence still applies — files outside the band
    are dropped BEFORE grouping. A group whose only members fall
    out of the filter disappears entirely.

    The row cap is *group-atomic*: a group is either fully included
    (every member enriched and rendered) or fully excluded (none of
    its files appear, the group counts toward ``hit_row_cap``).
    Splitting a group across the cap caused confusing "group is
    growing as I review" behaviour — confirming a high-confidence
    member would shift the cap and surface a previously-hidden low-
    confidence sibling, leaving the visible count unchanged or even
    growing. Atomic groups make the count match the reviewer's
    mental model: confirm one, the count drops by one.
    """
    # Stage 1: cheap path-only pass. Cost is one JOIN per pending row
    # (no candidate enrichment, no volume hydration); for a 5k-pending
    # library this is well under a second of DB time and a few hundred
    # KB of memory.
    cheap_buckets = await _list_pending_files_by_group(
        db,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
    )

    # Pick groups in big-first order until adding the next group would
    # cross the cap. Always include at least the first group — even
    # when it's larger than the cap on its own — so the reviewer
    # always sees *something*, and any group present is whole.
    ordered_keys = sorted(
        cheap_buckets.keys(),
        key=lambda k: (-len(cheap_buckets[k]), (k or "").lower()),
    )
    selected_keys: list[str | None] = []
    selected_file_ids: list[Any] = []
    for key in ordered_keys:
        bucket = cheap_buckets[key]
        if selected_file_ids and (len(selected_file_ids) + len(bucket) > PENDING_GROUP_ROW_CAP):
            # Adding this group would cross the cap. Stop — every
            # remaining group is reported via ``hit_row_cap``.
            break
        selected_keys.append(key)
        selected_file_ids.extend(f.file_id for f in bucket)

    hit_row_cap = len(selected_keys) < len(ordered_keys)
    if not selected_file_ids:
        return [], 0, hit_row_cap

    # Stage 2: enrich just the file_ids we picked. The IN clause is
    # the bound; ``list_pending_matches`` short-circuits ``limit`` /
    # ``offset`` in this mode.
    rows, _total_unfiltered = await list_pending_matches(
        db,
        file_ids=selected_file_ids,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
    )

    # Re-bucket the enriched rows by series key. Same group keys as
    # the cheap pass produced (filenames don't change between
    # passes), so every selected_key should have a non-empty bucket.
    enriched_buckets: dict[str | None, list[PendingRow]] = {}
    for row in rows:
        key = _group_key(row)
        enriched_buckets.setdefault(key, []).append(row)

    # Build groups in the picking order — biggest first by file count.
    # ``_aggregate_volume_suggestions`` walks every candidate of every
    # file in the bucket and tallies the distinct volumes; see its
    # docstring. The same helper powers the volume-confirm page's
    # volume picker so the queue card and the picker always agree on
    # the candidate set.
    groups: list[PendingGroup] = []
    total_files_seen = 0
    for key in selected_keys:
        group_rows = enriched_buckets.get(key) or []
        if not group_rows:
            # Defensive: cheap pass saw the group but the enrichment
            # pass came back empty. Skip rather than emit a 0-file group.
            continue
        sugg = await _aggregate_volume_suggestions(db, group_rows)
        years = [r.parsed_year for r in group_rows if r.parsed_year is not None]
        # Display order: natural sort by the issue number parsed from
        # each filename, so the group's thumbnail strip and expanded
        # file list read 1, 2, 3, … rather than matcher-confidence
        # order. Done after ``_aggregate_volume_suggestions`` so the
        # candidate tally (and thus the suggested volume) is unchanged.
        group_rows.sort(key=lambda r: sort_key_issue_number(r.parsed_issue_number))
        groups.append(
            PendingGroup(
                series_key=key,
                file_count=len(group_rows),
                year_min=min(years) if years else None,
                year_max=max(years) if years else None,
                top_volume_suggestions=sugg[:TOP_VOLUME_SUGGESTIONS],
                rows=group_rows,
            )
        )
        total_files_seen += len(group_rows)

    # ``selected_keys`` is already in (-file_count, name) order from
    # the cheap pass — same as the previous behaviour — but re-sort
    # by the *enriched* file count to defend against a row dropping
    # out between passes (e.g. a concurrent confirm).
    groups.sort(key=lambda g: (-g.file_count, (g.series_key or "").lower()))

    return groups, total_files_seen, hit_row_cap


@dataclass
class GroupReference:
    """Lightweight "what are we matching" summary for a series group.

    Powers the reference card on the volume-search page: when the
    reviewer leaves the volume-confirm page to hunt CV for the right volume,
    this carries over the first file's cover + the series name +
    the year span so they can eyeball what they're matching against
    while scanning search results.
    """

    series_key: str | None
    file_count: int
    first_file_id: Any  # uuid.UUID | None
    year_min: int | None
    year_max: int | None


async def get_group_reference(db: AsyncSession, *, series_key: str | None) -> GroupReference | None:
    """Summarize one PENDING series group for the volume-search
    reference card.

    Uses the cheap path-only group lookup so the count + year span
    cover *every* file in the group, not just those that survived the
    queue's row cap. Single-group views never honour
    ``PENDING_GROUP_ROW_CAP`` — that cap is a render-cost guard for
    the queue, not a correctness boundary for a specific series.

    Returns None when no PENDING files match the key — the group may
    have drained since the reviewer navigated.
    """
    buckets = await _list_pending_files_by_group(db)
    bucket = buckets.get(series_key)
    if not bucket:
        return None
    years = [f.parsed_year for f in bucket if f.parsed_year is not None]
    return GroupReference(
        series_key=series_key,
        file_count=len(bucket),
        first_file_id=bucket[0].file_id,
        year_min=min(years) if years else None,
        year_max=max(years) if years else None,
    )


# ---- Single-file review ------------------------------------------------


@dataclass
class FileReview:
    """Everything the per-file review page needs for one file.

    A superset of ``PendingRow``: the single-file page shows more
    than the queue's compact row — full path, page count, archive
    format, the current match status and what it points at. Built
    by ``get_file_review`` for any file that has a ``file_matches``
    row, whatever its status.
    """

    file_id: Any  # uuid.UUID
    path: str
    filename: str
    size_bytes: int | None
    sha256_short: str
    comicinfo_status: str
    page_count: int | None
    archive_format: str | None
    # Coarse single-issue vs collected-edition guess from the page
    # count — ``issue`` / ``collection`` / ``unknown``. Surfaced so
    # the reviewer knows what kind of CV volume to hunt for.
    format_guess: str
    status: str  # MatchStatus value
    source: str  # MatchSource value
    confidence: float | None
    issue_cv_id: int | None
    candidates: list[CandidateRow]
    # The candidate (if any) the file is currently matched to — the
    # one whose ``issue_cv_id`` equals the persisted ``FileMatch``
    # value. None for PENDING / UNMATCHED / REJECTED files. For a
    # manual match that isn't in the matcher's shortlist this is
    # synthesised straight from the cached issue row.
    current_match: CandidateRow | None
    parsed_series: str | None
    parsed_long_series: str | None
    parsed_issue_number: str | None
    parsed_year: int | None
    parsed_volume_year: int | None


async def get_file_review(db: AsyncSession, file_id: Any) -> FileReview | None:
    """Load one file's full review detail.

    Returns None when the file has no ``file_matches`` row — the
    matcher hasn't touched it, or the id is bogus. Works for a file
    in any match status, not just PENDING, so the page can also be
    used to re-review an AUTO/CONFIRMED match.
    """
    fm = await db.get(FileMatch, file_id)
    if fm is None:
        return None
    file = await db.get(File, file_id)

    # Current on-disk location — prefer a present (non-missing) row;
    # fall back to any location so the page still renders something
    # identifying for a file that's gone missing.
    present = (
        select(FileLocation.path)
        .where(FileLocation.file_id == file_id)
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.last_seen_at.desc())
        .limit(1)
    )
    path = (await db.execute(present)).scalar_one_or_none()
    if path is None:
        any_loc = (
            select(FileLocation.path)
            .where(FileLocation.file_id == file_id)
            .order_by(FileLocation.last_seen_at.desc())
            .limit(1)
        )
        path = (await db.execute(any_loc)).scalar_one_or_none()
    path = path or "(no current location)"
    filename = path.rsplit("/", 1)[-1] if "/" in path else path

    # Enrich candidates — batch-load issues + volumes, same pattern
    # as ``list_pending_matches``. The persisted ``issue_cv_id`` is
    # folded into the issue set so a manual match that sits outside
    # the candidate shortlist still resolves a cover + names.
    candidates_blob = fm.candidates or []
    issue_ids: set[int] = set()
    volume_ids: set[int] = set()
    for c in candidates_blob:
        cid = c.get("issue_cv_id")
        if isinstance(cid, int):
            issue_ids.add(cid)
        vid = c.get("volume_cv_id")
        if isinstance(vid, int):
            volume_ids.add(vid)
    if isinstance(fm.issue_cv_id, int):
        issue_ids.add(fm.issue_cv_id)

    issue_by_id: dict[int, CvIssue] = {}
    if issue_ids:
        for issue in (
            await db.execute(select(CvIssue).where(CvIssue.cv_id.in_(issue_ids)))
        ).scalars():
            issue_by_id[issue.cv_id] = issue
    # Pull in each cached issue's parent volume too — the matched
    # issue's volume may not be among the candidates' volumes.
    for issue in issue_by_id.values():
        if isinstance(issue.volume_cv_id, int):
            volume_ids.add(issue.volume_cv_id)
    volume_by_id: dict[int, CvVolume] = {}
    if volume_ids:
        for volume in (
            await db.execute(select(CvVolume).where(CvVolume.cv_id.in_(volume_ids)))
        ).scalars():
            volume_by_id[volume.cv_id] = volume

    candidates = [
        built
        for c in candidates_blob
        if (built := _build_candidate(c, issue_by_id, volume_by_id)) is not None
    ]

    current_match: CandidateRow | None = None
    if isinstance(fm.issue_cv_id, int):
        current_match = next((c for c in candidates if c.issue_cv_id == fm.issue_cv_id), None)
        if current_match is None:
            current_match = _candidate_from_issue(fm.issue_cv_id, issue_by_id, volume_by_id)

    parsed = parse_filename(filename)
    return FileReview(
        file_id=file_id,
        path=path,
        filename=filename,
        size_bytes=file.size_bytes if file else None,
        sha256_short=(file.sha256 if file else "")[:12],
        comicinfo_status=((file.comicinfo_status if file else None) or ComicInfoStatus.NONE.value),
        page_count=file.page_count if file else None,
        archive_format=file.archive_format if file else None,
        format_guess=classify_file_format(file.page_count if file else None),
        status=fm.status,
        source=fm.source,
        confidence=float(fm.confidence) if fm.confidence is not None else None,
        issue_cv_id=fm.issue_cv_id,
        candidates=candidates,
        current_match=current_match,
        parsed_series=parsed.series,
        parsed_long_series=parsed.long_series,
        parsed_issue_number=parsed.issue_number,
        parsed_year=parsed.year,
        parsed_volume_year=parsed.volume_year,
    )


@dataclass
class IssueOption:
    """One issue in the single-file issue picker.

    The picker lists every issue of a volume the reviewer chose via
    the manual volume search; ``is_suggested`` flags the issue whose
    number the matcher's loose comparison ties to the file's parsed
    number, so the page can highlight the likely pick.
    """

    issue_cv_id: int
    issue_number: str | None
    name: str | None
    cover_date: str | None
    cover_url: str | None
    is_suggested: bool


async def list_volume_issues(
    db: AsyncSession,
    volume_cv_id: int,
    *,
    suggested_number: str | None = None,
) -> list[IssueOption]:
    """List a cached volume's issues for the single-file issue
    picker, natural-sorted by issue number.

    The volume must already be hydrated — the route fetches it
    through the CV cache first, which writes the issue rows. When
    ``suggested_number`` is given, the issue the matcher's loose
    number comparison ties to it is flagged ``is_suggested``.
    """
    issues = (
        (await db.execute(select(CvIssue).where(CvIssue.volume_cv_id == volume_cv_id)))
        .scalars()
        .all()
    )

    volume = await db.get(CvVolume, volume_cv_id)
    volume_cover = cv_image_url(volume.raw_payload, "thumb") if volume is not None else None

    suggested_cv_id: int | None = None
    if suggested_number is not None:
        matched = await find_issue_by_number(db, volume_cv_id, suggested_number)
        if matched is not None:
            suggested_cv_id = matched.cv_id

    options = [
        IssueOption(
            issue_cv_id=issue.cv_id,
            issue_number=issue.issue_number,
            name=issue.name,
            cover_date=(issue.cover_date.isoformat() if issue.cover_date else None),
            cover_url=cv_image_url(issue.raw_payload, "thumb") or volume_cover,
            is_suggested=issue.cv_id == suggested_cv_id,
        )
        for issue in issues
    ]
    options.sort(key=lambda o: sort_key_issue_number(o.issue_number))
    return options


async def confirm_file_match(
    db: AsyncSession,
    *,
    file_id: Any,
    issue_cv_id: int,
    matched_by_user_id: Any,  # uuid.UUID
) -> bool:
    """Confirm a single file to ``issue_cv_id``.

    Sets status → CONFIRMED, source → MANUAL, confidence → 1.0 (a
    human pick supersedes the matcher's heuristic score),
    ``matched_by`` → the acting admin. The caller must ensure
    ``issue_cv_id`` exists in ``cv_issues`` first (the FileMatch
    FK) — the review route hydrates it through the CV cache.
    Returns False when the file has no ``file_matches`` row.
    """
    fm = await db.get(FileMatch, file_id)
    if fm is None:
        return False
    fm.issue_cv_id = issue_cv_id
    fm.status = MatchStatus.CONFIRMED.value
    fm.source = MatchSource.MANUAL.value
    fm.confidence = 1.0
    fm.matched_by = matched_by_user_id
    await db.commit()
    return True


async def reject_file_match(
    db: AsyncSession,
    *,
    file_id: Any,
    matched_by_user_id: Any,  # uuid.UUID
) -> bool:
    """Reject every candidate for a file.

    Sets status → REJECTED and clears ``issue_cv_id`` — the file
    leaves the PENDING queue but isn't confirmed to anything.
    ``candidates`` is left intact so a later re-review still has
    the matcher's shortlist to work from. Returns False when the
    file has no ``file_matches`` row.
    """
    fm = await db.get(FileMatch, file_id)
    if fm is None:
        return False
    fm.issue_cv_id = None
    fm.status = MatchStatus.REJECTED.value
    fm.matched_by = matched_by_user_id
    await db.commit()
    return True


@dataclass
class VolumeConfirmItem:
    """One row in a volume-confirm preview.

    Represents a file in the series-group and what would happen to
    it if the user commits the chosen volume: either a matched
    issue (``will_confirm=True``) or a skip with a reason
    (``will_confirm=False``)."""

    file_id: Any  # uuid.UUID
    filename: str
    parsed_issue_number: str | None
    parsed_year: int | None
    current_status: str  # MatchStatus value of the file's existing FileMatch row
    matched_issue_cv_id: int | None
    matched_issue_number: str | None
    matched_issue_name: str | None
    matched_issue_cover_url: str | None
    will_confirm: bool
    skip_reason: str | None  # human-readable; ``None`` when will_confirm is True


@dataclass
class VolumeConfirmPreview:
    """Result of previewing a volume-confirm commit.

    Captures everything the preview template needs to render the
    file-by-file mapping plus the summary banner. Also drives the
    actual write path — ``execute_volume_confirm`` calls
    ``preview_volume_confirm`` to compute the same set of items and
    then walks them, so the preview is always behaviorally
    equivalent to what the commit will do."""

    series_key: str | None
    volume_cv_id: int
    volume_name: str | None
    volume_year: int | None
    volume_cover_url: str | None
    # The chosen volume's format — ongoing / limited / one_shot /
    # collection (see ``classify_volume_format``).
    volume_format: str | None
    # The chosen volume's ComicVine description (wiki HTML) for the
    # hover popover on the volume-confirm page's selected-volume card.
    volume_description: str | None
    # The chosen volume's publisher name.
    volume_publisher: str | None
    items: list[VolumeConfirmItem]
    confirm_count: int
    skip_count: int
    # Every CV volume the matcher considered across this group's
    # files, popularity-sorted. Drives the volume-confirm page's
    # volume picker so the reviewer can confirm against a different
    # candidate without going back to the queue. The currently
    # previewed volume (``volume_cv_id``) is normally the first
    # entry but isn't guaranteed to appear at all — a hand-typed
    # ``?volume=`` can target a volume no file listed.
    volume_options: list[VolumeSuggestion]


@dataclass
class VolumeConfirmResult:
    """Outcome of executing a volume-confirm.

    Drives the post-redirect banner: "Confirmed N files to <volume>;
    M files skipped (no matching issue number)." Counts are computed
    from ``VolumeConfirmPreview.items`` after the write, so they
    reflect what actually landed rather than what was previewed —
    the two can differ if a file's status changed between the
    preview load and the commit POST."""

    confirmed_count: int
    skipped_count: int


async def preview_volume_confirm(
    db: AsyncSession,
    *,
    series_key: str | None,
    volume_cv_id: int,
) -> VolumeConfirmPreview | None:
    """Build the preview: every PENDING file in the series group,
    paired with the issue in ``volume_cv_id`` whose issue_number
    matches its parsed number.

    Returns ``None`` when the volume isn't in our cache (we can't
    enumerate its issues to map against). Files whose parsed
    issue_number has no match in the volume become skip items with
    a reason; they stay PENDING on commit.

    The grouping logic is single-sourced with the queue page — both
    use ``_list_pending_files_by_group`` and the same ``_group_key``
    logic — but single-group views never honour ``PENDING_GROUP_ROW_CAP``.
    That cap is a render-cost guard for the queue; this page is one
    group, so every file in it gets enriched.
    """
    volume = await db.get(CvVolume, volume_cv_id)
    if volume is None:
        return None

    volume_cover_url = cv_image_url(volume.raw_payload, "thumb")

    # Cheap pass to find every file in this group, then enrich just
    # those file_ids — bypasses the queue's row cap so a confirm
    # button on the volume-confirm page sees the full set.
    buckets = await _list_pending_files_by_group(db)
    bucket = buckets.get(series_key) or []
    group_file_ids = [f.file_id for f in bucket]
    if group_file_ids:
        group_rows, _total = await list_pending_matches(
            db,
            file_ids=group_file_ids,
        )
    else:
        group_rows = []

    # The candidate volumes for this group — same set the queue card
    # surfaces — so the volume-confirm page can render a picker letting
    # the reviewer switch the confirm target without round-tripping
    # through /review.
    volume_options = await _aggregate_volume_suggestions(db, group_rows)

    # Resolve each file's matched issue in one pass. The matcher's
    # ``find_issue_by_number`` already handles the leading-zero +
    # case normalisation we want, so we don't second-guess the
    # comparison here.
    items: list[VolumeConfirmItem] = []
    for r in group_rows:
        skip_reason: str | None = None
        matched_issue_cv_id: int | None = None
        matched_issue_number: str | None = None
        matched_issue_name: str | None = None
        matched_issue_cover_url: str | None = None

        if r.parsed_issue_number is None:
            skip_reason = "no issue number parsed from filename"
        else:
            matched = await find_issue_by_number(db, volume_cv_id, r.parsed_issue_number)
            if matched is None:
                skip_reason = f"volume has no issue #{r.parsed_issue_number}"
            else:
                matched_issue_cv_id = matched.cv_id
                matched_issue_number = matched.issue_number
                matched_issue_name = matched.name
                # The matched issue's own cover — deliberately with NO
                # volume-cover fallback. Every file on this page maps
                # into the *same* volume, so falling back to the volume
                # cover paints every row with one identical thumbnail,
                # which reads as "all the covers are broken / it's the
                # same issue". A not-yet-hydrated stub issue gets
                # ``None`` instead; the template renders a neutral
                # placeholder, and the real per-issue cover fills in
                # once the volume's bulk issue-hydration job
                # (``hydrate_volume_issues``) runs.
                matched_issue_cover_url = cv_image_url(matched.raw_payload, "thumb")

        items.append(
            VolumeConfirmItem(
                file_id=r.file_id,
                filename=r.filename,
                parsed_issue_number=r.parsed_issue_number,
                parsed_year=r.parsed_year,
                current_status=r.status,
                matched_issue_cv_id=matched_issue_cv_id,
                matched_issue_number=matched_issue_number,
                matched_issue_name=matched_issue_name,
                matched_issue_cover_url=matched_issue_cover_url,
                will_confirm=matched_issue_cv_id is not None,
                skip_reason=skip_reason,
            )
        )

    # Show the file list in natural issue-number order (parsed from the
    # filename), matching the review queue — not the matcher's
    # confidence order. ``items[0]`` also drives the page's header
    # cover, so this makes that the first issue. Order doesn't affect
    # the commit: each file's confirm is independent, and the counts
    # below are order-agnostic.
    items.sort(key=lambda i: sort_key_issue_number(i.parsed_issue_number))

    confirm_count = sum(1 for i in items if i.will_confirm)
    skip_count = len(items) - confirm_count

    return VolumeConfirmPreview(
        series_key=series_key,
        volume_cv_id=volume_cv_id,
        volume_name=volume.name,
        volume_year=volume.year,
        volume_cover_url=volume_cover_url,
        volume_format=classify_cv_volume(volume),
        volume_description=(volume.raw_payload or {}).get("description"),
        volume_publisher=_volume_publisher(volume),
        items=items,
        confirm_count=confirm_count,
        skip_count=skip_count,
        volume_options=volume_options,
    )


async def execute_volume_confirm(
    db: AsyncSession,
    *,
    series_key: str | None,
    volume_cv_id: int,
    matched_by_user_id: Any,  # uuid.UUID
    included_file_ids: set | None = None,  # set[uuid.UUID] | None
) -> VolumeConfirmResult | None:
    """Commit one series-group against a chosen volume.

    Walks the same preview as ``preview_volume_confirm`` and rewrites
    the ``file_matches`` rows for every file with a successful
    issue match: status → ``CONFIRMED``, source → ``MANUAL``,
    confidence → ``1.0`` (the human's volume pick supersedes the
    matcher's heuristic score), ``matched_by`` → the acting admin's
    user id. Files without a matching issue stay PENDING.

    ``included_file_ids`` optionally restricts the commit to a
    subset of the group's files. The volume-confirm page's per-row checkboxes
    pass the still-checked file ids, so a reviewer can exclude a
    file the matcher mis-mapped within an otherwise-correct volume.
    When None, every confirmable file is committed (the original
    whole-group behavior). Files excluded this way are counted as
    skipped and stay PENDING.

    Returns ``None`` when the volume isn't in cache (same path as
    preview's None result). On success returns the actual counts —
    these can differ from the preview's counts if a row's status
    changed between preview load and POST (e.g., someone else
    confirmed in another tab); the executor only writes rows that
    are still PENDING."""
    preview = await preview_volume_confirm(
        db,
        series_key=series_key,
        volume_cv_id=volume_cv_id,
    )
    if preview is None:
        return None

    confirmed = 0
    skipped = 0
    for item in preview.items:
        if not item.will_confirm or item.matched_issue_cv_id is None:
            skipped += 1
            continue
        # Reviewer unchecked this row on the volume-confirm page — exclude it
        # from the commit, leave it PENDING for individual review.
        if included_file_ids is not None and item.file_id not in included_file_ids:
            skipped += 1
            continue
        existing = await db.get(FileMatch, item.file_id)
        if existing is None:
            # Should be rare — the file was in the PENDING list a
            # moment ago. If the row vanished between preview and
            # commit (file deleted, race with another writer),
            # silently skip.
            skipped += 1
            continue
        # Defensive: only commit a row that's still in a reviewable
        # state (PENDING or UNMATCHED). Someone may have confirmed it
        # in another tab, or the matcher re-ran and bumped it to AUTO
        # — don't clobber that.
        if existing.status not in _REVIEWABLE_STATUSES:
            skipped += 1
            continue
        existing.issue_cv_id = item.matched_issue_cv_id
        existing.status = MatchStatus.CONFIRMED.value
        existing.source = MatchSource.MANUAL.value
        existing.confidence = 1.0
        existing.matched_by = matched_by_user_id
        confirmed += 1

    await db.commit()
    return VolumeConfirmResult(confirmed_count=confirmed, skipped_count=skipped)


# ---- Fix match (re-pick a wrong volume from the volume page) ----------
#
# A library-browse-side counterpart of the review-queue's volume-confirm
# flow. Sometimes the matcher confirms a group of files against the
# wrong CV volume — same series name, wrong publisher, etc. — and the
# reviewer notices it on the volume page, not in the queue. The
# fix-match flow lets them pick the correct volume from the volume page
# and re-map every owned file to issues in the new volume by their
# parsed issue number (the same mapping rule volume-confirm uses).


@dataclass
class FixMatchResult:
    """Outcome of ``execute_fix_match`` — drives the redirect banner."""

    rematched_count: int  # files now pointing at the new volume's issues
    skipped_count: int  # files whose number didn't exist in the new volume


async def execute_fix_match(
    db: AsyncSession,
    *,
    old_volume_cv_id: int,
    new_volume_cv_id: int,
    matched_by_user_id: Any,  # uuid.UUID
) -> FixMatchResult | None:
    """Re-map every CV-matched file from ``old_volume_cv_id`` to the
    corresponding issue in ``new_volume_cv_id``.

    For each file whose ``file_matches`` row currently points at an
    issue in the old volume, parse the file's filename to recover its
    issue number, and find the issue in the new volume with that
    number (via ``find_issue_by_number`` — same loose matching the
    matcher uses). When a match exists in the new volume, rewrite the
    ``file_matches`` row to point at it (status → ``CONFIRMED``,
    source → ``MANUAL``, confidence → 1.0). When it doesn't, leave
    the file pointed at its old (wrong) issue — the reviewer can
    follow up per-file from the review path.

    The caller's redirect carries ``rematched_count`` + ``skipped_count``
    so the user sees "Re-matched N files; M had no matching issue in
    the new volume." Returns None when the new volume isn't in cache
    (the caller hydrates it before calling, so this is defensive).
    """
    new_volume = await db.get(CvVolume, new_volume_cv_id)
    if new_volume is None:
        return None

    # Every CV-confirmed file whose current target issue belongs to
    # the old volume. AUTO + CONFIRMED + PENDING all count — a
    # mis-matched file could be in any of those states, and we want
    # to fix all of them.
    stmt = (
        select(FileMatch, File)
        .join(File, File.id == FileMatch.file_id)
        .join(CvIssue, CvIssue.cv_id == FileMatch.issue_cv_id)
        .where(CvIssue.volume_cv_id == old_volume_cv_id)
    )
    rows = (await db.execute(stmt)).all()

    rematched = 0
    skipped = 0
    for fm, file_row in rows:
        # Re-parse the filename to recover the issue number. The
        # one-current-location query mirrors what the matcher and
        # the review pages use.
        loc_stmt = (
            select(FileLocation.path)
            .where(FileLocation.file_id == file_row.id)
            .where(FileLocation.missing_since.is_(None))
            .order_by(FileLocation.last_seen_at.desc())
            .limit(1)
        )
        path = (await db.execute(loc_stmt)).scalar_one_or_none()
        if path is None:
            skipped += 1
            continue
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        parsed = parse_filename(filename)
        if parsed.issue_number is None:
            skipped += 1
            continue

        matched = await find_issue_by_number(db, new_volume_cv_id, parsed.issue_number)
        if matched is None:
            skipped += 1
            continue

        fm.issue_cv_id = matched.cv_id
        fm.status = MatchStatus.CONFIRMED.value
        fm.source = MatchSource.MANUAL.value
        fm.confidence = 1.0
        fm.matched_by = matched_by_user_id
        rematched += 1

    await db.commit()
    return FixMatchResult(
        rematched_count=rematched,
        skipped_count=skipped,
    )


@dataclass
class BulkConfirmResult:
    """Outcome of a queue-level bulk confirm spanning several groups.

    The review queue's bulk action confirms multiple series-groups
    at once, each against its own top volume suggestion. Drives the
    post-redirect banner on ``/review`` — "Confirmed N files across
    G groups."
    """

    group_count: int  # groups actually confirmed against a volume
    confirmed_count: int  # total files moved to CONFIRMED
    skipped_count: int  # total files left PENDING (no matching issue)


async def execute_bulk_confirm(
    db: AsyncSession,
    *,
    series_keys: list[str],
    matched_by_user_id: Any,  # uuid.UUID
) -> BulkConfirmResult:
    """Confirm several series-groups in one action — the review
    queue's bulk control.

    Each requested group is committed against its #1 aggregated
    volume suggestion — the volume the most files in the group agree
    on — exactly as ``execute_volume_confirm`` would for that group
    with no per-file exclusions. A group with no candidate volume,
    or a key that no longer matches a pending group, is silently
    skipped: the queue may have moved since it was rendered.

    ``series_keys`` are the group keys as the queue form sends them
    — the parsed series string, or ``""`` for the unparsed bucket.
    Duplicate keys are de-duplicated so a doubled checkbox can't
    double-count.
    """
    groups, _total, _capped = await list_pending_groups(db)
    by_form_key = {(g.series_key or ""): g for g in groups}

    group_count = 0
    confirmed = 0
    skipped = 0
    seen: set[str] = set()
    for key in series_keys:
        if key in seen:
            continue
        seen.add(key)
        group = by_form_key.get(key)
        if group is None or not group.top_volume_suggestions:
            continue
        top = group.top_volume_suggestions[0]
        result = await execute_volume_confirm(
            db,
            series_key=group.series_key,
            volume_cv_id=top.volume_cv_id,
            matched_by_user_id=matched_by_user_id,
        )
        if result is None:
            continue
        group_count += 1
        confirmed += result.confirmed_count
        skipped += result.skipped_count

    return BulkConfirmResult(
        group_count=group_count,
        confirmed_count=confirmed,
        skipped_count=skipped,
    )


async def _aggregate_volume_suggestions(
    db: AsyncSession, group_rows: list[PendingRow]
) -> list[VolumeSuggestion]:
    """Tally the distinct CV volumes across a series-group's candidate
    lists, enrich with cover URLs, and return them sorted by
    popularity.

    Walks EVERY persisted candidate for EVERY file in the group, not
    just each file's top candidate — the volume the whole group
    belongs to often isn't any single file's #1 pick (that's exactly
    why these rows landed in PENDING). ``count_in_group`` is how many
    candidate entries pointed at the volume; ties in the sort break
    on ``volume_year`` desc so newer volumes win.

    Shared by ``list_pending_groups`` (queue group cards) and
    ``preview_volume_confirm`` (the volume-confirm page's volume picker) so the
    two surfaces always offer the identical candidate set.
    """
    per_volume: dict[int, VolumeSuggestion] = {}
    for r in group_rows:
        for cand in await _candidates_for_row(db, r.file_id):
            vid = cand.get("volume_cv_id")
            if not isinstance(vid, int):
                continue
            if vid not in per_volume:
                per_volume[vid] = VolumeSuggestion(
                    volume_cv_id=vid,
                    volume_name=cand.get("volume_name"),
                    volume_year=cand.get("volume_year"),
                    count_in_group=1,
                )
            else:
                per_volume[vid].count_in_group += 1

    suggestions = list(per_volume.values())
    if suggestions:
        vol_stmt = select(CvVolume).where(CvVolume.cv_id.in_([s.volume_cv_id for s in suggestions]))
        volume_by_id = {v.cv_id: v for v in (await db.execute(vol_stmt)).scalars()}
        for s in suggestions:
            volume = volume_by_id.get(s.volume_cv_id)
            if volume is not None:
                s.cover_url = cv_image_url(volume.raw_payload, "thumb")
                s.format = classify_cv_volume(volume)
                s.description = (volume.raw_payload or {}).get("description")
                s.publisher = _volume_publisher(volume)

    suggestions.sort(
        key=lambda s: (s.count_in_group, s.volume_year or 0),
        reverse=True,
    )
    return suggestions


async def _candidates_for_row(db: AsyncSession, file_id: Any) -> list[dict]:
    """Re-fetch the candidates JSON for a single file row.

    ``list_pending_matches`` already pulled the candidates for the
    rows it returns, but the in-memory ``PendingRow`` only carries
    the top one. For grouping we want the full top-5 list per file
    so the confirm-target aggregation sees every volume the matcher
    considered. One extra cheap SELECT per file is fine at the
    typical PENDING scale; if it ever bites, lift this into the
    primary query."""
    stmt = select(FileMatch.candidates).where(FileMatch.file_id == file_id)
    raw = (await db.execute(stmt)).scalar_one_or_none()
    return raw or []


# ---- Helpers -----------------------------------------------------------


def _volume_publisher(volume: CvVolume) -> str | None:
    """Publisher name from a cached volume's ``raw_payload``.

    CV's volume payload carries a nested ``publisher`` dict, so we
    read the name straight from there rather than joining
    ``cv_publishers``. ``None`` for a stub volume (no payload
    publisher)."""
    pub = (volume.raw_payload or {}).get("publisher")
    return pub.get("name") if isinstance(pub, dict) else None


def _build_candidate(
    blob: dict,
    issue_by_id: dict[int, CvIssue],
    volume_by_id: dict[int, CvVolume],
) -> CandidateRow | None:
    """Turn one persisted candidate dict into a ``CandidateRow``,
    enriching with cached ``cv_issues`` / ``cv_volumes`` data when
    available.

    Cover URL falls through three tiers:
      1. Issue cover (preferred) — populated only after a per-issue
         ``/issue/{id}/`` fetch, which the matcher hasn't triggered
         on its own. So this tier rarely lands during the initial
         review pass.
      2. Volume cover (fallback) — CV's ``/volume/{id}/`` response
         always carries an ``image`` field, and the matcher always
         fetches the parent volume before scoring its issues. So
         this tier is the one that actually fills the queue's
         thumbnails for fresh PENDING rows.
      3. None — neither row is in the cache or both lack image data.
         Template renders the "no cover" placeholder.

    The matcher persists enough display data (volume_name,
    volume_year, issue_number, confidence) for a cover-less render
    on its own; the lookup fields are pure enhancement."""
    cv_id = blob.get("issue_cv_id")
    if not isinstance(cv_id, int):
        return None

    cover_url: str | None = None
    issue_name: str | None = None
    issue = issue_by_id.get(cv_id)
    if issue is not None:
        cover_url = cv_image_url(issue.raw_payload, "thumb")
        issue_name = issue.name

    # Resolve the parent volume — used both as a cover fallback and
    # to classify the candidate's format.
    volume = None
    vol_id = blob.get("volume_cv_id")
    if isinstance(vol_id, int):
        volume = volume_by_id.get(vol_id)
    if cover_url is None and volume is not None:
        cover_url = cv_image_url(volume.raw_payload, "thumb")

    return CandidateRow(
        issue_cv_id=cv_id,
        volume_cv_id=blob.get("volume_cv_id"),
        volume_name=blob.get("volume_name"),
        volume_year=blob.get("volume_year"),
        issue_number=blob.get("issue_number"),
        confidence=float(blob.get("confidence") or 0.0),
        cover_url=cover_url,
        issue_name=issue_name,
        format=classify_cv_volume(volume) if volume is not None else None,
        volume_description=(
            (volume.raw_payload or {}).get("description") if volume is not None else None
        ),
        volume_publisher=(_volume_publisher(volume) if volume is not None else None),
    )


def _candidate_from_issue(
    issue_cv_id: int,
    issue_by_id: dict[int, CvIssue],
    volume_by_id: dict[int, CvVolume],
) -> CandidateRow | None:
    """Build a ``CandidateRow`` straight from a cached ``cv_issues``
    row, for a file's current match that isn't one of the matcher's
    persisted candidates (e.g. a volume/issue picked by hand).

    ``confidence`` is 0.0 — there's no matcher score for a manual
    pick. Cover falls back issue → volume, same as ``_build_candidate``.
    Returns None when the issue isn't in our cache."""
    issue = issue_by_id.get(issue_cv_id)
    if issue is None:
        return None
    volume = volume_by_id.get(issue.volume_cv_id) if isinstance(issue.volume_cv_id, int) else None
    cover_url = cv_image_url(issue.raw_payload, "thumb")
    if cover_url is None and volume is not None:
        cover_url = cv_image_url(volume.raw_payload, "thumb")
    return CandidateRow(
        issue_cv_id=issue.cv_id,
        volume_cv_id=issue.volume_cv_id,
        volume_name=volume.name if volume is not None else None,
        volume_year=volume.year if volume is not None else None,
        issue_number=issue.issue_number,
        confidence=0.0,
        cover_url=cover_url,
        issue_name=issue.name,
        format=classify_cv_volume(volume) if volume is not None else None,
        volume_description=(
            (volume.raw_payload or {}).get("description") if volume is not None else None
        ),
        volume_publisher=(_volume_publisher(volume) if volume is not None else None),
    )


# ---- Bulk exclude-from-matching ---------------------------------------


async def exclude_files_by_series(db: AsyncSession, *, series_key: str | None) -> int:
    """Flip ``excluded_from_matching = True`` on every reviewable
    file whose parsed series-group key matches ``series_key``.

    Mirrors the "Create a local volume" bulk path: works against the
    same group key the volume-confirm / volume-search pages use, and
    against the same reviewable statuses (PENDING / UNMATCHED /
    REJECTED — the ``_REVIEWABLE_STATUSES`` set). Returns the count
    flipped, so the route can surface it in the redirect banner.

    Files already matched (AUTO / CONFIRMED / LOCAL / SUPPLEMENT) are
    skipped — those decisions stand. ``excluded_from_matching`` only
    affects future matcher runs (the matcher's guard at the top of
    ``match_file_job`` short-circuits to UNMATCHED for excluded
    files), so this is the "I don't want the matcher touching this
    series again" lever.

    Deliberately one-way at the service level — the route layer
    above has no "un-exclude" counterpart. A mistakenly-excluded
    file can still be flipped back via direct DB edit or the admin
    duplicates page's per-file un-exclude flow if/when added; this
    matches the user's "exclude is one-way" preference.
    """
    # Discover the file_ids in this group using the same cheap
    # path-only bucketing the local-group preview uses. Re-deriving
    # the group from the live queue (rather than trusting a list of
    # file_ids in the request body) means a confirm that raced the
    # reviewer doesn't get clobbered — only files STILL reviewable
    # at this instant get the flag flipped.
    buckets = await _list_pending_files_by_group(db)
    bucket = buckets.get(series_key) or []
    if not bucket:
        return 0
    file_ids = [f.file_id for f in bucket]

    result = await db.execute(
        update(File).where(File.id.in_(file_ids)).values(excluded_from_matching=True)
    )
    await db.commit()
    return result.rowcount or 0


async def exclude_single_file(
    db: AsyncSession,
    *,
    file_id,  # uuid.UUID
) -> bool:
    """Per-file version of the same lever — flip one file's
    ``excluded_from_matching`` flag. Returns False when the file
    doesn't exist (race with a Phase-2 missing-file mark or a
    manual delete)."""
    file_row = await db.get(File, file_id)
    if file_row is None:
        return False
    file_row.excluded_from_matching = True
    await db.commit()
    return True
