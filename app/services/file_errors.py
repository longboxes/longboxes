"""File-error inventory: record / list / dismiss / retry.

Backs the ``/admin/file-errors`` inspector. Three failure modes the
scanner / cover endpoint / ComicInfo parser used to silently swallow
into log lines:

* ``ARCHIVE_OPEN`` — the archive layer raised on open. No ``files``
  row exists, so the path itself is the only handle on the file.
* ``COVER_EXTRACTION`` — the cover endpoint hit its placeholder
  fallback. The ``files`` row is present.
* ``COMICINFO_PARSE`` — ComicInfo.xml present but unparseable.

The table holds *current* failures only. A successful pass on the
same ``(path, kind)`` deletes the row — re-scan / cover re-extract /
ComicInfo re-parse all converge on the same "clear it" hook. Re-
failure UPSERTs the row, bumping ``last_seen_at`` and refreshing the
captured exception class + message; ``first_seen_at`` stays put so
the inspector can tell "always failing" apart from "just started
failing today".
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.archives import open_archive
from app.archives.base import ArchiveError, UnsupportedArchiveError
from app.models import FileError, FileErrorKind
from app.services.settings import get_archive_backend


@dataclass
class FileErrorRow:
    """Display row for ``/admin/file-errors``."""

    id: uuid.UUID
    path: str
    kind: str
    error_class: str | None
    error_message: str | None
    file_id: uuid.UUID | None
    first_seen_at: datetime
    last_seen_at: datetime


# ---- Write helpers ----------------------------------------------------


async def record_error(
    db: AsyncSession,
    *,
    path: str,
    kind: FileErrorKind,
    exc: BaseException,
    file_id: uuid.UUID | None = None,
) -> None:
    """UPSERT one ``(path, kind)`` failure row.

    Caller passes the live exception, not just a string, so we can
    capture the class for grouping on the listing page. ``file_id`` is
    optional — ``ARCHIVE_OPEN`` failures don't have one because the
    file never made it into ``files``.

    Commits on success. Caller must not also commit (the upsert is a
    single-statement transaction we want to land before the surrounding
    try/except continues).
    """
    now = datetime.now(tz=UTC)
    error_class = type(exc).__name__
    error_message = str(exc)[:2000]  # belt + suspenders: avoid runaway messages
    stmt = (
        pg_insert(FileError)
        .values(
            path=path,
            kind=kind.value,
            error_class=error_class,
            error_message=error_message,
            file_id=file_id,
            first_seen_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["path", "kind"],
            set_={
                "error_class": error_class,
                "error_message": error_message,
                # Re-link the FK if the caller now has a files row
                # (a cover-extraction failure recorded after the
                # archive opened cleanly, say).
                "file_id": file_id,
                "last_seen_at": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
    # The raw INSERT...ON CONFLICT DO UPDATE bypasses the ORM, so any
    # FileError already in the session's identity map (e.g., one a
    # caller selected before record_error ran) keeps its stale column
    # values until expired. Same gotcha _upsert_volume / _upsert_issue
    # work around with ``populate_existing=True``. Cheap mark-as-dirty
    # so the next attribute access re-fetches from the DB.
    db.expire_all()


async def clear_errors_for_path(db: AsyncSession, path: str) -> int:
    """Drop every ``file_errors`` row for ``path``, regardless of kind.

    The scanner calls this after a file finishes processing
    successfully — every kind that could have fired for this path has
    just been re-checked and didn't fail. Returns the count deleted so
    callers can log it if useful.
    """
    result = await db.execute(delete(FileError).where(FileError.path == path))
    await db.commit()
    return result.rowcount or 0


async def clear_error_for_path_and_kind(db: AsyncSession, path: str, kind: FileErrorKind) -> int:
    """Drop just the ``(path, kind)`` row.

    Used by out-of-band success paths that don't have the scanner's
    full picture — e.g., the cover endpoint succeeds, so we can clear
    a stale ``COVER_EXTRACTION`` row without claiming the archive
    open + ComicInfo parse also succeeded.
    """
    result = await db.execute(
        delete(FileError).where(FileError.path == path).where(FileError.kind == kind.value)
    )
    await db.commit()
    return result.rowcount or 0


# ---- Read helpers -----------------------------------------------------


async def list_file_errors(db: AsyncSession) -> list[FileErrorRow]:
    """All current file-error rows, most-recent-failure first."""
    stmt = select(FileError).order_by(FileError.last_seen_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        FileErrorRow(
            id=r.id,
            path=r.path,
            kind=r.kind,
            error_class=r.error_class,
            error_message=r.error_message,
            file_id=r.file_id,
            first_seen_at=r.first_seen_at,
            last_seen_at=r.last_seen_at,
        )
        for r in rows
    ]


async def count_file_errors(db: AsyncSession) -> int:
    """Total row count — feeds the admin Health page stat."""
    return (await db.execute(select(func.count()).select_from(FileError))).scalar_one() or 0


# ---- Admin actions ----------------------------------------------------


async def dismiss_file_error(db: AsyncSession, error_id: uuid.UUID) -> bool:
    """Delete one row without retrying. Returns False if it was
    already gone (race with a concurrent scan that cleared it)."""
    result = await db.execute(delete(FileError).where(FileError.id == error_id))
    await db.commit()
    return bool(result.rowcount)


@dataclass
class TryOpenResult:
    """Outcome of an admin-initiated archive-open retry.

    ``ok=True`` means ``open_archive`` succeeded and we cleared the
    row. ``ok=False`` means it raised — we kept the row but refreshed
    its ``last_seen_at`` and exception fields so the listing reflects
    the latest attempt.
    """

    ok: bool
    error_class: str | None = None
    error_message: str | None = None


async def try_open_archive(db: AsyncSession, error_id: uuid.UUID) -> TryOpenResult | None:
    """Run ``open_archive`` against a recorded path and update the row.

    On success the (path, kind) row is deleted (and, if it was an
    ``ARCHIVE_OPEN`` row, that's the natural "this is fixed now"
    moment). On failure the row's exception fields are refreshed via
    ``record_error`` so the listing shows the latest attempt.

    Returns None if the row no longer exists. The archive open is
    pushed to a thread because ``open_archive`` can shell out to
    ``unar`` or block on a large CBZ read.
    """
    row = await db.get(FileError, error_id)
    if row is None:
        return None
    path = row.path
    kind = FileErrorKind(row.kind)
    backend = await get_archive_backend(db)

    def _open() -> None:
        # We don't need any data from the archive — just whether it
        # opens. Discard the reader immediately so we don't hold a
        # CBR/CBZ handle past the retry.
        reader = open_archive(path, backend=backend)
        # Listing pages is cheap and catches some "opened OK but
        # internally broken" archives (e.g., zero entries reported by
        # rarfile when the central directory is shot).
        reader.list_pages()

    try:
        await asyncio.to_thread(_open)
    except (ArchiveError, UnsupportedArchiveError, OSError) as e:
        await record_error(
            db,
            path=path,
            kind=kind,
            exc=e,
            file_id=row.file_id,
        )
        return TryOpenResult(
            ok=False,
            error_class=type(e).__name__,
            error_message=str(e)[:2000],
        )

    # Open succeeded. Clear this row (and any other kinds for this
    # path — if the archive opens cleanly, a stale comicinfo_parse
    # row from a previous run might also be irrelevant; the next
    # scan will re-record real ones).
    await clear_errors_for_path(db, path)
    return TryOpenResult(ok=True)
