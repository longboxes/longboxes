"""Reader-facing services (Phase 6).

Right now this is just the per-volume *reading direction* — the
left-to-right / right-to-left choice the reader's toggle controls.
Direction is a property of the content (manga reads RTL, Western comics
LTR), so it is stored on the volume, not per user: every issue in a
volume shares one setting.

The reader only ever knows a ``file_id``, so the work here is resolving
that file to its owning volume — a file belongs to a CV volume (via its
matched CV issue, or as a supplement attached straight to a volume) or
to a local volume (via its local issue). An unmatched file has no
volume; its direction simply falls back to the default and cannot be
persisted.

It also owns *reading progress* — the per-user page position the reader
saves as you read, plus the home page's "Continue reading" / "Recently
read" lists built from it. Progress is personal, so unlike direction it
is keyed by user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CvIssue,
    CvVolume,
    FileLocation,
    FileMatch,
    LocalIssue,
    LocalVolume,
    MatchStatus,
    ReadProgress,
)

# The two valid reading directions. ``ltr`` is the Western default;
# ``rtl`` is manga / right-to-left.
READING_DIRECTIONS: tuple[str, ...] = ("ltr", "rtl")
DEFAULT_READING_DIRECTION = "ltr"


async def _resolve_volume(
    db: AsyncSession, file_id: uuid.UUID
) -> tuple[str, int | uuid.UUID] | None:
    """The volume that owns ``file_id``, as a ``(kind, key)`` pair.

    ``("cv", cv_id)``      — a ``CvVolume`` (the int primary key).
    ``("local", uuid)``    — a ``LocalVolume`` (the uuid primary key).
    ``None``               — the file is not resolved to any volume
                             (no match row, or a match with no target).
    """
    match = await db.get(FileMatch, file_id)
    if match is None:
        return None
    # A local issue carries its own volume id directly.
    if match.local_issue_id is not None:
        issue = await db.get(LocalIssue, match.local_issue_id)
        if issue is not None:
            return ("local", issue.local_volume_id)
    # A CV-issue match reaches the volume through the issue row.
    if match.issue_cv_id is not None:
        issue = await db.get(CvIssue, match.issue_cv_id)
        if issue is not None and issue.volume_cv_id is not None:
            return ("cv", issue.volume_cv_id)
    # A supplement file is attached straight to a CV volume.
    if match.supplement_volume_cv_id is not None:
        return ("cv", match.supplement_volume_cv_id)
    return None


async def _load_volume(
    db: AsyncSession, resolved: tuple[str, int | uuid.UUID]
) -> CvVolume | LocalVolume | None:
    """Load the volume ORM row for a ``_resolve_volume`` result."""
    kind, key = resolved
    if kind == "cv":
        return await db.get(CvVolume, key)
    return await db.get(LocalVolume, key)


async def get_reading_direction(db: AsyncSession, file_id: uuid.UUID) -> str:
    """The stored reading direction for the file's volume.

    Falls back to :data:`DEFAULT_READING_DIRECTION` when the file has no
    owning volume to carry the setting.
    """
    resolved = await _resolve_volume(db, file_id)
    if resolved is None:
        return DEFAULT_READING_DIRECTION
    volume = await _load_volume(db, resolved)
    if volume is None:
        return DEFAULT_READING_DIRECTION
    return volume.reading_direction or DEFAULT_READING_DIRECTION


async def set_reading_direction(db: AsyncSession, file_id: uuid.UUID, direction: str) -> bool:
    """Persist ``direction`` onto the file's owning volume.

    Returns ``True`` when a volume was updated, ``False`` when the file
    has no volume to store it on — the reader still works, the choice
    just will not stick. Raises :class:`ValueError` for a direction
    outside :data:`READING_DIRECTIONS`.
    """
    if direction not in READING_DIRECTIONS:
        raise ValueError(f"invalid reading direction: {direction!r}")
    resolved = await _resolve_volume(db, file_id)
    if resolved is None:
        return False
    volume = await _load_volume(db, resolved)
    if volume is None:
        return False
    volume.reading_direction = direction
    await db.commit()
    return True


# ---- Reading progress ---------------------------------------------------


# File-match statuses where we DO track reading progress. These are
# the four "resolved" outcomes — each has a reachable detail page
# (CV issue / local issue / volume supplements section) where the
# reset-progress button lives. PENDING / UNMATCHED / REJECTED files
# don't have one, so tracking progress there strands the user without
# a way to clear it. Updated when MatchStatus grows a new "resolved"
# variant.
_TRACKING_STATUSES: frozenset[str] = frozenset(
    {
        MatchStatus.AUTO.value,
        MatchStatus.CONFIRMED.value,
        MatchStatus.LOCAL.value,
        MatchStatus.SUPPLEMENT.value,
    }
)


async def is_file_match_resolved(db: AsyncSession, file_id: uuid.UUID) -> bool:
    """True if the file has a match in one of the
    progress-trackable statuses (``_TRACKING_STATUSES``).

    The gate the save-progress route uses to decide whether to
    persist a position. Returns False for files with no
    ``file_matches`` row at all, and for rows in
    PENDING / UNMATCHED / REJECTED — none of those have a surface
    where the user could reset progress.
    """
    match = await db.get(FileMatch, file_id)
    return match is not None and match.status in _TRACKING_STATUSES


async def get_read_progress(
    db: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID
) -> ReadProgress | None:
    """The user's saved position in ``file_id``, or ``None`` if unread."""
    return await db.get(ReadProgress, (user_id, file_id))


