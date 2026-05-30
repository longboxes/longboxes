"""Phase 11 — services for user-authored local library entities.

Local volumes/issues are the escape hatch for comics with no ComicVine
record (see ``design/phase-11-local-metadata.md``). This module owns the
11B write path: turning an ``UNMATCHED`` file into a confirmed ``LOCAL``
entry — a hand-entered ``local_issues`` row under a ``local_volumes``
row, with the file's ``file_matches`` row rewritten to point at it. It
also owns the 11F supplement path: attaching a non-issue file (a cover
gallery, etc.) straight to a real CV volume as a ``SUPPLEMENT``.

Kept separate from ``app.services.review`` (the CV-match review queue)
because local content is a *parallel* of the CV cache, not part of it —
permanent, user-authored, never revalidated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    FileLocation,
    FileMatch,
    LocalIssue,
    LocalVolume,
    MatchSource,
    MatchStatus,
)
from app.services.cv_helpers import sort_key_issue_number
from app.services.reader import progress_bars_by_file

# The review-queue grouping is single-sourced: 11D's bulk create reuses
# the queue's own grouping helpers so the group it commits is exactly
# the bucket the reviewer was looking at (the same approach
# ``preview_volume_confirm`` / ``get_group_reference`` take). The import
# is one-directional — ``review`` never imports ``local`` — so there's
# no cycle.
from app.services.review import (
    _REVIEWABLE_STATUSES,
    _list_pending_files_by_group,
)


async def list_local_volumes(db: AsyncSession) -> list[dict]:
    """Every local volume, as plain dicts for the find-or-create picker.

    Returned as dicts (not a dataclass) because the create-local form
    serialises this straight to JSON for a client-side Alpine filter —
    the whole list is small (a personal library has at most dozens of
    local series), so it's preloaded into the page rather than searched
    server-side. Ordered by name so the picker list reads naturally.
    """
    rows = (
        await db.execute(select(LocalVolume).order_by(LocalVolume.name))
    ).scalars()
    return [
        {
            "id": str(v.id),
            "name": v.name,
            "year": v.year,
            "publisher_name": v.publisher_name,
            "description": v.description,
        }
        for v in rows
    ]


@dataclass
class LocalEntryResult:
    """Outcome of ``create_local_entry`` — what the redirect needs."""

    local_volume_id: uuid.UUID
    local_issue_id: uuid.UUID
    volume_name: str
    created_volume: bool  # True if a fresh local_volumes row was made


async def create_local_entry(
    db: AsyncSession,
    *,
    file_id: Any,  # uuid.UUID
    existing_volume_id: uuid.UUID | None,
    volume_name: str,
    volume_year: int | None,
    publisher_name: str | None,
    volume_description: str | None,
    issue_number: str | None,
    issue_name: str | None,
    created_by: Any,  # uuid.UUID
) -> LocalEntryResult | None:
    """Turn one file into a confirmed local entry.

    Resolve-or-create the ``local_volumes`` row — ``existing_volume_id``
    when the reviewer picked an existing series from the find-or-create
    picker, otherwise a new row from ``volume_name`` / ``volume_year`` /
    ``publisher_name`` / ``volume_description``. The volume-* fields are
    used only on the create path; attaching to an existing volume leaves
    its metadata untouched. Create a ``local_issues`` row under it, then
    rewrite the file's ``file_matches`` row to a ``LOCAL`` resolution:
    ``local_issue_id`` set, the CV-issue and supplement targets cleared
    so the single-target CHECK constraint holds, ``confidence`` NULLed
    (a heuristic score is meaningless for a hand-entered file).

    Returns None when the file has no ``file_matches`` row, or when a
    new volume was asked for but ``volume_name`` is blank.
    """
    fm = await db.get(FileMatch, file_id)
    if fm is None:
        return None

    volume: LocalVolume | None = None
    if existing_volume_id is not None:
        volume = await db.get(LocalVolume, existing_volume_id)

    created_volume = False
    if volume is None:
        # No existing volume picked (or a stale id) — make a new one.
        name = (volume_name or "").strip()
        if not name:
            return None  # nothing to create the volume from
        volume = LocalVolume(
            name=name,
            year=volume_year,
            publisher_name=((publisher_name or "").strip() or None),
            description=((volume_description or "").strip() or None),
            created_by=created_by,
        )
        db.add(volume)
        await db.flush()  # need volume.id for the issue FK
        created_volume = True

    issue = LocalIssue(
        local_volume_id=volume.id,
        issue_number=((issue_number or "").strip() or None),
        name=((issue_name or "").strip() or None),
        created_by=created_by,
    )
    db.add(issue)
    await db.flush()  # need issue.id for the file_matches FK

    # Rewrite file_matches → LOCAL. Clear the other two polymorphic
    # targets so ``ck_file_matches_single_target`` holds; NULL the
    # confidence (no heuristic score for a hand-entered file); record
    # who authored it.
    fm.status = MatchStatus.LOCAL.value
    fm.source = MatchSource.LOCAL.value
    fm.local_issue_id = issue.id
    fm.issue_cv_id = None
    fm.supplement_volume_cv_id = None
    fm.supplement_type = None
    fm.confidence = None
    fm.matched_by = created_by

    await db.commit()
    return LocalEntryResult(
        local_volume_id=volume.id,
        local_issue_id=issue.id,
        volume_name=volume.name,
        created_volume=created_volume,
    )


# ---- 11D: bulk create-from-group --------------------------------------
#
# The local-metadata counterpart of volume-confirm: from a review-queue
# series group whose comic isn't in ComicVine at all, create one local
# volume and a local issue per file in a single action.


@dataclass
class LocalGroupFile:
    """One file in a bulk local-create preview."""

    file_id: Any  # uuid.UUID
    filename: str
    parsed_issue_number: str | None


@dataclass
class LocalGroupPreview:
    """A review-queue series group about to become a local volume —
    what the 11D preview/edit page renders."""

    series_key: str | None
    file_count: int
    # Seeds for the editable volume fields on the form.
    suggested_volume_name: str
    suggested_volume_year: int | None
    files: list[LocalGroupFile]


async def preview_local_group(
    db: AsyncSession, series_key: str | None
) -> LocalGroupPreview | None:
    """Gather one review-queue series group for the bulk-local form.

    Uses the cheap path-only group helper from ``review`` so the
    group covers *every* file in the series, not just those that
    survived the queue's row cap. Single-group views never honour
    that cap — see ``get_group_reference`` for the same rationale.
    Returns None when no reviewable files match the key — the group
    drained since the reviewer navigated."""
    buckets = await _list_pending_files_by_group(db)
    bucket = buckets.get(series_key) or []
    if not bucket:
        return None

    # Suggested volume year: an explicit ``Volume YYYY`` tag if any file
    # carried one, else the earliest issue cover year in the group.
    vol_years = [
        f.parsed_volume_year for f in bucket if f.parsed_volume_year is not None
    ]
    issue_years = [f.parsed_year for f in bucket if f.parsed_year is not None]
    suggested_year = (
        vol_years[0]
        if vol_years
        else (min(issue_years) if issue_years else None)
    )

    return LocalGroupPreview(
        series_key=series_key,
        file_count=len(bucket),
        suggested_volume_name=series_key or "",
        suggested_volume_year=suggested_year,
        files=[
            LocalGroupFile(
                file_id=f.file_id,
                filename=f.filename,
                parsed_issue_number=f.parsed_issue_number,
            )
            for f in bucket
        ],
    )


@dataclass
class LocalGroupResult:
    """Outcome of ``create_local_group`` / ``attach_local_group`` — drives
    the redirect banner."""

    local_volume_id: uuid.UUID
    volume_name: str
    issue_count: int  # files turned into local issues
    skipped_count: int  # files that left a reviewable state since preview


@dataclass
class LocalGroupConflict:
    """One issue-number collision blocking an attach.

    ``existing`` is the colliding number from either an existing
    ``local_issues`` row under the target volume or a duplicate within
    the submitted batch. Surfaced per-file so the form can show the
    error inline next to the row the reviewer needs to fix."""

    file_id: Any  # uuid.UUID — the row in the submitted batch
    issue_number: str
    reason: str  # "existing" or "duplicate"


async def create_local_group(
    db: AsyncSession,
    *,
    series_key: str | None,
    volume_name: str,
    volume_year: int | None,
    publisher_name: str | None,
    volume_description: str | None,
    file_issue_numbers: dict[str, str],
    file_issue_names: dict[str, str],
    created_by: Any,  # uuid.UUID
) -> LocalGroupResult | None:
    """Create one local volume and a local issue per file in the group.

    Builds a single ``local_volumes`` row, then walks the group's files
    — re-derived from the live queue, not the submitted list, so a
    confirm that raced the reviewer isn't clobbered. Each file still in
    a reviewable state gets a ``local_issues`` row and has its
    ``file_matches`` row flipped to ``LOCAL``. Per-file issue numbers and
    titles come from ``file_issue_numbers`` / ``file_issue_names`` (the
    editable form fields, keyed by stringified file id) — the number
    falls back to the parsed number, the title to nothing.

    Returns None when ``volume_name`` is blank or the group has no
    reviewable files left — in the latter case the half-built volume is
    rolled back so no empty ``local_volumes`` row is orphaned."""
    name = (volume_name or "").strip()
    if not name:
        return None

    preview = await preview_local_group(db, series_key)
    if preview is None:
        return None

    volume = LocalVolume(
        name=name,
        year=volume_year,
        publisher_name=((publisher_name or "").strip() or None),
        description=((volume_description or "").strip() or None),
        created_by=created_by,
    )
    db.add(volume)
    await db.flush()  # need volume.id for the issue FKs

    issue_count = 0
    skipped = 0
    for f in preview.files:
        fm = await db.get(FileMatch, f.file_id)
        # Defensive, like execute_volume_confirm: only touch a file
        # that's still awaiting review — it may have been confirmed in
        # another tab between the preview load and this POST.
        if fm is None or fm.status not in _REVIEWABLE_STATUSES:
            skipped += 1
            continue

        raw = file_issue_numbers.get(str(f.file_id))
        number = (raw if raw is not None else f.parsed_issue_number) or ""
        name_raw = file_issue_names.get(str(f.file_id))
        issue = LocalIssue(
            local_volume_id=volume.id,
            issue_number=(number.strip() or None),
            name=((name_raw or "").strip() or None),
            created_by=created_by,
        )
        db.add(issue)
        await db.flush()  # need issue.id for the file_matches FK

        fm.status = MatchStatus.LOCAL.value
        fm.source = MatchSource.LOCAL.value
        fm.local_issue_id = issue.id
        fm.issue_cv_id = None
        fm.supplement_volume_cv_id = None
        fm.supplement_type = None
        fm.confidence = None
        fm.matched_by = created_by
        issue_count += 1

    if issue_count == 0:
        # Every file raced away — discard the empty volume too.
        await db.rollback()
        return None

    await db.commit()
    return LocalGroupResult(
        local_volume_id=volume.id,
        volume_name=volume.name,
        issue_count=issue_count,
        skipped_count=skipped,
    )


async def attach_local_group(
    db: AsyncSession,
    *,
    series_key: str | None,
    target_volume_id: uuid.UUID,
    file_issue_numbers: dict[str, str],
    file_issue_names: dict[str, str],
    created_by: Any,  # uuid.UUID
) -> tuple[LocalGroupResult, list[LocalGroupConflict]] | None:
    """Attach a whole review-queue series group to an existing local
    volume.

    The bulk counterpart of ``create_local_entry``'s ``existing_volume_id``
    branch: instead of building a new ``local_volumes`` row, append a
    ``local_issues`` row per file under ``target_volume_id`` and flip each
    file's ``file_matches`` row to ``LOCAL``. The target volume's
    metadata is never touched — its own edit page owns that.

    Two-pass shape, like ``create_local_group``. First pass: re-derive the
    group from the live queue and validate the submitted issue numbers
    against both the existing rows under the target volume AND each
    other; any collision aborts before a write touches the DB and is
    returned to the caller so the form can re-render with per-row
    errors. Second pass: only when validation cleared, walk the group
    again and write the rows.

    Returns:
        - ``(LocalGroupResult, [])`` on success.
        - ``(zero-row LocalGroupResult, [conflicts])`` when validation
          found collisions — no writes happened.
        - ``None`` when the target volume is gone or the group drained
          between preview and submit.

    A non-empty conflict list is a soft failure: the caller's expected
    response is to re-render the form with the conflicts surfaced inline
    so the reviewer can edit the offending issue numbers and resubmit.
    """
    target = await db.get(LocalVolume, target_volume_id)
    if target is None:
        return None

    preview = await preview_local_group(db, series_key)
    if preview is None:
        return None

    # ---- Pass 1: validate issue numbers before touching the DB.

    # The numbers already on the target volume — case-insensitive set so
    # ``#1`` vs ``#01`` collisions surface, and so a user-entered ``"1"``
    # doesn't sneak past an existing ``"1 "``. Empty/None numbers don't
    # collide with anything (unnumbered issues are allowed to coexist).
    existing_rows = (
        await db.execute(
            select(LocalIssue.issue_number).where(
                LocalIssue.local_volume_id == target_volume_id
            )
        )
    ).scalars()
    existing_keys: set[str] = set()
    for n in existing_rows:
        key = (n or "").strip().lower()
        if key:
            existing_keys.add(key)

    conflicts: list[LocalGroupConflict] = []
    seen_in_batch: dict[str, Any] = {}  # key → first file_id that used it
    # Resolve the per-file submitted number against the parsed fallback,
    # exactly the same way the create path does — so the conflict
    # surface matches what would actually be written.
    resolved: list[tuple[Any, str | None]] = []
    for f in preview.files:
        raw = file_issue_numbers.get(str(f.file_id))
        number = (raw if raw is not None else f.parsed_issue_number) or ""
        number = number.strip()
        resolved.append((f.file_id, number or None))
        if not number:
            continue
        key = number.lower()
        if key in existing_keys:
            conflicts.append(
                LocalGroupConflict(
                    file_id=f.file_id,
                    issue_number=number,
                    reason="existing",
                )
            )
            continue
        if key in seen_in_batch:
            conflicts.append(
                LocalGroupConflict(
                    file_id=f.file_id,
                    issue_number=number,
                    reason="duplicate",
                )
            )
            continue
        seen_in_batch[key] = f.file_id

    if conflicts:
        # Soft failure — no writes. The caller re-renders the form.
        return (
            LocalGroupResult(
                local_volume_id=target_volume_id,
                volume_name=target.name,
                issue_count=0,
                skipped_count=0,
            ),
            conflicts,
        )

    # ---- Pass 2: write. Same race-handling shape as create_local_group.

    issue_count = 0
    skipped = 0
    for f, number in zip(preview.files, [n for _, n in resolved], strict=True):
        fm = await db.get(FileMatch, f.file_id)
        if fm is None or fm.status not in _REVIEWABLE_STATUSES:
            skipped += 1
            continue

        name_raw = file_issue_names.get(str(f.file_id))
        issue = LocalIssue(
            local_volume_id=target_volume_id,
            issue_number=number,
            name=((name_raw or "").strip() or None),
            created_by=created_by,
        )
        db.add(issue)
        await db.flush()  # need issue.id for the file_matches FK

        fm.status = MatchStatus.LOCAL.value
        fm.source = MatchSource.LOCAL.value
        fm.local_issue_id = issue.id
        fm.issue_cv_id = None
        fm.supplement_volume_cv_id = None
        fm.supplement_type = None
        fm.confidence = None
        fm.matched_by = created_by
        issue_count += 1

    if issue_count == 0:
        await db.rollback()
        return None

    await db.commit()
    return (
        LocalGroupResult(
            local_volume_id=target_volume_id,
            volume_name=target.name,
            issue_count=issue_count,
            skipped_count=skipped,
        ),
        [],
    )


# ---- 11C: browse the local library (read path) ------------------------
#
# Local volumes/issues get their own /local/volume/{id} and
# /local/issue/{id} pages — the CV-cache browse routes are int-typed and
# stay untouched. These builders return small dedicated dataclasses (not
# the CV pages' VolumeDetail / IssueDetail): a local entity carries core
# identification metadata only, so the CV templates' arc / credit /
# hydration machinery would have nothing to render — lean dedicated
# templates are simpler and safer than shimming a CvVolume through the
# CV-coupled volume.html.


@dataclass
class LocalIssueRef:
    """A local issue as a list row or a prev/next nav neighbor."""

    id: uuid.UUID
    issue_number: str | None
    name: str | None
    cover_file_id: Any  # file id whose first page is the cover, or None
    # Phase 6 (reader). The viewing user's reading progress for this
    # issue's file, as a ProgressBar — None when unread or built without
    # a user. Set by get_local_volume_detail when user_id is supplied.
    progress: Any = None


@dataclass
class LocalVolumeDetail:
    """The /local/volume/{id} page payload."""

    id: uuid.UUID
    name: str
    year: int | None
    publisher_name: str | None
    description: str | None
    issues: list[LocalIssueRef]

    @property
    def cover_file_id(self) -> Any:
        """Representative cover — the first issue with a matched file."""
        for issue in self.issues:
            if issue.cover_file_id is not None:
                return issue.cover_file_id
        return None


@dataclass
class LocalIssueFileRef:
    """One on-disk file backing a local issue."""

    file_id: Any
    path: str


@dataclass
class LocalIssueDetail:
    """The /local/issue/{id} page payload."""

    id: uuid.UUID
    issue_number: str | None
    name: str | None
    cover_date: date | None
    volume_id: uuid.UUID
    volume_name: str
    volume_year: int | None
    publisher_name: str | None
    files: list[LocalIssueFileRef]
    prev_issue: LocalIssueRef | None
    next_issue: LocalIssueRef | None

    @property
    def cover_file_id(self) -> Any:
        """The issue cover — the first matched file's first page."""
        return self.files[0].file_id if self.files else None


