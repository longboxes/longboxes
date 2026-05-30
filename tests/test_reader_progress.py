"""Tests for per-user reading progress (Phase 6).

Covers ``app.services.reader``'s progress half: saving / reading a
position, the sticky ``finished_at`` semantics, and the home page's
"Continue reading" / "Recently read" list queries with their label
resolution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models import (
    ComicInfoStatus,
    CvIssue,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    LocalIssue,
    LocalVolume,
    MatchSource,
    MatchStatus,
    User,
)
from app.services.reader import (
    get_read_progress,
    is_file_match_resolved,
    issue_progress_for_volume,
    list_continue_reading,
    list_recently_read,
    progress_bars_by_file,
    reset_read_progress,
    save_read_progress,
)

pytestmark = pytest.mark.asyncio


# ---- Row builders -------------------------------------------------------


def _user(name: str = "reader") -> User:
    return User(username=name, password_hash="x", role="viewer")


def _file() -> File:
    return File(
        sha256=f"{uuid.uuid4().int:064x}",
        size_bytes=1024,
        archive_format="cbz",
        page_count=20,
        comicinfo_status=ComicInfoStatus.NONE,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )


def _location(file_id: uuid.UUID, path: str) -> FileLocation:
    return FileLocation(
        file_id=file_id,
        path=path,
        mtime=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
    )


def _cv_volume(cv_id: int, name: str) -> CvVolume:
    return CvVolume(
        cv_id=cv_id,
        name=name,
        year=2012,
        publisher_cv_id=None,
        count_of_issues=5,
        raw_payload={"id": cv_id},
        fetched_at=datetime.now(tz=UTC),
    )


def _cv_issue(cv_id: int, *, volume_cv_id: int, number: str, name: str | None = None) -> CvIssue:
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=volume_cv_id,
        issue_number=number,
        cover_date=None,
        name=name,
        raw_payload=None,
        fetched_at=None,
    )


def _local_volume(name: str) -> LocalVolume:
    return LocalVolume(name=name, year=2020)


def _local_issue(local_volume_id: uuid.UUID, number: str) -> LocalIssue:
    return LocalIssue(local_volume_id=local_volume_id, issue_number=number)


def _cv_match(file_id: uuid.UUID, issue_cv_id: int) -> FileMatch:
    return FileMatch(
        file_id=file_id,
        issue_cv_id=issue_cv_id,
        status=MatchStatus.AUTO,
        source=MatchSource.FILENAME,
        matched_at=datetime.now(tz=UTC),
    )


def _local_match(file_id: uuid.UUID, local_issue_id: uuid.UUID) -> FileMatch:
    return FileMatch(
        file_id=file_id,
        local_issue_id=local_issue_id,
        status=MatchStatus.LOCAL,
        source=MatchSource.LOCAL,
        matched_at=datetime.now(tz=UTC),
    )


def _resolved_match(file_id: uuid.UUID) -> FileMatch:
    """A minimal status=AUTO ``file_matches`` row with no specific
    target — enough to satisfy the resolved-status filter on
    ``list_continue_reading`` / ``list_recently_read`` without
    standing up a full CV-issue or local-issue fixture.

    The ``ck_file_matches_single_target`` CHECK allows zero or one
    targets (``<= 1``), so an all-null AUTO row is valid. Used in
    tests that exercise the listing logic itself, not the label
    resolution (which has its own dedicated tests below)."""
    return FileMatch(
        file_id=file_id,
        status=MatchStatus.AUTO,
        source=MatchSource.FILENAME,
        matched_at=datetime.now(tz=UTC),
    )


# ---- save / get ---------------------------------------------------------


async def test_save_and_get_progress(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.commit()

    assert await get_read_progress(db_session, user.id, f.id) is None

    saved = await save_read_progress(db_session, user.id, f.id, page=3, page_count=20)
    assert saved.page == 3
    assert saved.page_count == 20
    assert saved.finished_at is None

    got = await get_read_progress(db_session, user.id, f.id)
    assert got is not None
    assert got.page == 3


async def test_finished_at_set_on_last_page(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.commit()

    saved = await save_read_progress(db_session, user.id, f.id, page=19, page_count=20)
    assert saved.finished_at is not None


async def test_finished_at_is_sticky(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.commit()

    finished = (
        await save_read_progress(db_session, user.id, f.id, page=19, page_count=20)
    ).finished_at
    assert finished is not None

    # Paging back — re-reading must not un-finish the comic.
    again = await save_read_progress(db_session, user.id, f.id, page=2, page_count=20)
    assert again.page == 2
    assert again.finished_at == finished


async def test_save_clamps_page_into_range(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.commit()

    over = await save_read_progress(db_session, user.id, f.id, page=999, page_count=10)
    assert over.page == 9  # clamped onto the last page
    assert over.finished_at is not None

    under = await save_read_progress(db_session, user.id, f.id, page=-5, page_count=10)
    assert under.page == 0


# ---- list_continue_reading ---------------------------------------------


async def test_continue_reading_lists_in_progress(db_session):
    user, fa, fb, fc = _user(), _file(), _file(), _file()
    db_session.add_all([user, fa, fb, fc])
    await db_session.flush()
    # Listing query joins file_matches and filters to resolved statuses;
    # bare files without a match row are skipped (see the unmatched-files
    # tests below). The actual target doesn't matter here — we're
    # exercising the in-progress vs finished ordering, not labels.
    db_session.add_all([_resolved_match(fa.id), _resolved_match(fb.id), _resolved_match(fc.id)])
    await db_session.commit()

    await save_read_progress(db_session, user.id, fa.id, page=5, page_count=20)
    await save_read_progress(db_session, user.id, fb.id, page=2, page_count=20)
    # Finished — belongs in Recently read, not Continue reading.
    await save_read_progress(db_session, user.id, fc.id, page=19, page_count=20)

    cards = await list_continue_reading(db_session, user.id)
    ids = [c.file_id for c in cards]
    assert set(ids) == {fa.id, fb.id}
    assert ids[0] == fb.id  # most recently read first


async def test_continue_reading_excludes_page_zero(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.flush()
    db_session.add(_resolved_match(f.id))
    await db_session.commit()

    # Opened but never paged past the first page — not "in progress".
    await save_read_progress(db_session, user.id, f.id, page=0, page_count=20)
    assert await list_continue_reading(db_session, user.id) == []


async def test_progress_is_per_user(db_session):
    alice, bob, f = _user("alice"), _user("bob"), _file()
    db_session.add_all([alice, bob, f])
    await db_session.flush()
    db_session.add(_resolved_match(f.id))
    await db_session.commit()

    await save_read_progress(db_session, alice.id, f.id, page=4, page_count=20)
    assert len(await list_continue_reading(db_session, alice.id)) == 1
    assert await list_continue_reading(db_session, bob.id) == []


# ---- list_recently_read -------------------------------------------------


async def test_recently_read_lists_finished(db_session):
    user, fa, fb = _user(), _file(), _file()
    db_session.add_all([user, fa, fb])
    await db_session.flush()
    db_session.add_all([_resolved_match(fa.id), _resolved_match(fb.id)])
    await db_session.commit()

    await save_read_progress(db_session, user.id, fa.id, page=9, page_count=10)
    await save_read_progress(db_session, user.id, fb.id, page=9, page_count=10)

    cards = await list_recently_read(db_session, user.id)
    ids = [c.file_id for c in cards]
    assert set(ids) == {fa.id, fb.id}
    assert ids[0] == fb.id  # most recently finished first
    assert all(c.finished for c in cards)


# ---- card labels --------------------------------------------------------


async def test_card_label_cv_file(db_session):
    user = _user()
    vol = _cv_volume(100, "Saga")
    db_session.add_all([user, vol])
    await db_session.flush()
    issue = _cv_issue(1001, volume_cv_id=100, number="7", name="The Issue")
    f = _file()
    db_session.add_all([issue, f])
    await db_session.flush()
    db_session.add(_cv_match(f.id, 1001))
    await db_session.commit()

    await save_read_progress(db_session, user.id, f.id, page=4, page_count=20)
    cards = await list_continue_reading(db_session, user.id)
    assert len(cards) == 1
    assert cards[0].title == "Saga"
    assert cards[0].subtitle == "#7 — The Issue"
    assert cards[0].cover_url == f"/review/file/{f.id}/cover"
    # Cover thumb → reader, title/subtitle → CV issue page.
    assert cards[0].read_url == f"/read/{f.id}"
    assert cards[0].issue_url == "/issue/1001"
    # page 4 (0-based) -> 5 of 20 -> 25%
    assert cards[0].percent == 25


async def test_card_label_local_file(db_session):
    user = _user()
    lv = _local_volume("Indie Book")
    db_session.add_all([user, lv])
    await db_session.flush()
    li = _local_issue(lv.id, "3")
    f = _file()
    db_session.add_all([li, f])
    await db_session.flush()
    db_session.add(_local_match(f.id, li.id))
    await db_session.commit()

    await save_read_progress(db_session, user.id, f.id, page=1, page_count=20)
    cards = await list_continue_reading(db_session, user.id)
    assert cards[0].title == "Indie Book"
    assert cards[0].subtitle == "#3"
    # Title/subtitle text links to the local-issue page.
    assert cards[0].issue_url == f"/local/issue/{li.id}"


async def test_continue_reading_excludes_unmatched_files(db_session):
    """Files with no resolved match (no ``file_matches`` row, or one
    in PENDING / UNMATCHED / REJECTED) don't appear on Continue
    Reading. The user has no surface to reach reset-progress on an
    unmatched file, so showing it would strand them. The save
    service may still have a stale row from before the gate landed;
    the listing query joins to ``file_matches`` and filters at query
    time, so the stale row drops out without a migration.

    Also pins that a CV-matched file in the same fixture DOES still
    appear — the filter is a positive whitelist, not a global gate.
    """
    user = _user()
    db_session.add(user)
    await db_session.flush()

    # File 1: unmatched (no file_matches row).
    unmatched = _file()
    db_session.add(unmatched)
    await db_session.flush()
    db_session.add(_location(unmatched.id, "/library/Mystery Comic 4.cbz"))

    # File 2: a properly CV-matched file in the same fixture.
    vol = _cv_volume(200, "Visible Volume")
    db_session.add(vol)
    await db_session.flush()
    issue = _cv_issue(2001, volume_cv_id=200, number="1", name="Pilot")
    matched = _file()
    db_session.add_all([issue, matched])
    await db_session.flush()
    db_session.add(_cv_match(matched.id, 2001))
    await db_session.commit()

    # save_read_progress is the LOW-LEVEL helper — bypasses the
    # route's gate intentionally so we can simulate a pre-existing
    # row for the unmatched file. The route-level gate is tested
    # separately below.
    await save_read_progress(
        db_session,
        user.id,
        unmatched.id,
        page=2,
        page_count=20,
    )
    await save_read_progress(
        db_session,
        user.id,
        matched.id,
        page=2,
        page_count=20,
    )

    cards = await list_continue_reading(db_session, user.id)
    # Only the matched file surfaces.
    assert [c.file_id for c in cards] == [matched.id]


async def test_recently_read_excludes_unmatched_files(db_session):
    """Same filter on Recently Read — finished progress against an
    unmatched file is suppressed too."""
    user = _user()
    unmatched = _file()
    db_session.add_all([user, unmatched])
    await db_session.flush()
    db_session.add(_location(unmatched.id, "/library/Unknown.cbz"))
    await db_session.commit()

    await save_read_progress(
        db_session,
        user.id,
        unmatched.id,
        page=19,
        page_count=20,
    )
    # save_read_progress stamps finished_at when the last page is reached.
    cards = await list_recently_read(db_session, user.id)
    assert cards == []


# ---- reset --------------------------------------------------------------


async def test_reset_read_progress_deletes_row(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.commit()
    await save_read_progress(db_session, user.id, f.id, page=5, page_count=20)
    assert await get_read_progress(db_session, user.id, f.id) is not None

    removed = await reset_read_progress(db_session, user.id, f.id)
    assert removed is True
    assert await get_read_progress(db_session, user.id, f.id) is None


async def test_reset_read_progress_noop_when_unread(db_session):
    user, f = _user(), _file()
    db_session.add_all([user, f])
    await db_session.commit()
    # Nothing saved — reset reports False rather than raising.
    assert await reset_read_progress(db_session, user.id, f.id) is False


# ---- issue-grid batch helpers ------------------------------------------


async def test_progress_bars_by_file(db_session):
    user, f1, f2, f3 = _user(), _file(), _file(), _file()
    db_session.add_all([user, f1, f2, f3])
    await db_session.commit()
    await save_read_progress(db_session, user.id, f1.id, page=5, page_count=20)
    # f2 only ever saw the first page — no bar worth showing.
    await save_read_progress(db_session, user.id, f2.id, page=0, page_count=20)
    # f3 is unread.

    bars = await progress_bars_by_file(db_session, user.id, [f1.id, f2.id, f3.id])
    assert set(bars) == {f1.id}
    assert bars[f1.id].percent == 30  # page 6 of 20


async def test_issue_progress_for_volume(db_session):
    user = _user()
    vol = _cv_volume(100, "Saga")
    db_session.add_all([user, vol])
    await db_session.flush()
    i1 = _cv_issue(1001, volume_cv_id=100, number="1")
    i2 = _cv_issue(1002, volume_cv_id=100, number="2")
    f1, f2 = _file(), _file()
    db_session.add_all([i1, i2, f1, f2])
    await db_session.flush()
    db_session.add_all([_cv_match(f1.id, 1001), _cv_match(f2.id, 1002)])
    await db_session.commit()
    # Only issue 1001's file has been read.
    await save_read_progress(db_session, user.id, f1.id, page=9, page_count=20)

    bars = await issue_progress_for_volume(db_session, user.id, [1001, 1002])
    assert set(bars) == {1001}
    assert bars[1001].percent == 50  # page 10 of 20


# ---- is_file_match_resolved -------------------------------------------


async def test_is_file_match_resolved_true_for_auto_match(db_session):
    user = _user()
    vol = _cv_volume(300, "X")
    db_session.add_all([user, vol])
    await db_session.flush()
    issue = _cv_issue(3001, volume_cv_id=300, number="1", name="One")
    f = _file()
    db_session.add_all([issue, f])
    await db_session.flush()
    db_session.add(_cv_match(f.id, 3001))  # status=AUTO by helper default
    await db_session.commit()

    assert await is_file_match_resolved(db_session, f.id) is True


async def test_is_file_match_resolved_false_for_unmatched(db_session):
    """No file_matches row at all — the bare-file case the user hit."""
    f = _file()
    db_session.add(f)
    await db_session.commit()
    assert await is_file_match_resolved(db_session, f.id) is False


async def test_is_file_match_resolved_false_for_pending(db_session):
    """A PENDING file_matches row also fails the gate — the file is
    in the review queue, not on an issue page where reset-progress
    could be reached."""
    f = _file()
    db_session.add(f)
    await db_session.flush()
    db_session.add(
        FileMatch(
            file_id=f.id,
            issue_cv_id=None,
            confidence=None,
            status=MatchStatus.PENDING,
            source=MatchSource.FILENAME,
            matched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()
    assert await is_file_match_resolved(db_session, f.id) is False