async def save_read_progress(
    db: AsyncSession,
    user_id: uuid.UUID,
    file_id: uuid.UUID,
    page: int,
    page_count: int,
) -> ReadProgress:
    """Upsert the user's reading position in a file.

    ``page`` is a 0-based index, clamped into range. ``finished_at`` is
    stamped the first time the last page is reached and left alone
    afterwards — paging back through a comic you have finished does not
    un-finish it.
    """
    page_count = max(page_count, 0)
    page = min(max(page, 0), page_count - 1) if page_count else max(page, 0)
    now = datetime.now(tz=UTC)

    progress = await db.get(ReadProgress, (user_id, file_id))
    if progress is None:
        progress = ReadProgress(user_id=user_id, file_id=file_id)
        db.add(progress)
    progress.page = page
    progress.page_count = page_count
    progress.updated_at = now
    if progress.finished_at is None and page_count > 0 and page >= page_count - 1:
        progress.finished_at = now
    await db.commit()
    return progress


async def reset_read_progress(db: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID) -> bool:
    """Clear the user's saved reading position for a file.

    Returns ``True`` if a row was removed, ``False`` when there was
    nothing saved to begin with.
    """
    progress = await db.get(ReadProgress, (user_id, file_id))
    if progress is None:
        return False
    await db.delete(progress)
    await db.commit()
    return True


@dataclass
class ReadingProgressCard:
    """One file on the home page's "Continue reading" / "Recently read"
    lists — enough to draw a cover, a label, and a progress bar.

    ``read_url`` is the cover-thumb click target — opens the reader
    at the saved page. ``issue_url`` is the text-label click target
    — opens the issue page (or local-issue page, or supplement
    volume page) so the user can read the description, reset
    reading progress, etc. When the file isn't matched to anything,
    ``issue_url`` falls back to ``read_url`` so the link still goes
    somewhere useful.
    """

    file_id: uuid.UUID
    title: str  # volume name, or a filename fallback
    subtitle: str | None  # "#7 — Issue name", or None
    cover_url: str
    issue_url: str
    page: int  # 0-based
    page_count: int
    finished: bool

    @property
    def read_url(self) -> str:
        return f"/read/{self.file_id}"

    @property
    def percent(self) -> int:
        """How far through the file, 0-100. ``page`` is 0-based, so the
        first page already counts as ``1 / page_count``."""
        if self.page_count <= 0:
            return 100 if self.finished else 0
        return min(100, round(100 * (self.page + 1) / self.page_count))


