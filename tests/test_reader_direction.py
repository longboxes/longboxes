"""Tests for the reader's per-volume reading-direction service (Phase 6).

Covers ``app.services.reader``: resolving a file to its owning volume,
and reading / persisting the left-to-right vs right-to-left choice
across CV, local, supplement, and unmatched files.
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
    FileMatch,
    LocalIssue,
    LocalVolume,
    MatchSource,
    MatchStatus,
)
from app.services.reader import (
    DEFAULT_READING_DIRECTION,
    get_reading_direction,
    set_reading_direction,
)

pytestmark = pytest.mark.asyncio


# ---- Row builders -------------------------------------------------------


def _file() -> File:
    return File(
        sha256=f"{uuid.uuid4().int:064x}",
        size_bytes=1024,
        archive_format="cbz",
        page_count=12,
        comicinfo_status=ComicInfoStatus.NONE,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )


def _cv_volume(cv_id: int, *, reading_direction: str | None = None) -> CvVolume:
    vol = CvVolume(
        cv_id=cv_id,
        name=f"CV Volume {cv_id}",
        year=2015,
        publisher_cv_id=None,
        count_of_issues=3,
        raw_payload={"id": cv_id},
        fetched_at=datetime.now(tz=UTC),
    )
    if reading_direction is not None:
        vol.reading_direction = reading_direction
    return vol


def _cv_issue(cv_id: int, *, volume_cv_id: int) -> CvIssue:
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=volume_cv_id,
        issue_number="1",
        cover_date=None,
        name=None,
        raw_payload=None,
        fetched_at=None,
    )


def _local_volume(*, reading_direction: str | None = None) -> LocalVolume:
    vol = LocalVolume(name="Local Series", year=2020)
    if reading_direction is not None:
        vol.reading_direction = reading_direction
    return vol


def _local_issue(local_volume_id: uuid.UUID) -> LocalIssue:
    return LocalIssue(local_volume_id=local_volume_id, issue_number="1")


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


def _supplement_match(file_id: uuid.UUID, volume_cv_id: int) -> FileMatch:
    return FileMatch(
        file_id=file_id,
        supplement_volume_cv_id=volume_cv_id,
        supplement_type="cover_gallery",
        status=MatchStatus.SUPPLEMENT,
        source=MatchSource.MANUAL,
        matched_at=datetime.now(tz=UTC),
    )


# ---- Model default ------------------------------------------------------


async def test_new_volumes_default_to_ltr(db_session):
    cv = _cv_volume(300)
    local = _local_volume()
    db_session.add_all([cv, local])
    await db_session.commit()
    assert cv.reading_direction == "ltr"
    assert local.reading_direction == "ltr"


# ---- CV file ------------------------------------------------------------


async def test_cv_file_reads_and_writes_volume_direction(db_session):
    db_session.add(_cv_volume(100))
    await db_session.flush()  # volume before issue (FK)
    db_session.add(_cv_issue(1001, volume_cv_id=100))
    f = _file()
    db_session.add(f)
    await db_session.flush()  # issue + file before match (FK)
    db_session.add(_cv_match(f.id, 1001))
    await db_session.commit()

    # Untouched volume reads as the default.
    assert await get_reading_direction(db_session, f.id) == "ltr"

    # Setting RTL persists onto the CV volume and round-trips back.
    assert await set_reading_direction(db_session, f.id, "rtl") is True
    assert await get_reading_direction(db_session, f.id) == "rtl"
    refreshed = await db_session.get(CvVolume, 100)
    assert refreshed.reading_direction == "rtl"


# ---- Local file ---------------------------------------------------------


async def test_local_file_reads_and_writes_volume_direction(db_session):
    vol = _local_volume()
    db_session.add(vol)
    await db_session.flush()  # local volume before local issue (FK)
    issue = _local_issue(vol.id)
    db_session.add(issue)
    f = _file()
    db_session.add(f)
    await db_session.flush()  # issue + file before match (FK)
    db_session.add(_local_match(f.id, issue.id))
    await db_session.commit()

    assert await get_reading_direction(db_session, f.id) == "ltr"
    assert await set_reading_direction(db_session, f.id, "rtl") is True
    assert await get_reading_direction(db_session, f.id) == "rtl"
    refreshed = await db_session.get(LocalVolume, vol.id)
    assert refreshed.reading_direction == "rtl"


# ---- Supplement file ----------------------------------------------------


async def test_supplement_file_uses_attached_cv_volume(db_session):
    # A supplement file attaches straight to a CV volume — no issue hop.
    db_session.add(_cv_volume(200, reading_direction="rtl"))
    f = _file()
    db_session.add(f)
    await db_session.flush()  # volume + file before match (FK)
    db_session.add(_supplement_match(f.id, 200))
    await db_session.commit()

    assert await get_reading_direction(db_session, f.id) == "rtl"
    assert await set_reading_direction(db_session, f.id, "ltr") is True
    assert await get_reading_direction(db_session, f.id) == "ltr"


# ---- Unmatched files ----------------------------------------------------


async def test_unmatched_file_falls_back_to_default(db_session):
    f = _file()
    db_session.add(f)
    await db_session.commit()

    assert await get_reading_direction(db_session, f.id) == DEFAULT_READING_DIRECTION
    # No volume to store it on — reports False rather than raising.
    assert await set_reading_direction(db_session, f.id, "rtl") is False
    assert await get_reading_direction(db_session, f.id) == "ltr"


async def test_unknown_file_id_falls_back_to_default(db_session):
    missing = uuid.uuid4()
    assert await get_reading_direction(db_session, missing) == "ltr"
    assert await set_reading_direction(db_session, missing, "rtl") is False


# ---- Validation ---------------------------------------------------------


async def test_invalid_direction_raises(db_session):
    f = _file()
    db_session.add(f)
    await db_session.commit()
    with pytest.raises(ValueError):
        await set_reading_direction(db_session, f.id, "sideways")
