"""Tests for ``app.services.file_errors`` — the per-file failure
inventory backing ``/admin/file-errors``.

Covers the contract the admin page depends on:

* ``record_error`` upserts on ``(path, kind)`` — re-failure refreshes
  the exception fields and ``last_seen_at`` but leaves ``first_seen_at``
  alone.
* ``clear_errors_for_path`` deletes every row for a path across kinds
  (the scanner's "this path now succeeded" hook).
* ``clear_error_for_path_and_kind`` is the narrow version used by
  out-of-band success paths (e.g., the cover endpoint).
* ``dismiss_file_error`` drops one row, returns False on a stale id.
* ``try_open_archive`` succeeds → row deleted; fails → row stays and
  ``last_seen_at`` advances.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.archives.base import ArchiveError
from app.models import FileError, FileErrorKind
from app.services.file_errors import (
    clear_error_for_path_and_kind,
    clear_errors_for_path,
    count_file_errors,
    dismiss_file_error,
    list_file_errors,
    record_error,
    try_open_archive,
)
from tests.fixtures import build_cbz

pytestmark = pytest.mark.asyncio


async def test_record_error_inserts_then_upserts(db_session):
    """First call inserts; second call for the same (path, kind)
    UPSERTs in place — refreshes the message + ``last_seen_at``
    but leaves ``first_seen_at`` untouched."""
    await record_error(
        db_session,
        path="/library/broken.cbr",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("rar header missing"),
    )
    rows = (await db_session.execute(select(FileError))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    first_seen = row.first_seen_at
    assert row.error_class == "ArchiveError"
    assert "rar header missing" in (row.error_message or "")

    # Sleep so ``last_seen_at`` is measurably later. The timestamps
    # are pg ``now()`` server-side — within the same statement they're
    # identical, so we need real wall-clock separation.
    await asyncio.sleep(0.01)
    await record_error(
        db_session,
        path="/library/broken.cbr",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("different message now"),
    )
    rows = (await db_session.execute(select(FileError))).scalars().all()
    assert len(rows) == 1  # still one row
    row = rows[0]
    assert "different message now" in (row.error_message or "")
    assert row.first_seen_at == first_seen
    assert row.last_seen_at >= first_seen


async def test_record_error_distinct_kinds_for_same_path(db_session):
    """``(path, kind)`` is the unique key. Different kinds at the
    same path produce separate rows — the inspector wants to show
    both the archive_open and the comicinfo_parse failure for a
    single broken file."""
    await record_error(
        db_session,
        path="/library/x.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("a"),
    )
    await record_error(
        db_session,
        path="/library/x.cbz",
        kind=FileErrorKind.COMICINFO_PARSE,
        exc=ValueError("b"),
    )
    rows = (await db_session.execute(select(FileError))).scalars().all()
    assert len(rows) == 2
    assert {r.kind for r in rows} == {"archive_open", "comicinfo_parse"}


async def test_clear_errors_for_path_drops_every_kind(db_session):
    await record_error(
        db_session,
        path="/lib/a.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("e"),
    )
    await record_error(
        db_session,
        path="/lib/a.cbz",
        kind=FileErrorKind.COVER_EXTRACTION,
        exc=ArchiveError("e"),
    )
    # An unrelated path stays put.
    await record_error(
        db_session,
        path="/lib/b.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("e"),
    )
    deleted = await clear_errors_for_path(db_session, "/lib/a.cbz")
    assert deleted == 2
    remaining = (await db_session.execute(select(FileError))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].path == "/lib/b.cbz"


async def test_clear_error_for_path_and_kind_narrow(db_session):
    """The out-of-band success path: cover endpoint succeeds → drop
    the cover_extraction row only. Don't claim the archive_open or
    comicinfo_parse rows for the same path are also resolved (they
    weren't re-checked)."""
    await record_error(
        db_session,
        path="/lib/c.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("e"),
    )
    await record_error(
        db_session,
        path="/lib/c.cbz",
        kind=FileErrorKind.COVER_EXTRACTION,
        exc=ArchiveError("e"),
    )
    deleted = await clear_error_for_path_and_kind(
        db_session, "/lib/c.cbz", FileErrorKind.COVER_EXTRACTION
    )
    assert deleted == 1
    remaining = (await db_session.execute(select(FileError))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].kind == "archive_open"


async def test_list_and_count(db_session):
    assert await count_file_errors(db_session) == 0
    assert await list_file_errors(db_session) == []

    await record_error(
        db_session,
        path="/lib/1.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("e1"),
    )
    await record_error(
        db_session,
        path="/lib/2.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("e2"),
    )
    assert await count_file_errors(db_session) == 2
    rows = await list_file_errors(db_session)
    # Sorted most-recent first; both inserted in the same statement
    # window, so we accept either order — the contract is just
    # "stable list of rows".
    paths = {r.path for r in rows}
    assert paths == {"/lib/1.cbz", "/lib/2.cbz"}


async def test_dismiss_file_error_returns_false_when_gone(db_session):
    await record_error(
        db_session,
        path="/lib/x.cbz",
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("e"),
    )
    row = (await db_session.execute(select(FileError))).scalar_one()
    assert await dismiss_file_error(db_session, row.id) is True
    # Second call: row already gone.
    assert await dismiss_file_error(db_session, row.id) is False


async def test_try_open_archive_success_clears_row(db_session, tmp_path):
    """A real, openable CBZ on disk — the retry succeeds and the
    (path, kind) row is deleted."""
    archive = tmp_path / "good.cbz"
    build_cbz(archive)
    # Pre-populate a stale error row for this path.
    await record_error(
        db_session,
        path=str(archive),
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("was broken before"),
    )
    row = (await db_session.execute(select(FileError))).scalar_one()

    result = await try_open_archive(db_session, row.id)
    assert result is not None
    assert result.ok is True
    # Row is gone.
    assert (
        await db_session.execute(select(FileError))
    ).scalar_one_or_none() is None


async def test_try_open_archive_failure_refreshes_row(db_session, tmp_path):
    """A path that doesn't open — the retry surfaces the exception
    and refreshes the row's message + ``last_seen_at``."""
    bad = tmp_path / "missing.cbr"  # doesn't exist on disk
    await record_error(
        db_session,
        path=str(bad),
        kind=FileErrorKind.ARCHIVE_OPEN,
        exc=ArchiveError("first attempt"),
    )
    row_before = (await db_session.execute(select(FileError))).scalar_one()
    first_msg = row_before.error_message
    await asyncio.sleep(0.01)

    result = await try_open_archive(db_session, row_before.id)
    assert result is not None
    assert result.ok is False
    row_after = (await db_session.execute(select(FileError))).scalar_one()
    # Row still present, exception fields refreshed with the new
    # failure's class+message.
    assert row_after.id == row_before.id
    assert row_after.error_message != first_msg
    assert row_after.last_seen_at >= row_before.last_seen_at


async def test_try_open_archive_returns_none_for_missing_id(db_session):
    import uuid as _uuid
    result = await try_open_archive(db_session, _uuid.uuid4())
    assert result is None