@dataclass
class ProgressBar:
    """Just enough to draw a thin reading-progress bar over a cover —
    a percentage, whether the file is finished, and a tooltip label."""

    percent: int  # 0-100
    finished: bool
    label: str


def progress_bar(progress: ReadProgress | None) -> ProgressBar | None:
    """A :class:`ProgressBar` for a ``read_progress`` row, or ``None``
    when there is nothing worth showing — no row at all, or a file that
    was opened but never paged past the first page.
    """
    if progress is None:
        return None
    finished = progress.finished_at is not None
    if not finished and progress.page <= 0:
        return None
    if progress.page_count <= 0:
        return ProgressBar(100 if finished else 0, finished, "")
    percent = min(100, round(100 * (progress.page + 1) / progress.page_count))
    label = "Finished" if finished else f"Page {progress.page + 1} of {progress.page_count}"
    return ProgressBar(percent, finished, label)


def _issue_label(number: str | None, name: str | None) -> str | None:
    """A "#7 — Name" style label from an issue number and name."""
    if number and name:
        return f"#{number} — {name}"
    if number:
        return f"#{number}"
    return name or None


async def _file_card_label(
    db: AsyncSession, file_id: uuid.UUID
) -> tuple[str, str | None, str | None]:
    """``(title, subtitle, issue_url)`` for a file's progress card.

    Resolves through the file's match to a CV or local issue; an
    unmatched file falls back to its on-disk filename and a None
    URL — the card builder falls back to the reader URL in that
    case so the text link still goes somewhere useful.

    URL targets, by match shape:
      * CV issue match → ``/issue/{cv_id}`` (the standard issue page)
      * Local issue match → ``/local/issue/{uuid}``
      * Supplement → ``/volume/{cv_id}`` (supplements don't have
        their own page; the volume page lists them in a section)
    """
    match = await db.get(FileMatch, file_id)
    if match is not None:
        if match.local_issue_id is not None:
            issue = await db.get(LocalIssue, match.local_issue_id)
            if issue is not None:
                volume = await db.get(LocalVolume, issue.local_volume_id)
                return (
                    volume.name if volume else "Local issue",
                    _issue_label(issue.issue_number, issue.name),
                    f"/local/issue/{issue.id}",
                )
        if match.issue_cv_id is not None:
            issue = await db.get(CvIssue, match.issue_cv_id)
            if issue is not None:
                volume = (
                    await db.get(CvVolume, issue.volume_cv_id)
                    if issue.volume_cv_id is not None
                    else None
                )
                return (
                    volume.name if volume else "Issue",
                    _issue_label(issue.issue_number, issue.name),
                    f"/issue/{issue.cv_id}",
                )
        if match.supplement_volume_cv_id is not None:
            volume = await db.get(CvVolume, match.supplement_volume_cv_id)
            return (
                volume.name if volume else "Volume",
                "Supplement",
                f"/volume/{match.supplement_volume_cv_id}",
            )

    # Unmatched / unresolved — show the filename so the card is not blank.
    path = (
        await db.execute(select(FileLocation.path).where(FileLocation.file_id == file_id).limit(1))
    ).scalar_one_or_none()
    return (Path(path).name if path else "Unknown file", None, None)


async def _progress_cards(db: AsyncSession, rows: list[ReadProgress]) -> list[ReadingProgressCard]:
    """Build display cards for a set of progress rows."""
    cards: list[ReadingProgressCard] = []
    for row in rows:
        title, subtitle, issue_url = await _file_card_label(db, row.file_id)
        # Fall back to the reader URL when no issue page exists for
        # this file (unmatched) — the text label still becomes a
        # live link rather than dead text.
        if not issue_url:
            issue_url = f"/read/{row.file_id}"
        cards.append(
            ReadingProgressCard(
                file_id=row.file_id,
                title=title,
                subtitle=subtitle,
                cover_url=f"/review/file/{row.file_id}/cover",
                issue_url=issue_url,
                page=row.page,
                page_count=row.page_count,
                finished=row.finished_at is not None,
            )
        )
    return cards