async def _local_issue_cover_files(
    db: AsyncSession, issue_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    """Map ``local_issue_id -> file_id`` for a set of local issues —
    one representative cover file each (first ``LOCAL`` match wins)."""
    if not issue_ids:
        return {}
    rows = (
        await db.execute(
            select(FileMatch.local_issue_id, FileMatch.file_id)
            .where(FileMatch.local_issue_id.in_(issue_ids))
            .where(FileMatch.status == MatchStatus.LOCAL.value)
        )
    ).all()
    covers: dict[uuid.UUID, Any] = {}
    for issue_id, file_id in rows:
        covers.setdefault(issue_id, file_id)
    return covers


async def get_local_volume_detail(
    db: AsyncSession,
    volume_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
) -> LocalVolumeDetail | None:
    """Build the /local/volume page payload, or None if no such volume.

    Issues are ordered by ``sort_key_issue_number`` so a local issue
    list reads identically to a CV volume's. When ``user_id`` is given,
    each issue carries the user's reading progress for the issue grid."""
    volume = await db.get(LocalVolume, volume_id)
    if volume is None:
        return None
    issues = list(
        (
            await db.execute(
                select(LocalIssue).where(
                    LocalIssue.local_volume_id == volume_id
                )
            )
        ).scalars()
    )
    covers = await _local_issue_cover_files(db, [i.id for i in issues])
    issues.sort(key=lambda i: sort_key_issue_number(i.issue_number))
    issue_refs = [
        LocalIssueRef(
            id=i.id,
            issue_number=i.issue_number,
            name=i.name,
            cover_file_id=covers.get(i.id),
        )
        for i in issues
    ]
    # Reading-progress bars for the issue grid — one batch lookup. A
    # local issue's file is its own cover file, so no issue->file hop.
    if user_id is not None:
        bars = await progress_bars_by_file(
            db, user_id, [r.cover_file_id for r in issue_refs]
        )
        for ref in issue_refs:
            ref.progress = bars.get(ref.cover_file_id)
    return LocalVolumeDetail(
        id=volume.id,
        name=volume.name,
        year=volume.year,
        publisher_name=volume.publisher_name,
        description=volume.description,
        issues=issue_refs,
    )


async def get_local_issue_detail(
    db: AsyncSession, issue_id: uuid.UUID
) -> LocalIssueDetail | None:
    """Build the /local/issue page payload, or None if no such issue.

    ``prev_issue`` / ``next_issue`` are the issue-number neighbors
    within the same local volume."""
    issue = await db.get(LocalIssue, issue_id)
    if issue is None:
        return None
    volume = await db.get(LocalVolume, issue.local_volume_id)
    if volume is None:  # the FK guarantees this — stay defensive anyway
        return None

    # Files on disk — one row per non-missing location, newest first,
    # the same shape the CV issue page's "Files on disk" list uses.
    file_rows = (
        await db.execute(
            select(FileMatch.file_id, FileLocation.path)
            .join(FileLocation, FileLocation.file_id == FileMatch.file_id)
            .where(FileMatch.local_issue_id == issue_id)
            .where(FileMatch.status == MatchStatus.LOCAL.value)
            .where(FileLocation.missing_since.is_(None))
            .order_by(FileLocation.last_seen_at.desc())
        )
    ).all()
    files = [
        LocalIssueFileRef(file_id=file_id, path=path)
        for file_id, path in file_rows
    ]

    # Sibling issues for prev/next navigation.
    siblings = list(
        (
            await db.execute(
                select(LocalIssue).where(
                    LocalIssue.local_volume_id == volume.id
                )
            )
        ).scalars()
    )
    siblings.sort(key=lambda i: sort_key_issue_number(i.issue_number))
    sibling_covers = await _local_issue_cover_files(
        db, [s.id for s in siblings]
    )

    def _ref(li: LocalIssue) -> LocalIssueRef:
        return LocalIssueRef(
            id=li.id,
            issue_number=li.issue_number,
            name=li.name,
            cover_file_id=sibling_covers.get(li.id),
        )

    idx = next(
        (n for n, s in enumerate(siblings) if s.id == issue_id), None
    )
    prev_issue = (
        _ref(siblings[idx - 1]) if idx is not None and idx > 0 else None
    )
    next_issue = (
        _ref(siblings[idx + 1])
        if idx is not None and idx < len(siblings) - 1
        else None
    )

    return LocalIssueDetail(
        id=issue.id,
        issue_number=issue.issue_number,
        name=issue.name,
        cover_date=issue.cover_date,
        volume_id=volume.id,
        volume_name=volume.name,
        volume_year=volume.year,
        publisher_name=volume.publisher_name,
        files=files,
        prev_issue=prev_issue,
        next_issue=next_issue,
    )


# ---- 11E: edit local metadata -----------------------------------------
#
# Hand-entered metadata will be wrong sometimes — a typo'd series name, a
# mis-parsed issue number. These update the ``local_volumes`` /
# ``local_issues`` rows in place. They never touch ``file_matches``: a
# file's resolution doesn't change, only the metadata it points at. (The
# merge-two-volumes tool is a separate piece — see the phase doc's 11E.)


async def update_local_volume(
    db: AsyncSession,
    volume_id: uuid.UUID,
    *,
    name: str,
    year: int | None,
    publisher_name: str | None,
    description: str | None,
) -> LocalVolume | None:
    """Update a local volume's hand-entered metadata in place.

    Returns the updated row, or None when no volume has that id. A blank
    ``name`` is rejected by the caller (the route) before this runs;
    ``publisher_name`` / ``description`` normalise empty/whitespace to
    NULL, the same convention ``create_local_*`` use."""
    volume = await db.get(LocalVolume, volume_id)
    if volume is None:
        return None
    volume.name = name.strip()
    volume.year = year
    volume.publisher_name = (publisher_name or "").strip() or None
    volume.description = (description or "").strip() or None
    await db.commit()
    return volume


async def update_local_issue(
    db: AsyncSession,
    issue_id: uuid.UUID,
    *,
    issue_number: str | None,
    name: str | None,
    cover_date: date | None,
) -> LocalIssue | None:
    """Update a local issue's hand-entered metadata in place.

    Returns the updated row, or None when no issue has that id. Empty
    strings normalise to NULL — same convention as ``create_local_*`` —
    so clearing a field in the form clears it on the row."""
    issue = await db.get(LocalIssue, issue_id)
    if issue is None:
        return None
    issue.issue_number = (issue_number or "").strip() or None
    issue.name = (name or "").strip() or None
    issue.cover_date = cover_date
    await db.commit()
    return issue


@dataclass
class MergeResult:
    """Outcome of ``merge_local_volumes`` — what the redirect needs."""

    target_id: uuid.UUID
    target_name: str
    source_name: str
    moved_issue_count: int


async def merge_local_volumes(
    db: AsyncSession,
    *,
    target_id: uuid.UUID,
    source_id: uuid.UUID,
) -> MergeResult | None:
    """Merge the ``source`` local volume into ``target``.

    Every local issue under the source volume is reassigned to the
    target, then the now-empty source volume is deleted. ``file_matches``
    rows are untouched — a local issue keeps its id, only its parent
    volume changes — so the files move with their issues.

    Duplicate issue numbers (both volumes had a "#1") are left to
    coexist; the reviewer can fix them afterward by editing. Returns None
    when either volume is missing, or when the two ids are the same."""
    if target_id == source_id:
        return None
    target = await db.get(LocalVolume, target_id)
    source = await db.get(LocalVolume, source_id)
    if target is None or source is None:
        return None
    target_name = target.name
    source_name = source.name
    moved = int(
        (
            await db.execute(
                select(func.count())
                .select_from(LocalIssue)
                .where(LocalIssue.local_volume_id == source_id)
            )
        ).scalar()
        or 0
    )
    # Reassign the source's issues to the target with a bulk Core UPDATE,
    # then delete the now-empty source volume. ``passive_deletes`` on the
    # LocalVolume.issues relationship means the ORM delete trusts the DB
    # and won't try to async-lazy-load the (already-moved) children.
    await db.execute(
        update(LocalIssue)
        .where(LocalIssue.local_volume_id == source_id)
        .values(local_volume_id=target_id)
        .execution_options(synchronize_session=False)
    )
    await db.delete(source)
    await db.commit()
    return MergeResult(
        target_id=target_id,
        target_name=target_name,
        source_name=source_name,
        moved_issue_count=moved,
    )


# ---- 11F: supplemental content ----------------------------------------
#
# A non-issue file (a cover gallery, a sketch archive) belonging to a
# real ComicVine series. Rather than a local entry, it's attached as a
# *supplement* straight to the CV volume — ``file_matches`` resolves to
# ``supplement_volume_cv_id`` instead of an issue. The volume page
# lists these below its issue run.

# Supplement types — ``(key, label)`` pairs; the wired vocabulary for
# the ``supplement_type`` column (which is itself open-ended). Adding
# a new kind is a single tuple here, no migration. ``bonus_content``
# is the catch-all for things that aren't strictly a cover gallery —
# sketch archives, behind-the-scenes pages, scripts, trade-dress
# variants, the back-matter PDFs that publishers sometimes ship as
# their own archive.
SUPPLEMENT_TYPES: list[tuple[str, str]] = [
    ("cover_gallery", "Cover gallery"),
    ("bonus_content", "Bonus content"),
]
SUPPLEMENT_TYPE_LABELS: dict[str, str] = dict(SUPPLEMENT_TYPES)


@dataclass
class SupplementRef:
    """One supplement file attached to a CV volume — a row in the
    volume page's "Supplements" section."""

    file_id: Any  # uuid.UUID
    filename: str
    supplement_type: str
    type_label: str


async def attach_supplement(
    db: AsyncSession,
    *,
    file_id: Any,  # uuid.UUID
    volume_cv_id: int,
    supplement_type: str,
    attached_by: Any,  # uuid.UUID
) -> bool:
    """Attach a file to a CV volume as a supplement.

    Rewrites the file's ``file_matches`` row to a ``SUPPLEMENT``
    resolution: ``supplement_volume_cv_id`` + ``supplement_type`` set,
    the CV-issue and local-issue targets cleared so the single-target
    CHECK constraint holds, ``confidence`` NULLed (no heuristic score
    for a hand-resolved file), ``source`` MANUAL (attaching a
    supplement *is* a human picking a real CV volume). The caller must
    have hydrated the CV volume first — it's the FK target.

    Returns False when the file has no ``file_matches`` row.
    """
    fm = await db.get(FileMatch, file_id)
    if fm is None:
        return False
    fm.status = MatchStatus.SUPPLEMENT.value
    fm.source = MatchSource.MANUAL.value
    fm.supplement_volume_cv_id = volume_cv_id
    fm.supplement_type = supplement_type
    fm.issue_cv_id = None
    fm.local_issue_id = None
    fm.confidence = None
    fm.matched_by = attached_by
    await db.commit()
    return True


async def list_volume_supplements(
    db: AsyncSession, volume_cv_id: int
) -> list[SupplementRef]:
    """Files attached to a CV volume as supplements — for the volume
    page's "Supplements" section.

    De-duped by file (a file with several on-disk locations contributes
    one row); the filename is the basename of a location path."""
    rows = (
        await db.execute(
            select(
                FileMatch.file_id,
                FileMatch.supplement_type,
                FileLocation.path,
            )
            .join(FileLocation, FileLocation.file_id == FileMatch.file_id)
            .where(
                FileMatch.status == MatchStatus.SUPPLEMENT.value,
                FileMatch.supplement_volume_cv_id == volume_cv_id,
            )
            .order_by(FileLocation.path)
        )
    ).all()
    out: dict[Any, SupplementRef] = {}
    for file_id, stype, path in rows:
        if file_id in out:
            continue
        out[file_id] = SupplementRef(
            file_id=file_id,
            filename=path.rsplit("/", 1)[-1],
            supplement_type=stype or "",
            type_label=SUPPLEMENT_TYPE_LABELS.get(stype or "", "Supplement"),
        )
    return list(out.values())