async def list_continue_reading(
    db: AsyncSession, user_id: uuid.UUID, limit: int = 8
) -> list[ReadingProgressCard]:
    """In-progress comics — started, past the first page, not finished —
    most recently read first.

    Skips files without a resolved match — same gate the save-progress
    route uses. An unmatched file has no detail page where the
    reset-progress button lives, so showing it on "Continue reading"
    would strand the user with no way to clear it. A pre-existing
    progress row from before the gate landed is filtered out at query
    time (INNER JOIN to ``file_matches``), no migration needed.
    """
    rows = list(
        (
            await db.execute(
                select(ReadProgress)
                .join(FileMatch, FileMatch.file_id == ReadProgress.file_id)
                .where(ReadProgress.user_id == user_id)
                .where(ReadProgress.finished_at.is_(None))
                .where(ReadProgress.page > 0)
                .where(FileMatch.status.in_(_TRACKING_STATUSES))
                .order_by(ReadProgress.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return await _progress_cards(db, rows)


async def list_recently_read(
    db: AsyncSession, user_id: uuid.UUID, limit: int = 8
) -> list[ReadingProgressCard]:
    """The user's finished comics, most recently finished first.

    Same resolved-match filter as ``list_continue_reading`` — an
    unmatched file can't reach reset-progress and shouldn't surface
    here either.
    """
    rows = list(
        (
            await db.execute(
                select(ReadProgress)
                .join(FileMatch, FileMatch.file_id == ReadProgress.file_id)
                .where(ReadProgress.user_id == user_id)
                .where(ReadProgress.finished_at.is_not(None))
                .where(FileMatch.status.in_(_TRACKING_STATUSES))
                .order_by(ReadProgress.finished_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return await _progress_cards(db, rows)


async def progress_bars_by_file(
    db: AsyncSession, user_id: uuid.UUID, file_ids: list[Any]
) -> dict[Any, ProgressBar]:
    """Map ``file_id -> ProgressBar`` for the user across a set of files.

    Files with no saved progress (or none worth showing) are simply
    absent from the map. Used to decorate issue grids in one batch.
    """
    ids = {f for f in file_ids if f is not None}
    if not ids:
        return {}
    rows = (
        (
            await db.execute(
                select(ReadProgress)
                .where(ReadProgress.user_id == user_id)
                .where(ReadProgress.file_id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    out: dict[Any, ProgressBar] = {}
    for row in rows:
        bar = progress_bar(row)
        if bar is not None:
            out[row.file_id] = bar
    return out


async def issue_progress_for_volume(
    db: AsyncSession, user_id: uuid.UUID, issue_cv_ids: list[int]
) -> dict[int, ProgressBar]:
    """Map ``cv_issue_id -> ProgressBar`` for the user across a set of
    CV issues — each issue resolved to its matched file, then to that
    file's reading progress. Issues with no file or no progress are
    absent from the map.
    """
    if not issue_cv_ids:
        return {}
    rows = (
        await db.execute(
            select(FileMatch.issue_cv_id, FileMatch.file_id)
            .where(FileMatch.issue_cv_id.in_(issue_cv_ids))
            .where(FileMatch.status.in_((MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)))
        )
    ).all()
    # One representative file per issue — first match wins.
    issue_to_file: dict[int, Any] = {}
    for issue_cv_id, file_id in rows:
        issue_to_file.setdefault(issue_cv_id, file_id)
    bars = await progress_bars_by_file(db, user_id, list(issue_to_file.values()))
    return {
        issue_cv_id: bars[file_id]
        for issue_cv_id, file_id in issue_to_file.items()
        if file_id in bars
    }
