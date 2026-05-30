"""Tests for the library browse service queries.

These set up small in-memory libraries (synthetic CV cache rows + file +
match rows) and assert on the aggregations / sort behavior. Routes are
covered separately in test_library_routes.py.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    ComicInfoStatus,
    CvCharacter,
    CvCharacterVolume,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvStoryArc,
    CvTeam,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    LocalIssue,
    LocalVolume,
    MatchSource,
    MatchStatus,
    ReadProgress,
    User,
)
from app.services.library import (
    LibraryFilters,
    get_arc_detail,
    get_character_detail,
    get_creator_detail,
    get_issue_detail,
    get_team_detail,
    get_volume_detail,
    list_library_volumes,
    list_publishers_in_library,
    list_recently_added,
)
from app.services.local import (
    attach_local_group,
    attach_supplement,
    get_local_issue_detail,
    get_local_volume_detail,
    list_volume_supplements,
    merge_local_volumes,
    preview_local_group,
    update_local_issue,
    update_local_volume,
)
from app.services.review import (
    exclude_files_by_series,
    execute_fix_match,
    get_group_reference,
    list_pending_groups,
)

pytestmark = pytest.mark.asyncio


# ---- Test data helpers --------------------------------------------------


def _publisher(cv_id: int, name: str) -> CvPublisher:
    return CvPublisher(
        cv_id=cv_id,
        name=name,
        raw_payload={"id": cv_id, "name": name},
        fetched_at=datetime.now(tz=UTC),
    )


def _volume(
    cv_id: int,
    name: str,
    *,
    year: int | None = 2012,
    publisher_cv_id: int | None = None,
    count_of_issues: int | None = 3,
    image: dict | None = None,
) -> CvVolume:
    return CvVolume(
        cv_id=cv_id,
        name=name,
        year=year,
        publisher_cv_id=publisher_cv_id,
        count_of_issues=count_of_issues,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": image or {"thumb_url": f"https://example.com/{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
    )


def _char_volume(
    character_cv_id: int,
    volume_cv_id: int,
    name: str,
    *,
    cover_url: str | None = None,
    position: int = 0,
) -> CvCharacterVolume:
    """A scraped character→volume row (the issues-cover scrape output)."""
    return CvCharacterVolume(
        character_cv_id=character_cv_id,
        volume_cv_id=volume_cv_id,
        name=name,
        cover_url=cover_url,
        position=position,
    )


def _issue(
    cv_id: int,
    *,
    volume_cv_id: int,
    issue_number: str,
    name: str | None = None,
    cover_date: date | None = None,
    payload: dict | None = None,
) -> CvIssue:
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=volume_cv_id,
        issue_number=issue_number,
        cover_date=cover_date,
        name=name,
        raw_payload=payload,
        fetched_at=datetime.now(tz=UTC) if payload else None,
    )


def _file(sha: str) -> File:
    return File(
        sha256=sha,
        size_bytes=1024,
        archive_format="cbz",
        page_count=20,
        comicinfo_status=ComicInfoStatus.NONE,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )


def _location(file_id, path: str) -> FileLocation:
    return FileLocation(
        file_id=file_id,
        path=path,
        mtime=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
    )


def _match(file_id, issue_cv_id: int, status: MatchStatus = MatchStatus.AUTO) -> FileMatch:
    return FileMatch(
        file_id=file_id,
        issue_cv_id=issue_cv_id,
        confidence=None,
        status=status,
        source=MatchSource.FILENAME,
        matched_at=datetime.now(tz=UTC),
    )


def _bare_match(file_id) -> FileMatch:
    """An UNMATCHED file_matches row with no target — the pre-state a
    file sits in before a reviewer attaches it as a supplement."""
    return FileMatch(
        file_id=file_id,
        issue_cv_id=None,
        confidence=None,
        status=MatchStatus.UNMATCHED,
        source=MatchSource.FILENAME,
        matched_at=datetime.now(tz=UTC),
    )


async def _make_library(db_session) -> None:
    """Seed: 2 publishers, 2 volumes, several issues, files+matches such
    that volume 100 has 2/3 owned and volume 200 has 1/2 owned.

    Flushes between FK-dependent groups because the CV models don't define
    ``relationship()`` between cv_volumes / cv_issues / cv_publishers — the
    SQLAlchemy UoW therefore can't auto-order INSERTs by FK dependency, and
    a single trailing flush tries to insert issues before their volumes.
    Real app code never hits this (the cache layer commits each upsert
    individually); only this synthetic test helper does.
    """
    db_session.add_all([_publisher(31, "Image"), _publisher(10, "Marvel")])
    await db_session.flush()  # publishers must exist before volumes reference them

    db_session.add_all(
        [
            _volume(100, "Saga", year=2012, publisher_cv_id=31, count_of_issues=3),
            _volume(200, "X-Men", year=1991, publisher_cv_id=10, count_of_issues=2),
        ]
    )
    await db_session.flush()  # volumes must exist before issues reference them

    db_session.add_all(
        [
            _issue(1001, volume_cv_id=100, issue_number="1"),
            _issue(1002, volume_cv_id=100, issue_number="2"),
            _issue(1003, volume_cv_id=100, issue_number="3"),
            _issue(2001, volume_cv_id=200, issue_number="1"),
            _issue(2002, volume_cv_id=200, issue_number="2"),
        ]
    )
    files = [_file(f"{i:064x}") for i in range(3)]
    db_session.add_all(files)
    await db_session.flush()  # issues + files must exist before locations/matches reference them

    db_session.add_all(
        [
            _location(files[0].id, "/library/saga1.cbz"),
            _location(files[1].id, "/library/saga2.cbz"),
            _location(files[2].id, "/library/xmen1.cbz"),
            _match(files[0].id, 1001),  # owns Saga #1
            _match(files[1].id, 1002),  # owns Saga #2
            _match(files[2].id, 2001),  # owns X-Men #1
        ]
    )
    await db_session.commit()


# ---- list_library_volumes ----------------------------------------------


async def test_empty_library_returns_no_rows(db_session):
    rows, total = await list_library_volumes(db_session)
    assert rows == []
    assert total == 0


async def test_lists_volumes_with_owned_and_missing(db_session):
    await _make_library(db_session)
    rows, total = await list_library_volumes(db_session)
    by_id = {r.cv_id: r for r in rows}
    assert by_id[100].owned_count == 2
    assert by_id[100].count_of_issues == 3
    assert by_id[100].missing_count == 1
    assert by_id[100].has_missing is True
    assert by_id[200].owned_count == 1
    assert by_id[200].has_missing is True
    assert total == len(rows)


async def test_filter_by_publisher(db_session):
    await _make_library(db_session)
    rows, total = await list_library_volumes(
        db_session, LibraryFilters(publisher_cv_id=31)
    )
    assert [r.cv_id for r in rows] == [100]
    assert total == 1


async def test_filter_by_year(db_session):
    """When issues carry no cover_dates, the year filter falls back to
    matching start_year directly (the volumes in this fixture have
    year=1991 for X-Men and year=2012 for Saga)."""
    await _make_library(db_session)
    rows, _total = await list_library_volumes(
        db_session, LibraryFilters(year=1991)
    )
    assert [r.cv_id for r in rows] == [200]
    rows, _total = await list_library_volumes(
        db_session, LibraryFilters(year=2012)
    )
    assert [r.cv_id for r in rows] == [100]


async def test_filter_by_year_uses_hydrated_boundary_issues(db_session):
    """Long-running volumes need their last_issue hydrated for the
    span filter to compute the right end-year. ``_upsert_volume``'s
    first-touch branch enqueues a bulk ``volume_issues`` job that
    fills cover_date on every issue (including the first and last)
    in one paginated trip. This test simulates the partial state
    where only the boundary issues happen to be hydrated (the
    interior is still stubs) and verifies the filter still spans
    the full run."""
    db_session.add(_publisher(10, "DC"))
    await db_session.flush()
    db_session.add(
        _volume(300, "Action Comics", year=1938, publisher_cv_id=10,
                count_of_issues=904)
    )
    await db_session.flush()
    db_session.add_all([
        # first_issue — hydrated by the boundary-hydration enqueue.
        _issue(3001, volume_cv_id=300, issue_number="1",
               cover_date=date(1938, 6, 1)),
        # Mid-run issues — still stubs (cover_date=None).
        _issue(3500, volume_cv_id=300, issue_number="500"),
        # last_issue — hydrated by the boundary-hydration enqueue.
        _issue(3904, volume_cv_id=300, issue_number="904",
               cover_date=date(2011, 9, 1)),
    ])
    file_ = _file("d" * 64)
    db_session.add(file_)
    await db_session.flush()
    db_session.add_all([
        _location(file_.id, "/library/ac1.cbz"),
        _match(file_.id, 3001),
    ])
    await db_session.commit()
    # 2010 should match — last_issue is 2011-09-01.
    rows, _ = await list_library_volumes(
        db_session, LibraryFilters(year=2010)
    )
    assert [r.cv_id for r in rows] == [300]
    # 2012 should NOT match — past the last_issue year.
    rows, _ = await list_library_volumes(
        db_session, LibraryFilters(year=2012)
    )
    assert rows == []


async def test_filter_by_year_spans_running_volumes(db_session):
    """A multi-year volume matches every year it was actively
    publishing — not just its start_year. Built directly here rather
    than mutating ``_make_library`` so the assertion is obvious."""
    db_session.add(_publisher(31, "Image"))
    await db_session.flush()
    # Saga: started 2012, issues dated 2012 → 2014.
    db_session.add(
        _volume(100, "Saga", year=2012, publisher_cv_id=31, count_of_issues=3)
    )
    await db_session.flush()
    db_session.add_all([
        _issue(1001, volume_cv_id=100, issue_number="1", cover_date=date(2012, 6, 1)),
        _issue(1002, volume_cv_id=100, issue_number="2", cover_date=date(2013, 6, 1)),
        _issue(1003, volume_cv_id=100, issue_number="3", cover_date=date(2014, 6, 1)),
    ])
    file_ = _file("a" * 64)
    db_session.add(file_)
    await db_session.flush()
    db_session.add_all([
        _location(file_.id, "/library/saga1.cbz"),
        _match(file_.id, 1001),
    ])
    await db_session.commit()
    # Filtering by any year in [2012, 2014] matches Saga.
    for year in (2012, 2013, 2014):
        rows, _total = await list_library_volumes(
            db_session, LibraryFilters(year=year)
        )
        assert [r.cv_id for r in rows] == [100], f"year={year}"
    # Years outside the span don't match.
    for year in (2011, 2015):
        rows, _total = await list_library_volumes(
            db_session, LibraryFilters(year=year)
        )
        assert rows == [], f"year={year}"


async def test_filter_has_missing_only_excludes_complete(db_session):
    """Both seed volumes are incomplete. Make one complete and verify the
    filter excludes it."""
    await _make_library(db_session)
    # Add the third X-Men match → X-Men becomes complete (2/2).
    # sha256 column is VARCHAR(64); 59 + 5 = 64 chars exactly.
    file3 = _file("c" * 59 + "xmen2")
    db_session.add(file3)
    await db_session.flush()
    db_session.add_all([_location(file3.id, "/library/xmen2.cbz"), _match(file3.id, 2002)])
    await db_session.commit()
    rows, _total = await list_library_volumes(
        db_session, LibraryFilters(has_missing_only=True)
    )
    assert [r.cv_id for r in rows] == [100]  # only Saga still has missing issues


async def test_name_query_substring_match(db_session):
    """``name_query`` does case-insensitive %q% substring match.
    LIKE wildcards in the query are escaped, so searching for an
    underscore or percent matches the literal character."""
    await _make_library(db_session)
    # ``saga`` matches "Saga" (case-insensitive).
    rows_saga, total_saga = await list_library_volumes(
        db_session, LibraryFilters(name_query="saga")
    )
    assert [r.cv_id for r in rows_saga] == [100]
    assert total_saga == 1
    # ``men`` is a substring of "X-Men".
    rows_men, _ = await list_library_volumes(
        db_session, LibraryFilters(name_query="men")
    )
    assert [r.cv_id for r in rows_men] == [200]
    # ``%`` is escaped, so it doesn't match anything (no volume
    # actually contains a literal percent sign).
    rows_pct, _ = await list_library_volumes(
        db_session, LibraryFilters(name_query="%")
    )
    assert rows_pct == []
    # Empty/whitespace query is a no-op.
    rows_blank, total_blank = await list_library_volumes(
        db_session, LibraryFilters(name_query="   ")
    )
    assert len(rows_blank) == 2 and total_blank == 2


async def test_name_starts_with_letter(db_session):
    """``name_starts_with='X'`` returns X-Men only; 'S' returns Saga
    only; the case-insensitive 's' matches the same as 'S'."""
    await _make_library(db_session)
    rows_x, total_x = await list_library_volumes(
        db_session, LibraryFilters(name_starts_with="X")
    )
    assert [r.cv_id for r in rows_x] == [200]
    assert total_x == 1
    rows_s, _ = await list_library_volumes(
        db_session, LibraryFilters(name_starts_with="s")
    )
    assert [r.cv_id for r in rows_s] == [100]


async def test_pagination_limit_offset(db_session):
    """``limit`` + ``offset`` slice the result; ``total`` is the
    pre-pagination count under filters."""
    await _make_library(db_session)
    # Two volumes total (Saga, X-Men); name sort is alphabetical so
    # the order is Saga, X-Men.
    page1, total1 = await list_library_volumes(db_session, limit=1, offset=0)
    page2, total2 = await list_library_volumes(db_session, limit=1, offset=1)
    assert total1 == 2 and total2 == 2
    assert len(page1) == 1 and len(page2) == 1
    assert [page1[0].cv_id, page2[0].cv_id] == [100, 200]


async def test_publishers_in_library(db_session):
    await _make_library(db_session)
    pubs = await list_publishers_in_library(db_session)
    by_id = {pid: (name, icon) for pid, name, icon in pubs}
    assert set(by_id.keys()) == {10, 31}
    assert by_id[31][0] == "Image"
    # icon_url is None for these fixture publishers (no image payload).
    assert by_id[31][1] is None


# ---- get_volume_detail -------------------------------------------------


async def test_volume_detail_returns_issues_sorted(db_session):
    await _make_library(db_session)
    detail = await get_volume_detail(db_session, 100)
    assert detail is not None
    assert detail.volume.name == "Saga"
    assert [i.issue_number for i in detail.issues] == ["1", "2", "3"]
    assert detail.owned_count == 2
    assert detail.publisher_name == "Image"


async def test_volume_detail_marks_owned_correctly(db_session):
    await _make_library(db_session)
    detail = await get_volume_detail(db_session, 100)
    owned_by_issue = {i.issue_number: i.owned for i in detail.issues}
    assert owned_by_issue == {"1": True, "2": True, "3": False}


async def test_volume_detail_returns_none_for_unknown(db_session):
    assert await get_volume_detail(db_session, 99999) is None


async def test_volume_detail_themes_status_and_type(db_session):
    """A volume's scraped CV themes drive its status badge and override
    the issue-count type heuristic."""
    # One issue → heuristically a one-shot...
    vol = _volume(100, "Saga", count_of_issues=1)
    # ...but the scraped themes say Ongoing (type) + Complete (status).
    vol.themes = [
        {"id": 61, "name": "Ongoing"},
        {"id": 52, "name": "Complete"},
        {"id": 14, "name": "Crime"},
    ]
    db_session.add(vol)
    await db_session.commit()

    detail = await get_volume_detail(db_session, 100)
    assert detail is not None
    # The "Ongoing" theme beats the lone-issue one-shot heuristic.
    assert detail.volume_format == "ongoing"
    # Status comes from the themes; "Complete" wins over "Ongoing".
    assert detail.volume_status == "complete"

    # A volume with no scraped themes has no status, heuristic type.
    bare = _volume(200, "X-Men", count_of_issues=1)
    db_session.add(bare)
    await db_session.commit()
    bare_detail = await get_volume_detail(db_session, 200)
    assert bare_detail.volume_status is None
    assert bare_detail.volume_format == "one_shot"


async def test_volume_detail_credit_filter(db_session):
    """A volume opened from a team / creator volume card is narrowed to
    the issues that entity is credited on — the intersection of the
    volume's issues with the entity's credited-issue list."""
    # volume 100 (Saga): issues 1001 / 1002 / 1003, with 1001 + 1002 owned.
    await _make_library(db_session)
    # A team credited on Saga #1 and #3, plus an issue from another
    # volume (which the intersection drops).
    db_session.add(
        CvTeam(
            cv_id=850,
            name="The Longbox Crew",
            raw_payload={
                "id": 850,
                "name": "The Longbox Crew",
                "issue_credits": [
                    {"id": 1001, "name": "Saga #1"},
                    {"id": 1003, "name": "Saga #3"},
                    {"id": 2001, "name": "X-Men #1"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    # A creator — the person payload names the list ``issues`` rather
    # than ``issue_credits``; the service tolerates both keys.
    db_session.add(
        CvPerson(
            cv_id=700,
            name="Fiona Staples",
            raw_payload={
                "id": 700,
                "name": "Fiona Staples",
                "issues": [{"id": 1002, "name": "Saga #2"}],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    # A character — credited on Saga #2 and #3 via ``issue_credits``.
    db_session.add(
        CvCharacter(
            cv_id=600,
            name="The Will",
            raw_payload={
                "id": 600,
                "name": "The Will",
                "issue_credits": [
                    {"id": 1002, "name": "Saga #2"},
                    {"id": 1003, "name": "Saga #3"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    class _StubCreditCache:
        """Returns whatever's already in the DB — no network."""

        async def get_volume(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvVolume, cv_id)

        async def get_team(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvTeam, cv_id)

        async def get_person(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvPerson, cv_id)

        async def get_character(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvCharacter, cv_id)

    cache = _StubCreditCache()

    # No filter -> all three Saga issues, credit_filter unset.
    plain = await get_volume_detail(db_session, 100, cv_cache=cache)
    assert [i.issue_number for i in plain.issues] == ["1", "2", "3"]
    assert plain.credit_filter is None

    # Team filter -> only the credited issues, kept in volume order.
    by_team = await get_volume_detail(
        db_session, 100, cv_cache=cache, credit_filter=("team", 850),
    )
    assert [i.cv_id for i in by_team.issues] == [1001, 1003]
    assert [i.issue_number for i in by_team.issues] == ["1", "3"]
    cf = by_team.credit_filter
    assert cf is not None
    assert cf.kind == "team" and cf.cv_id == 850
    assert cf.name == "The Longbox Crew"
    assert cf.matched == 2 and cf.volume_total == 3
    # owned_issue_count is re-derived for the subset: 1001 owned, 1003 not.
    assert by_team.owned_issue_count == 1

    # Creator filter -> the ``issues`` fallback key, via get_person.
    by_creator = await get_volume_detail(
        db_session, 100, cv_cache=cache, credit_filter=("creator", 700),
    )
    assert [i.cv_id for i in by_creator.issues] == [1002]
    assert by_creator.credit_filter.kind == "creator"
    assert by_creator.credit_filter.name == "Fiona Staples"
    assert by_creator.owned_issue_count == 1  # 1002 is owned

    # Character filter -> a character's issue_credits, via get_character.
    by_character = await get_volume_detail(
        db_session, 100, cv_cache=cache, credit_filter=("character", 600),
    )
    assert [i.cv_id for i in by_character.issues] == [1002, 1003]
    assert by_character.credit_filter.kind == "character"
    assert by_character.credit_filter.name == "The Will"

    # An unresolved entity leaves the volume unfiltered.
    missing = await get_volume_detail(
        db_session, 100, cv_cache=cache, credit_filter=("team", 999999),
    )
    assert missing.credit_filter is None
    assert len(missing.issues) == 3


async def test_volume_detail_aggregates_story_arc_names(db_session):
    """Story arc names are harvested from hydrated issue payloads."""
    db_session.add(_volume(100, "Saga", count_of_issues=2))
    await db_session.flush()  # volume must exist before issues reference it
    db_session.add(
        _issue(
            1001,
            volume_cv_id=100,
            issue_number="1",
            payload={
                "id": 1001,
                "story_arc_credits": [{"id": 7, "name": "The Beginning"}],
            },
        )
    )
    db_session.add(
        _issue(
            1002,
            volume_cv_id=100,
            issue_number="2",
            payload={
                "id": 1002,
                "story_arc_credits": [
                    {"id": 7, "name": "The Beginning"},  # dup, should dedupe
                    {"id": 8, "name": "The Middle"},
                ],
            },
        )
    )
    await db_session.commit()
    detail = await get_volume_detail(db_session, 100)
    assert set(detail.story_arc_names) == {"The Beginning", "The Middle"}


async def test_volume_detail_arc_fetch_fills_in_stub_issue_arcs(db_session):
    """An arc fetch populates arc membership for stub issues — those whose
    own payload doesn't yet list ``story_arc_credits``. This is the whole
    point of the arc-driven population strategy: one ``/story_arc/X/``
    call lights up every member issue in the volume, including stubs."""
    from datetime import UTC, datetime

    from app.models import CvStoryArc

    db_session.add(_volume(100, "Saga", count_of_issues=3))
    await db_session.flush()
    # Issue 1001 is hydrated and mentions arc 7. Issues 1002 and 1003 are
    # stubs (raw_payload=None) — they don't know about any arcs from their
    # own payloads.
    db_session.add(
        _issue(
            1001,
            volume_cv_id=100,
            issue_number="1",
            payload={
                "id": 1001,
                "story_arc_credits": [{"id": 7, "name": "The Beginning"}],
            },
        )
    )
    db_session.add(_issue(1002, volume_cv_id=100, issue_number="2"))
    db_session.add(_issue(1003, volume_cv_id=100, issue_number="3"))
    # Pre-seed the arc cache row so the stand-in cache can fetch it back
    # without hitting the network. The arc's "issues" list includes all
    # three of this volume's issues plus an irrelevant issue from some
    # other volume (which the merge code should ignore).
    db_session.add(
        CvStoryArc(
            cv_id=7,
            name="The Beginning",
            raw_payload={
                "id": 7,
                "name": "The Beginning",
                "issues": [
                    {"id": 1001, "name": "Saga #1"},
                    {"id": 1002, "name": "Saga #2"},
                    {"id": 1003, "name": "Saga #3"},
                    {"id": 9999, "name": "Unrelated"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    class _StubArcCache:
        """Returns whatever's already in the DB — no network."""

        async def get_volume(self, db, cv_id, *, force_refresh=False):
            # ``get_volume_detail`` calls this first to support on-demand
            # fetching for unseen volumes. Tests pre-seed the volume row,
            # so passing through to ``db.get`` is equivalent to a cache
            # hit and avoids needing a real CV client in the test setup.
            return await db.get(CvVolume, cv_id)

        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvStoryArc, cv_id)

    detail = await get_volume_detail(db_session, 100, cv_cache=_StubArcCache())
    assert detail is not None
    # Every issue in the volume now carries arc 7 — including the two
    # stubs that knew nothing about it on their own.
    arcs_by_issue = {i.issue_number: [a.cv_id for a in i.arc_credits] for i in detail.issues}
    assert arcs_by_issue == {"1": [7], "2": [7], "3": [7]}
    # Arc slot list is unchanged: one slot for arc 7.
    assert [a.cv_id for a in detail.arc_slots] == [7]


async def test_volume_detail_arc_boundary_arrows(db_session):
    """Boundary arrows appear only where an arc's prev/next sibling lives
    in a different volume. In-volume neighbors get no arrow."""
    from datetime import UTC, datetime

    from app.models import CvStoryArc

    db_session.add(_volume(100, "Saga", count_of_issues=3))
    await db_session.flush()
    # All three of this volume's issues are in arc 7, plus the arc has
    # neighbors at positions 0 (before #1) and 4 (after #3) that live
    # elsewhere. So:
    #   row #1 → prev sibling (issue 9000) is elsewhere → ← arrow
    #            next sibling (issue 1002) is in this volume → no →
    #   row #2 → both neighbors in this volume → no arrows
    #   row #3 → prev in this volume → no ←
    #            next sibling (issue 9001) is elsewhere → → arrow
    # At least one issue's payload has to declare arc 7 — that's the
    # signal ``get_volume_detail`` uses to discover the arc and request
    # its full member list from the cache. Once arc 7 is in
    # ``arc_first_seen_order``, the fill-in pass propagates membership
    # to the stub siblings AND wires up the boundary arrows we're
    # actually testing here.
    db_session.add_all(
        [
            _issue(
                1001,
                volume_cv_id=100,
                issue_number="1",
                payload={
                    "id": 1001,
                    "story_arc_credits": [{"id": 7, "name": "Arc 7"}],
                },
            ),
            _issue(1002, volume_cv_id=100, issue_number="2"),
            _issue(1003, volume_cv_id=100, issue_number="3"),
        ]
    )
    db_session.add(
        CvStoryArc(
            cv_id=7,
            name="Arc 7",
            raw_payload={
                "id": 7,
                "name": "Arc 7",
                "issues": [
                    {"id": 9000, "name": "External Prev"},
                    {"id": 1001, "name": "Saga #1"},
                    {"id": 1002, "name": "Saga #2"},
                    {"id": 1003, "name": "Saga #3"},
                    {"id": 9001, "name": "External Next"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    class _StubArcCache:
        async def get_volume(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvVolume, cv_id)

        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvStoryArc, cv_id)

    detail = await get_volume_detail(db_session, 100, cv_cache=_StubArcCache())
    by_num = {i.issue_number: i for i in detail.issues}
    # Row #1: ← arrow to external prev (9000), no →
    assert [
        (link.arc.cv_id, link.issue_cv_id)
        for link in by_num["1"].prev_arc_links
    ] == [(7, 9000)]
    assert by_num["1"].next_arc_links == []
    # Row #2: nothing
    assert by_num["2"].prev_arc_links == []
    assert by_num["2"].next_arc_links == []
    # Row #3: no ←, → arrow to external next (9001)
    assert by_num["3"].prev_arc_links == []
    assert [
        (link.arc.cv_id, link.issue_cv_id)
        for link in by_num["3"].next_arc_links
    ] == [(7, 9001)]
    # Text-color palette is populated alongside bg palette
    assert 7 in detail.arc_text_color_classes
    assert detail.arc_text_color_classes[7].startswith("text-")


async def test_volume_detail_gallery_segments_split_on_arc_gaps(db_session):
    """Even when consecutive volume issues share the same arc fingerprint,
    an arc gap (the arc visits another volume in between) splits the
    segment. Each sub-segment gets its own arrows pointing at the
    elsewhere member that interrupted it."""
    from datetime import UTC, datetime

    from app.models import CvStoryArc

    db_session.add(_volume(100, "Vol", count_of_issues=4))
    await db_session.flush()
    # Issues #1, #2, #3, #4 all in arc 7. In arc reading order though,
    # there's an external X1 between #1 and #2, and #3 → #4 is
    # consecutive. So segments should be: [#1] [#2, [#3 cont…]] etc.
    # Actually: #1 → X1 → #2 → #3 → #4. So fingerprint is {7} throughout
    # in the volume, but the arc-order gap between #1 and #2 forces a
    # split. #2, #3, #4 stay together because #2 → #3 → #4 are
    # consecutive in arc order.
    db_session.add_all(
        [
            _issue(
                1001,
                volume_cv_id=100,
                issue_number="1",
                payload={"id": 1001, "story_arc_credits": [{"id": 7, "name": "A"}]},
            ),
            _issue(
                1002,
                volume_cv_id=100,
                issue_number="2",
                payload={"id": 1002, "story_arc_credits": [{"id": 7, "name": "A"}]},
            ),
            _issue(
                1003,
                volume_cv_id=100,
                issue_number="3",
                payload={"id": 1003, "story_arc_credits": [{"id": 7, "name": "A"}]},
            ),
            _issue(
                1004,
                volume_cv_id=100,
                issue_number="4",
                payload={"id": 1004, "story_arc_credits": [{"id": 7, "name": "A"}]},
            ),
        ]
    )
    db_session.add(
        CvStoryArc(
            cv_id=7,
            name="A",
            raw_payload={
                "id": 7,
                "name": "A",
                "issues": [
                    {"id": 1001, "name": "Vol #1"},
                    {"id": 9000, "name": "External Tie-in"},
                    {"id": 1002, "name": "Vol #2"},
                    {"id": 1003, "name": "Vol #3"},
                    {"id": 1004, "name": "Vol #4"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    class _StubArcCache:
        async def get_volume(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvVolume, cv_id)

        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvStoryArc, cv_id)

    detail = await get_volume_detail(db_session, 100, cv_cache=_StubArcCache())
    segs = detail.gallery_segments
    # Expected segmentation: [#1] | [#2, #3, #4]
    assert [[i.issue_number for i in s.issues] for s in segs] == [
        ["1"],
        ["2", "3", "4"],
    ]
    # Seg 0 (#1): no ← (#1 is first arc member). → arrow points to 9000
    # (the external tie-in), NOT suppressed even though seg 1 also has
    # arc 7 — because the actual next arc member after #1 is 9000, not
    # seg 1's first issue.
    assert segs[0].prev_links == [None]
    assert segs[0].next_links[0] is not None
    assert segs[0].next_links[0].issue_cv_id == 9000
    # Seg 1 (#2-#4): ← points to 9000 (the external tie-in), → has no
    # arrow (#4 is last arc member).
    assert segs[1].prev_links[0] is not None
    assert segs[1].prev_links[0].issue_cv_id == 9000
    assert segs[1].next_links == [None]


async def test_volume_detail_gallery_segments_group_by_arc_fingerprint(db_session):
    """Gallery segments are built in volume order and break whenever the
    arc fingerprint changes. Adjacent-arc continuation suppresses arrows."""
    from datetime import UTC, datetime

    from app.models import CvStoryArc

    db_session.add(_volume(100, "Saga", count_of_issues=5))
    await db_session.flush()
    # Issues 1-5 in volume. Arc A spans #1-4. Arc B spans #3-4 only.
    # Arc A has an extra member 9001 (elsewhere) AFTER #4 — so #4 has a
    # next arc-A member. There's no preceding A member (#1 is first).
    db_session.add_all(
        [
            _issue(
                1001,
                volume_cv_id=100,
                issue_number="1",
                payload={"id": 1001, "story_arc_credits": [{"id": 10, "name": "A"}]},
            ),
            _issue(
                1002,
                volume_cv_id=100,
                issue_number="2",
                payload={"id": 1002, "story_arc_credits": [{"id": 10, "name": "A"}]},
            ),
            _issue(
                1003,
                volume_cv_id=100,
                issue_number="3",
                payload={
                    "id": 1003,
                    "story_arc_credits": [
                        {"id": 10, "name": "A"},
                        {"id": 20, "name": "B"},
                    ],
                },
            ),
            _issue(
                1004,
                volume_cv_id=100,
                issue_number="4",
                payload={
                    "id": 1004,
                    "story_arc_credits": [
                        {"id": 10, "name": "A"},
                        {"id": 20, "name": "B"},
                    ],
                },
            ),
            _issue(1005, volume_cv_id=100, issue_number="5"),  # no arcs
        ]
    )
    db_session.add(
        CvStoryArc(
            cv_id=10,
            name="A",
            raw_payload={
                "id": 10,
                "name": "A",
                "issues": [
                    {"id": 1001, "name": "Issue 1"},
                    {"id": 1002, "name": "Issue 2"},
                    {"id": 1003, "name": "Issue 3"},
                    {"id": 1004, "name": "Issue 4"},
                    {"id": 9001, "name": "External Next"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    db_session.add(
        CvStoryArc(
            cv_id=20,
            name="B",
            raw_payload={
                "id": 20,
                "name": "B",
                "issues": [
                    {"id": 9000, "name": "External Prev"},
                    {"id": 1003, "name": "Issue 3"},
                    {"id": 1004, "name": "Issue 4"},
                    {"id": 9002, "name": "External Next"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    class _StubArcCache:
        async def get_volume(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvVolume, cv_id)

        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvStoryArc, cv_id)

    detail = await get_volume_detail(db_session, 100, cv_cache=_StubArcCache())
    segs = detail.gallery_segments
    # Three segments expected:
    #   seg 0: #1, #2     arcs = {A}
    #   seg 1: #3, #4     arcs = {A, B}
    #   seg 2: #5         arcs = {}
    assert [[i.issue_number for i in s.issues] for s in segs] == [
        ["1", "2"],
        ["3", "4"],
        ["5"],
    ]
    assert [[a.cv_id for a in s.arcs] for s in segs] == [[10], [10, 20], []]

    # Seg 0: arc A's next member is #3 — which is in the immediately
    # following segment that also has arc A — so → arrow is SUPPRESSED.
    # No prev for A either (#1 is first). So both link slots are None.
    assert segs[0].prev_links == [None]
    assert segs[0].next_links == [None]

    # Seg 1: arc A's prev (#2) is in immediately preceding segment (also
    # has A) → suppress ←. Arc A's next (9001) is elsewhere; no following
    # segment has arc A → SHOW → for A. Arc B's prev (9000) is elsewhere;
    # no preceding segment has B → SHOW ← for B. Arc B's next (9002) is
    # elsewhere; following segment doesn't have B → SHOW → for B.
    assert segs[1].prev_links[0] is None                       # A ←: suppressed
    assert segs[1].next_links[0] is not None                   # A →: 9001
    assert segs[1].next_links[0].issue_cv_id == 9001
    assert segs[1].prev_links[1] is not None                   # B ←: 9000
    assert segs[1].prev_links[1].issue_cv_id == 9000
    assert segs[1].next_links[1] is not None                   # B →: 9002
    assert segs[1].next_links[1].issue_cv_id == 9002

    # Seg 2 has no arcs → no link slots
    assert segs[2].prev_links == []
    assert segs[2].next_links == []


async def test_volume_detail_arc_fetch_handles_errors_gracefully(db_session):
    """A ComicVineError from get_story_arc must not crash the page —
    we just fall back to whatever the hydrated issues already knew."""
    from app.comicvine.errors import ComicVineApiError

    db_session.add(_volume(100, "Saga", count_of_issues=2))
    await db_session.flush()
    db_session.add(
        _issue(
            1001,
            volume_cv_id=100,
            issue_number="1",
            payload={
                "id": 1001,
                "story_arc_credits": [{"id": 7, "name": "The Beginning"}],
            },
        )
    )
    db_session.add(_issue(1002, volume_cv_id=100, issue_number="2"))
    await db_session.commit()

    class _FailingCache:
        async def get_volume(self, db, cv_id, *, force_refresh=False):
            # Volume fetch succeeds — the test is specifically about
            # arc fetch failures, so the volume lookup needs to work.
            return await db.get(CvVolume, cv_id)

        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            raise ComicVineApiError("simulated CV failure")

    detail = await get_volume_detail(db_session, 100, cv_cache=_FailingCache())
    assert detail is not None
    arcs_by_issue = {i.issue_number: [a.cv_id for a in i.arc_credits] for i in detail.issues}
    # Issue 1 still has its own arc; issue 2 doesn't get arc 7 fill-in
    # because the fetch failed.
    assert arcs_by_issue == {"1": [7], "2": []}


# ---- get_issue_detail --------------------------------------------------


class _NoCallCache:
    """Stand-in ComicVineCache that just returns whatever's already in DB
    (no network). Asserts that the route doesn't accidentally call CV when
    we don't expect it to."""

    def __init__(self, db_session):
        self.db = db_session

    async def get_issue(self, db, cv_id, *, force_refresh=False):
        from app.models import CvIssue as _CvIssue

        return await db.get(_CvIssue, cv_id)

    async def get_story_arc(self, db, cv_id, *, force_refresh=False):
        # ``get_issue_detail`` calls this to build the cross-volume arc
        # branches for the issue rail. The stub returns whatever the
        # test happens to have seeded in DB (or ``None``), no network.
        from app.models import CvStoryArc

        return await db.get(CvStoryArc, cv_id)


async def test_issue_detail_returns_neighbors(db_session):
    await _make_library(db_session)
    detail = await get_issue_detail(db_session, _NoCallCache(db_session), 1002)
    assert detail is not None
    # Saga #2 → prev #1, next #3
    assert detail.prev_neighbor is not None
    assert detail.prev_neighbor.issue_number == "1"
    assert detail.next_neighbor is not None
    assert detail.next_neighbor.issue_number == "3"


async def test_issue_detail_no_prev_for_first_issue(db_session):
    await _make_library(db_session)
    detail = await get_issue_detail(db_session, _NoCallCache(db_session), 1001)
    assert detail.prev_neighbor is None
    assert detail.next_neighbor.issue_number == "2"


async def test_issue_detail_returns_matched_files(db_session):
    await _make_library(db_session)
    detail = await get_issue_detail(db_session, _NoCallCache(db_session), 1001)
    assert [f.path for f in detail.matched_files] == ["/library/saga1.cbz"]
    # file_id is carried so the issue page can build a "Fix match" link.
    assert all(f.file_id is not None for f in detail.matched_files)


async def test_issue_detail_returns_none_for_unknown(db_session):
    assert (
        await get_issue_detail(db_session, _NoCallCache(db_session), 99999) is None
    )


async def test_issue_detail_extracts_credits_from_payload(db_session):
    db_session.add(_volume(100, "Saga", count_of_issues=1))
    await db_session.flush()  # volume must exist before the issue references it
    db_session.add(
        _issue(
            1001,
            volume_cv_id=100,
            issue_number="1",
            payload={
                "id": 1001,
                "person_credits": [
                    {"id": 5, "name": "BK Vaughan", "role": "writer"},
                    {"id": 6, "name": "F Staples", "role": "artist"},
                ],
                "character_credits": [{"id": 21, "name": "Alana"}],
                "story_arc_credits": [{"id": 7, "name": "The Beginning"}],
                "team_credits": [],
            },
        )
    )
    await db_session.commit()
    detail = await get_issue_detail(db_session, _NoCallCache(db_session), 1001)
    assert [p.name for p in detail.persons] == ["BK Vaughan", "F Staples"]
    assert detail.persons[0].role == "writer"
    assert [c.name for c in detail.characters] == ["Alana"]
    assert [a.name for a in detail.story_arcs] == ["The Beginning"]


# ---- get_arc_detail ----------------------------------------------------


async def test_arc_detail_assembles_members_across_volumes(db_session):
    """The arc page assembles issue rows for every arc member,
    enriches them from cv_issues + cv_volumes where available, and
    falls back to the arc-payload nested data for members we don't
    have cached. Owned status comes from file_matches."""
    from app.models import CvStoryArc

    db_session.add_all([_publisher(10, "Marvel"), _publisher(31, "Image")])
    await db_session.flush()
    db_session.add_all([
        _volume(100, "Saga", year=2012, publisher_cv_id=31, count_of_issues=3),
        _volume(200, "Other Vol", year=2013, publisher_cv_id=10),
    ])
    await db_session.flush()
    db_session.add_all([
        # Saga #1 and #2 are in the arc; Saga #3 isn't.
        _issue(1001, volume_cv_id=100, issue_number="1"),
        _issue(1002, volume_cv_id=100, issue_number="2"),
        # Other Vol #5 also in the arc.
        _issue(2005, volume_cv_id=200, issue_number="5"),
    ])
    files = [_file(f"{i:064x}") for i in range(2)]
    db_session.add_all(files)
    await db_session.flush()
    db_session.add_all([
        _location(files[0].id, "/library/saga1.cbz"),
        _location(files[1].id, "/library/other5.cbz"),
        _match(files[0].id, 1001),   # own Saga #1
        _match(files[1].id, 2005),   # own Other Vol #5
        # Saga #2 missing.
    ])
    # Arc payload: 4 members in arc-reading order. Member 4 is a
    # cross-over from a volume we don't have cached at all — should
    # fall back to the nested data.
    db_session.add(
        CvStoryArc(
            cv_id=7,
            name='"Big Book" The Arc Name',
            raw_payload={
                "id": 7,
                "name": '"Big Book" The Arc Name',
                "description": "<p>An arc.</p>",
                "issues": [
                    {"id": 1001, "name": "Saga #1", "issue_number": "1"},
                    {"id": 2005, "name": "Crossover", "issue_number": "5"},
                    {"id": 1002, "name": "Saga #2", "issue_number": "2"},
                    {
                        "id": 9999,
                        "name": "Off-the-grid",
                        "issue_number": "1",
                        "volume": {"id": 500, "name": "Uncached Vol", "start_year": "2015"},
                    },
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    class _StubCache:
        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            return await db.get(CvStoryArc, cv_id)

    detail = await get_arc_detail(db_session, _StubCache(), 7)
    assert detail is not None
    # CV's quoted-prefix is parsed out.
    assert detail.name == "The Arc Name"
    assert detail.primary_book == "Big Book"
    assert detail.description == "<p>An arc.</p>"

    # All four members surfaced in arc-reading order.
    assert [i.cv_id for i in detail.issues] == [1001, 2005, 1002, 9999]
    # Cached members carry volume metadata; the uncached one falls
    # back to the nested data CV gave us.
    by_id = {i.cv_id: i for i in detail.issues}
    assert by_id[1001].volume_name == "Saga"
    assert by_id[1001].volume_year == 2012
    assert by_id[1001].owned is True
    assert by_id[1002].owned is False  # not matched
    assert by_id[2005].volume_name == "Other Vol"
    assert by_id[2005].owned is True
    assert by_id[9999].volume_cv_id == 500
    assert by_id[9999].volume_name == "Uncached Vol"
    assert by_id[9999].volume_year == 2015
    assert by_id[9999].owned is False

    # Totals reflect every member, owned reflects only the matched ones.
    assert detail.total_count == 4
    assert detail.owned_count == 2

    # Gallery shelves: one per volume, in the order each volume first
    # appears in arc-reading order. Saga appears at position 0 (Saga
    # #1), Other Vol at 1 (Crossover), Uncached at 3 — note Saga's
    # second issue (#2 at position 2) STILL goes to the Saga shelf
    # because we group by volume, not by position.
    assert [s.volume_cv_id for s in detail.volume_shelves] == [100, 200, 500]
    saga_shelf = detail.volume_shelves[0]
    assert [i.cv_id for i in saga_shelf.issues] == [1001, 1002]


async def test_arc_detail_returns_none_when_arc_missing(db_session):
    """If the cv_cache can't return an arc (None / not found), the
    service short-circuits to None — the route turns that into a 404."""

    class _MissingCache:
        async def get_story_arc(self, db, cv_id, *, force_refresh=False):
            return None

    detail = await get_arc_detail(db_session, _MissingCache(), 9999)
    assert detail is None


# ---- list_recently_added -----------------------------------------------


async def test_recently_added_groups_by_volume(db_session):
    await _make_library(db_session)
    rows = await list_recently_added(db_session, limit=10)
    # _make_library matches two issues into Saga and one into X-Men;
    # grouped by volume that's two entries, not three issue rows.
    assert len(rows) == 2
    by_id = {r.cv_id: r for r in rows}
    assert by_id[100].kind == "cv"
    assert by_id[100].issue_count == 2  # Saga: #1 + #2
    assert by_id[100].detail_url == "/volume/100"
    assert by_id[200].issue_count == 1  # X-Men: #1
    # Most-recently-matched volume first.
    matched_at_values = [r.matched_at for r in rows]
    assert matched_at_values == sorted(matched_at_values, reverse=True)


# ---- Phase 11C: local volumes & issues ---------------------------------
#
# Local volumes/issues (Phase 11) are user-authored library content for
# comics with no ComicVine record. 11C merges them into the browse
# surfaces: /library, the /local/volume + /local/issue page builders,
# and the home page's recently-added list. These helpers build the
# local-entity equivalent of ``_make_library``'s CV rows.


def _local_volume(name, *, year=2019, publisher_name=None) -> LocalVolume:
    # Explicit id so issues/matches can reference it before the flush.
    return LocalVolume(
        id=uuid.uuid4(), name=name, year=year, publisher_name=publisher_name
    )


def _local_issue(
    local_volume_id, *, issue_number, name=None, cover_date=None
) -> LocalIssue:
    return LocalIssue(
        id=uuid.uuid4(),
        local_volume_id=local_volume_id,
        issue_number=issue_number,
        name=name,
        cover_date=cover_date,
    )


def _local_match(file_id, local_issue_id, *, matched_at=None) -> FileMatch:
    """A file_matches row resolved to a local issue — status/source LOCAL,
    the CV-issue target left NULL (the single-target invariant)."""
    return FileMatch(
        file_id=file_id,
        issue_cv_id=None,
        local_issue_id=local_issue_id,
        confidence=None,
        status=MatchStatus.LOCAL,
        source=MatchSource.LOCAL,
        matched_at=matched_at or datetime.now(tz=UTC),
    )


async def _make_local_volume(
    db_session,
    name,
    issue_numbers,
    *,
    year=2019,
    publisher_name=None,
    sha_offset=100,
    matched_at=None,
):
    """Seed one local volume with an issue + a matched file per entry in
    ``issue_numbers``. Returns ``(local_volume, [local_issues...])`` with
    the issues in the order given. ``sha_offset`` keeps the file shas
    distinct from ``_make_library``'s 0/1/2."""
    lv = _local_volume(name, year=year, publisher_name=publisher_name)
    db_session.add(lv)
    issues = [_local_issue(lv.id, issue_number=n) for n in issue_numbers]
    db_session.add_all(issues)
    files = [_file(f"{sha_offset + i:064x}") for i in range(len(issues))]
    db_session.add_all(files)
    await db_session.flush()
    for idx, (issue, f) in enumerate(zip(issues, files, strict=True)):
        db_session.add(_location(f.id, f"/library/{name}-{idx}.cbz"))
        db_session.add(_local_match(f.id, issue.id, matched_at=matched_at))
    await db_session.commit()
    return lv, issues


async def test_local_volume_appears_in_library(db_session):
    lv, _issues = await _make_local_volume(
        db_session, "My Indie Series", ["1", "2"],
        year=2019, publisher_name="self-published",
    )
    rows, total = await list_library_volumes(db_session)
    assert total == 1 and len(rows) == 1
    row = rows[0]
    assert row.kind == "local"
    assert row.local_id == lv.id
    assert row.cv_id == 0
    assert row.name == "My Indie Series"
    assert row.year == 2019
    assert row.publisher_name == "self-published"
    assert row.owned_count == 2
    # No CV issue total for a local volume — "missing" is undefined.
    assert row.count_of_issues is None
    assert row.missing_count is None
    assert row.detail_url == f"/local/volume/{lv.id}"
    # Cover is the first issue's file, served by the file-cover route.
    assert row.cover_url is not None
    assert row.cover_url.startswith("/review/file/")
    assert row.cover_url.endswith("/cover")


async def test_library_merges_and_sorts_cv_with_local(db_session):
    await _make_library(db_session)  # CV volumes: Saga (100), X-Men (200)
    await _make_local_volume(db_session, "Indie Press", ["1"])
    rows, total = await list_library_volumes(db_session)  # default name sort
    assert total == 3
    # The local volume interleaves with the CV volumes by name.
    assert [r.name for r in rows] == ["Indie Press", "Saga", "X-Men"]
    assert [r.kind for r in rows] == ["local", "cv", "cv"]


async def test_publisher_facet_excludes_local_volumes(db_session):
    await _make_library(db_session)
    await _make_local_volume(db_session, "Indie Press", ["1"])
    # A CV-publisher facet can't match a free-text local publisher, so
    # local volumes drop out of a publisher-filtered list entirely.
    rows, total = await list_library_volumes(
        db_session, LibraryFilters(publisher_cv_id=31)
    )
    assert total == 1
    assert [r.cv_id for r in rows] == [100]
    assert all(r.kind == "cv" for r in rows)


async def test_has_missing_only_excludes_local_volumes(db_session):
    await _make_library(db_session)
    await _make_local_volume(db_session, "Indie Press", ["1"])
    # A local volume has no CV issue total, so "missing" is undefined —
    # has_missing_only drops them.
    rows, _total = await list_library_volumes(
        db_session, LibraryFilters(has_missing_only=True)
    )
    assert all(r.kind == "cv" for r in rows)
    assert "Indie Press" not in [r.name for r in rows]


async def test_pagination_spans_cv_and_local(db_session):
    await _make_library(db_session)  # Saga, X-Men
    await _make_local_volume(db_session, "Indie Press", ["1"])
    # Name sort across the union is: Indie Press, Saga, X-Men.
    page0, total = await list_library_volumes(db_session, limit=1, offset=0)
    page1, _ = await list_library_volumes(db_session, limit=1, offset=1)
    page2, _ = await list_library_volumes(db_session, limit=1, offset=2)
    assert total == 3
    assert [page0[0].name, page1[0].name, page2[0].name] == [
        "Indie Press", "Saga", "X-Men",
    ]
    assert page0[0].kind == "local"


async def test_get_local_volume_detail(db_session):
    # Issues created out of order — the detail builder must sort them.
    lv, _issues = await _make_local_volume(
        db_session, "My Indie Series", ["2", "1", "10"]
    )
    detail = await get_local_volume_detail(db_session, lv.id)
    assert detail is not None
    assert detail.name == "My Indie Series"
    assert [i.issue_number for i in detail.issues] == ["1", "2", "10"]
    # Every issue has a matched file, so every row has a cover.
    assert all(i.cover_file_id is not None for i in detail.issues)
    # The volume's representative cover is its first issue's file.
    assert detail.cover_file_id == detail.issues[0].cover_file_id


async def test_get_local_volume_detail_none_for_unknown(db_session):
    assert await get_local_volume_detail(db_session, uuid.uuid4()) is None


async def test_get_local_issue_detail(db_session):
    lv, issues = await _make_local_volume(
        db_session, "My Indie Series", ["1", "2", "3"]
    )
    detail = await get_local_issue_detail(db_session, issues[1].id)  # #2
    assert detail is not None
    assert detail.issue_number == "2"
    assert detail.volume_id == lv.id
    assert detail.volume_name == "My Indie Series"
    # One file on disk, with its path.
    assert len(detail.files) == 1
    assert detail.files[0].path.startswith("/library/")
    assert detail.cover_file_id == detail.files[0].file_id
    # Neighbours are the issue-number siblings within the volume.
    assert detail.prev_issue is not None
    assert detail.prev_issue.issue_number == "1"
    assert detail.next_issue is not None
    assert detail.next_issue.issue_number == "3"


async def test_get_local_issue_detail_boundary_neighbors(db_session):
    _lv, issues = await _make_local_volume(db_session, "Mini", ["1", "2"])
    first = await get_local_issue_detail(db_session, issues[0].id)
    last = await get_local_issue_detail(db_session, issues[1].id)
    # The first issue has no previous; the last has no next.
    assert first.prev_issue is None
    assert first.next_issue is not None and first.next_issue.issue_number == "2"
    assert last.next_issue is None
    assert last.prev_issue is not None and last.prev_issue.issue_number == "1"


async def test_get_local_issue_detail_none_for_unknown(db_session):
    assert await get_local_issue_detail(db_session, uuid.uuid4()) is None


async def test_recently_added_includes_local_volumes(db_session):
    """Recently-added groups by volume across both CV and local."""
    # A CV volume matched in 2020.
    db_session.add(_publisher(31, "Image"))
    await db_session.flush()
    db_session.add(_volume(100, "Saga", publisher_cv_id=31))
    await db_session.flush()
    db_session.add(_issue(1001, volume_cv_id=100, issue_number="1"))
    cv_file = _file(f"{1:064x}")
    db_session.add(cv_file)
    await db_session.flush()
    db_session.add(_location(cv_file.id, "/library/saga1.cbz"))
    db_session.add(
        FileMatch(
            file_id=cv_file.id,
            issue_cv_id=1001,
            confidence=None,
            status=MatchStatus.AUTO,
            source=MatchSource.FILENAME,
            matched_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.commit()
    # A local volume catalogued in 2023 — newer than the CV match.
    await _make_local_volume(
        db_session, "Indie", ["1"], matched_at=datetime(2023, 6, 1, tzinfo=UTC)
    )
    rows = await list_recently_added(db_session, limit=10)
    assert len(rows) == 2
    # Newest first: the 2023 local volume leads the 2020 CV volume.
    assert rows[0].kind == "local"
    assert rows[0].name == "Indie"
    assert rows[0].issue_count == 1
    assert rows[0].cover_url.startswith("/review/file/")
    assert rows[0].detail_url.startswith("/local/volume/")
    assert rows[1].kind == "cv"
    assert rows[1].name == "Saga"


# ---- Phase 11E: editing local metadata ---------------------------------


async def test_update_local_volume(db_session):
    lv, _issues = await _make_local_volume(
        db_session, "Typo Name", ["1"], year=2018, publisher_name="oops",
    )
    result = await update_local_volume(
        db_session, lv.id,
        name="Fixed Name", year=2020, publisher_name="Real Publisher",
        description="A self-published mini-series.",
    )
    assert result is not None
    detail = await get_local_volume_detail(db_session, lv.id)
    assert detail.name == "Fixed Name"
    assert detail.year == 2020
    assert detail.publisher_name == "Real Publisher"
    assert detail.description == "A self-published mini-series."


async def test_update_local_volume_blank_fields_clear_to_none(db_session):
    lv, _issues = await _make_local_volume(
        db_session, "Indie", ["1"], year=2018, publisher_name="self-published",
    )
    # Give it a description first so the clear below is meaningful.
    await update_local_volume(
        db_session, lv.id, name="Indie", year=2018,
        publisher_name="self-published", description="A blurb.",
    )
    # Blank year / whitespace publisher + description normalise to NULL.
    await update_local_volume(
        db_session, lv.id, name="Indie", year=None,
        publisher_name="   ", description="   ",
    )
    detail = await get_local_volume_detail(db_session, lv.id)
    assert detail.year is None
    assert detail.publisher_name is None
    assert detail.description is None


async def test_update_local_volume_none_for_unknown(db_session):
    result = await update_local_volume(
        db_session, uuid.uuid4(),
        name="X", year=None, publisher_name=None, description=None,
    )
    assert result is None


async def test_update_local_issue(db_session):
    _lv, issues = await _make_local_volume(db_session, "Indie", ["1"])
    result = await update_local_issue(
        db_session, issues[0].id,
        issue_number="2", name="Renamed Chapter", cover_date=date(2019, 5, 1),
    )
    assert result is not None
    detail = await get_local_issue_detail(db_session, issues[0].id)
    assert detail.issue_number == "2"
    assert detail.name == "Renamed Chapter"
    assert detail.cover_date == date(2019, 5, 1)


async def test_update_local_issue_blank_fields_clear_to_none(db_session):
    _lv, issues = await _make_local_volume(db_session, "Indie", ["1"])
    await update_local_issue(
        db_session, issues[0].id,
        issue_number="  ", name="", cover_date=None,
    )
    detail = await get_local_issue_detail(db_session, issues[0].id)
    assert detail.issue_number is None
    assert detail.name is None
    assert detail.cover_date is None


async def test_update_local_issue_none_for_unknown(db_session):
    result = await update_local_issue(
        db_session, uuid.uuid4(),
        issue_number="1", name=None, cover_date=None,
    )
    assert result is None


async def test_merge_local_volumes(db_session):
    keep, _ = await _make_local_volume(
        db_session, "Keep", ["1", "2"], sha_offset=100,
    )
    dupe, _ = await _make_local_volume(
        db_session, "Dupe", ["3", "4"], sha_offset=200,
    )
    result = await merge_local_volumes(
        db_session, target_id=keep.id, source_id=dupe.id,
    )
    assert result is not None
    assert result.moved_issue_count == 2
    # The source volume is gone.
    assert await get_local_volume_detail(db_session, dupe.id) is None
    # The target absorbed both of the source's issues.
    detail = await get_local_volume_detail(db_session, keep.id)
    assert [i.issue_number for i in detail.issues] == ["1", "2", "3", "4"]


async def test_merge_local_volumes_into_self_rejected(db_session):
    keep, _ = await _make_local_volume(
        db_session, "Solo", ["1"], sha_offset=100,
    )
    result = await merge_local_volumes(
        db_session, target_id=keep.id, source_id=keep.id,
    )
    assert result is None
    # The volume is untouched.
    assert await get_local_volume_detail(db_session, keep.id) is not None


async def test_merge_local_volumes_unknown_source(db_session):
    keep, _ = await _make_local_volume(
        db_session, "Keep", ["1"], sha_offset=100,
    )
    result = await merge_local_volumes(
        db_session, target_id=keep.id, source_id=uuid.uuid4(),
    )
    assert result is None


async def test_merge_local_volumes_reflected_in_library(db_session):
    keep, _ = await _make_local_volume(
        db_session, "Keep", ["1", "2"], sha_offset=100,
    )
    dupe, _ = await _make_local_volume(
        db_session, "Dupe", ["3"], sha_offset=200,
    )
    await merge_local_volumes(
        db_session, target_id=keep.id, source_id=dupe.id,
    )
    rows, total = await list_library_volumes(db_session)
    # Only the surviving volume is left, owning all three issues.
    assert total == 1
    assert rows[0].local_id == keep.id
    assert rows[0].owned_count == 3


# ---- Library type (classified-format) facet ----------------------------


async def _seed_format_library(db_session) -> None:
    """Two CV volumes — an ongoing series (20 issues) and a limited
    series (5) — each with one owned issue, so both show in /library."""
    db_session.add(_publisher(31, "Image"))
    await db_session.flush()
    db_session.add_all([
        _volume(100, "Ongoing One", year=2012, publisher_cv_id=31,
                count_of_issues=20),
        _volume(200, "Limited One", year=2015, publisher_cv_id=31,
                count_of_issues=5),
    ])
    await db_session.flush()
    db_session.add_all([
        _issue(1001, volume_cv_id=100, issue_number="1"),
        _issue(2001, volume_cv_id=200, issue_number="1"),
    ])
    files = [_file(f"{i:064x}") for i in range(2)]
    db_session.add_all(files)
    await db_session.flush()
    db_session.add_all([
        _location(files[0].id, "/library/a.cbz"),
        _location(files[1].id, "/library/b.cbz"),
        _match(files[0].id, 1001),
        _match(files[1].id, 2001),
    ])
    await db_session.commit()


async def test_library_rows_carry_classified_format(db_session):
    await _seed_format_library(db_session)
    rows, _total = await list_library_volumes(db_session)
    by_id = {r.cv_id: r for r in rows}
    # The format is classified onto every row for the card's badge.
    assert by_id[100].format == "ongoing"  # 20 issues
    assert by_id[200].format == "limited"  # 5 issues


async def test_library_format_facet_filters(db_session):
    await _seed_format_library(db_session)
    rows, total = await list_library_volumes(
        db_session, LibraryFilters(format="limited")
    )
    assert total == 1
    assert [r.cv_id for r in rows] == [200]
    rows, total = await list_library_volumes(
        db_session, LibraryFilters(format="ongoing")
    )
    assert total == 1
    assert [r.cv_id for r in rows] == [100]


async def test_library_format_facet_excludes_local_volumes(db_session):
    await _seed_format_library(db_session)
    # A local volume has no classified format — the facet is CV-only.
    await _make_local_volume(db_session, "Local Series", ["1"], sha_offset=300)
    rows, total = await list_library_volumes(
        db_session, LibraryFilters(format="ongoing")
    )
    assert total == 1
    assert all(r.kind == "cv" for r in rows)
    assert [r.cv_id for r in rows] == [100]


# ---- Reading progress (Phase 6) ----------------------------------------


async def _finish_issue_1001(db_session, user_id) -> None:
    """Mark the file matched to Saga #1 (issue 1001) finished for a user."""
    file_id = (
        await db_session.execute(
            select(FileMatch.file_id).where(FileMatch.issue_cv_id == 1001)
        )
    ).scalar_one()
    db_session.add(
        ReadProgress(
            user_id=user_id,
            file_id=file_id,
            page=19,
            page_count=20,
            finished_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()


async def test_list_library_volumes_read_count(db_session):
    await _make_library(db_session)
    user = User(username="reader", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    await _finish_issue_1001(db_session, user.id)

    rows, _ = await list_library_volumes(db_session, user_id=user.id)
    by_id = {r.cv_id: r for r in rows}
    assert by_id[100].read_count == 1  # Saga — one finished issue
    assert by_id[200].read_count == 0  # X-Men — nothing read

    # Without a user, read_count stays 0.
    rows, _ = await list_library_volumes(db_session)
    assert all(r.read_count == 0 for r in rows)


async def test_get_volume_detail_finished_issue_count(db_session):
    await _make_library(db_session)
    user = User(username="reader", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    await _finish_issue_1001(db_session, user.id)

    detail = await get_volume_detail(db_session, 100, user_id=user.id)
    assert detail is not None
    assert detail.finished_issue_count == 1
    assert detail.owned_issue_count == 2  # Saga #1 and #2 are owned

    # Without a user the reading count is 0.
    detail = await get_volume_detail(db_session, 100)
    assert detail is not None
    assert detail.finished_issue_count == 0


# ---- Character page -----------------------------------------------------


class _StubCharacterCache:
    """Minimal ComicVineCache stand-in — get_character returns a row."""

    def __init__(self, character):
        self._character = character

    async def get_character(self, db, cv_id, *, force_refresh=False):
        return self._character


async def test_get_character_detail_appearances(db_session):
    # _make_library: Saga (vol 100) has issues 1001/1002/1003; files are
    # matched to 1001 and 1002 (owned), 1003 is missing.
    await _make_library(db_session)
    character = CvCharacter(
        cv_id=500,
        name="Alana",
        raw_payload={
            "id": 500,
            "name": "Alana",
            "deck": "A soldier.",
            "issue_credits": [
                {"id": 1001, "name": "Saga #1"},
                {"id": 1002, "name": "Saga #2"},
                {"id": 9999, "name": "An uncached appearance"},
            ],
        },
        fetched_at=datetime.now(tz=UTC),
        # The volume list has already been scraped.
        volumes_scraped_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    # The scraped volume-appearance rows (the issues-cover scrape
    # output) — the scraped names are deliberately terse "(gallery)"
    # variants to prove the cv_volumes title overrides them.
    db_session.add_all(
        [
            _char_volume(500, 200, "X-Men (gallery)", position=0),
            _char_volume(500, 100, "Saga (gallery)", cover_url="https://x/s.jpg",
                         position=1),
        ]
    )
    await db_session.commit()

    detail = await get_character_detail(
        db_session, _StubCharacterCache(character), 500
    )
    assert detail is not None
    assert detail.name == "Alana"
    # The completeness ring counts issue appearances from issue_credits:
    # 3 appearances, 1001 + 1002 have matches.
    assert detail.total_count == 3
    assert detail.owned_count == 2
    # The Appearances tab is the scraped volume list, sorted by name.
    assert detail.volumes_scraping is False
    assert detail.appearance_volumes_total == 2
    assert [v.cv_id for v in detail.appearance_volumes] == [100, 200]
    saga = detail.appearance_volumes[0]
    # vol 100 is cached by _make_library -> the card is enriched, and
    # the canonical cv_volumes title replaces the scraped "(gallery)".
    assert saga.name == "Saga"
    assert saga.is_hydrated is True
    assert saga.year == 2012


async def test_get_character_detail_volumes_scraping(db_session):
    # A character whose volume list hasn't been scraped yet: the
    # Appearances tab is in the "building" state — volumes_scraping is
    # True and there are no cards. The ring still works off issue_credits.
    await _make_library(db_session)
    character = CvCharacter(
        cv_id=504,
        name="Ghost",
        raw_payload={
            "id": 504,
            "name": "Ghost",
            "issue_credits": [{"id": 1001}, {"id": 1002}, {"id": 1003}],
        },
        fetched_at=datetime.now(tz=UTC),
        volumes_scraped_at=None,  # never scraped
    )
    db_session.add(character)
    await db_session.commit()

    detail = await get_character_detail(
        db_session, _StubCharacterCache(character), 504
    )
    assert detail.volumes_scraping is True
    assert detail.appearance_volumes == []
    assert detail.appearance_volumes_total == 0
    # 1001 + 1002 are owned in _make_library; 1003 is missing.
    assert detail.total_count == 3
    assert detail.owned_count == 2


async def test_get_character_detail_info(db_session):
    # The "General Information" sidebar card is parsed from the CV
    # character payload into CharacterDetail.info.
    await _make_library(db_session)
    # A hydrated issue (carrying a cover image) for the first
    # appearance — get_character_detail pulls its cover thumbnail.
    db_session.add(
        _issue(
            9001,
            volume_cv_id=100,
            issue_number="8",
            name="Turbulence",
            payload={
                "id": 9001,
                "name": "Turbulence",
                "image": {"thumb_url": "https://example.com/fa.jpg"},
            },
        )
    )
    await db_session.commit()
    character = CvCharacter(
        cv_id=510,
        name="3-D Man",
        raw_payload={
            "id": 510,
            "name": "3-D Man",
            "real_name": "Delroy Garrett, Jr.",
            # CV gives aliases as one newline-separated string.
            "aliases": "Triathlon\nThree Dimensional Man\nTriathlon",
            "gender": 1,
            "origin": {"id": 1, "name": "Human"},
            "creators": [
                {"id": 700, "name": "Kurt Busiek"},
                {"id": 701, "name": "George Pérez"},
            ],
            "first_appeared_in_issue": {
                "id": 9001, "name": "Turbulence", "issue_number": "8",
            },
            "issues_died_in": [
                {"id": 2001, "name": "Final Battle", "issue_number": "1"},
            ],
            "count_of_issue_appearances": 211,
            "powers": [
                {"id": 1, "name": "Agility"},
                {"id": 2, "name": "Super Speed"},
            ],
            "issue_credits": [{"id": 1001, "name": "Saga #1"}],
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    await db_session.commit()

    detail = await get_character_detail(
        db_session, _StubCharacterCache(character), 510
    )
    info = detail.info
    assert info.real_name == "Delroy Garrett, Jr."
    # Aliases split from the newline string, trimmed and de-duped.
    assert info.aliases == ["Triathlon", "Three Dimensional Man"]
    assert info.gender == "Male"  # CV gender code 1
    assert info.character_type == "Human"  # CV "origin"
    # creators / appearance_count are parsed but not shown in the
    # sidebar card — kept on CharacterInfo for other uses.
    assert [c.name for c in info.creators] == ["Kurt Busiek", "George Pérez"]
    assert info.creators[0].cv_id == 700
    assert info.appearance_count == 211
    assert info.first_appearance is not None
    assert info.first_appearance.cv_id == 9001
    assert info.first_appearance.issue_number == "8"
    # The first-appearance issue's cached cover thumbnail is pulled in.
    assert info.first_appearance.cover_url == "https://example.com/fa.jpg"
    assert info.first_appearance.is_hydrated is True
    # issues_died_in -> CharacterInfo.died_in (issue refs).
    assert [d.cv_id for d in info.died_in] == [2001]
    assert info.died_in[0].name == "Final Battle"
    assert info.died_in[0].issue_number == "1"
    assert info.powers == ["Agility", "Super Speed"]


async def test_get_character_detail_friends(db_session):
    # character_friends -> a paginated grid of CharacterCards; cached
    # friends carry a portrait, uncached ones don't.
    await _make_library(db_session)
    # Two friends are already cached (with cover images); two are not.
    db_session.add_all(
        [
            CvCharacter(
                cv_id=601,
                name="Iron Man",
                raw_payload={
                    "id": 601, "name": "Iron Man",
                    "image": {"icon_url": "https://example.com/601.jpg"},
                },
                fetched_at=datetime.now(tz=UTC),
            ),
            CvCharacter(
                cv_id=602,
                name="Thor",
                raw_payload={
                    "id": 602, "name": "Thor",
                    "image": {"icon_url": "https://example.com/602.jpg"},
                },
                fetched_at=datetime.now(tz=UTC),
            ),
        ]
    )
    character = CvCharacter(
        cv_id=530,
        name="Captain America",
        raw_payload={
            "id": 530,
            "name": "Captain America",
            "character_friends": [
                {"id": 601, "name": "Iron Man"},
                {"id": 602, "name": "Thor"},
                {"id": 603, "name": "Hawkeye"},
                {"id": 604, "name": "Black Widow"},
                {"id": 601, "name": "Iron Man"},  # CV repeat -> de-duped
            ],
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    await db_session.commit()
    cache = _StubCharacterCache(character)

    # Page 1, 2 per page: 4 unique friends -> 2 pages.
    p1 = await get_character_detail(
        db_session, cache, 530, friends_page=1, page_size=2
    )
    assert p1.friends_total == 4  # the duplicate 601 collapsed
    assert p1.friends_page == 1 and p1.friends_page_count == 2
    assert [f.cv_id for f in p1.friends] == [601, 602]
    # 601 / 602 are cached -> hydrated, with an icon avatar.
    assert all(f.is_hydrated for f in p1.friends)
    assert p1.friends[0].icon_url == "https://example.com/601.jpg"

    # Page 2 -> the uncached friends: no avatar, not hydrated.
    p2 = await get_character_detail(
        db_session, cache, 530, friends_page=2, page_size=2
    )
    assert [f.cv_id for f in p2.friends] == [603, 604]
    assert not any(f.is_hydrated for f in p2.friends)
    assert p2.friends[0].icon_url is None
    assert p2.friends[0].name == "Hawkeye"

    # An out-of-range friends page clamps to the last.
    p9 = await get_character_detail(
        db_session, cache, 530, friends_page=9, page_size=2
    )
    assert p9.friends_page == 2


async def test_get_character_detail_no_friends(db_session):
    await _make_library(db_session)
    character = CvCharacter(
        cv_id=531,
        name="Loner",
        raw_payload={"id": 531, "name": "Loner"},
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    await db_session.commit()
    detail = await get_character_detail(
        db_session, _StubCharacterCache(character), 531
    )
    assert detail.friends_total == 0
    assert detail.friends == []
    assert detail.friends_page_count == 1


async def test_get_character_detail_enemies(db_session):
    # character_enemies -> the Enemies tab, paginated exactly like
    # Friends (both go through the shared _character_card_page).
    await _make_library(db_session)
    db_session.add(
        CvCharacter(
            cv_id=611,
            name="Red Skull",
            raw_payload={
                "id": 611, "name": "Red Skull",
                "image": {"icon_url": "https://example.com/611.jpg"},
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    character = CvCharacter(
        cv_id=540,
        name="Captain America",
        raw_payload={
            "id": 540,
            "name": "Captain America",
            "character_enemies": [
                {"id": 611, "name": "Red Skull"},
                {"id": 612, "name": "Baron Zemo"},
                {"id": 613, "name": "Crossbones"},
            ],
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    await db_session.commit()
    cache = _StubCharacterCache(character)

    # 3 enemies, 2 per page -> 2 pages.
    p1 = await get_character_detail(
        db_session, cache, 540, enemies_page=1, page_size=2
    )
    assert p1.enemies_total == 3
    assert p1.enemies_page == 1 and p1.enemies_page_count == 2
    assert [e.cv_id for e in p1.enemies] == [611, 612]
    # 611 is cached -> hydrated with an icon avatar; 612 is not.
    assert p1.enemies[0].is_hydrated is True
    assert p1.enemies[0].icon_url == "https://example.com/611.jpg"
    assert p1.enemies[1].is_hydrated is False

    p2 = await get_character_detail(
        db_session, cache, 540, enemies_page=2, page_size=2
    )
    assert [e.cv_id for e in p2.enemies] == [613]
    assert p2.enemies_page == 2
    # The friends and enemies pagers are independent.
    assert p2.friends_total == 0


async def test_get_character_detail_teams(db_session):
    # teams -> the Teams tab, paginated like Friends/Enemies; the cards
    # enrich from cv_teams (not cv_characters).
    await _make_library(db_session)
    db_session.add(
        CvTeam(
            cv_id=801,
            name="Avengers",
            raw_payload={
                "id": 801, "name": "Avengers",
                "image": {"icon_url": "https://example.com/801.jpg"},
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    character = CvCharacter(
        cv_id=550,
        name="Captain America",
        raw_payload={
            "id": 550,
            "name": "Captain America",
            "teams": [
                {"id": 801, "name": "Avengers"},
                {"id": 802, "name": "Invaders"},
                {"id": 803, "name": "Secret Avengers"},
            ],
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    await db_session.commit()
    cache = _StubCharacterCache(character)

    p1 = await get_character_detail(
        db_session, cache, 550, teams_page=1, page_size=2
    )
    assert p1.teams_total == 3
    assert p1.teams_page == 1 and p1.teams_page_count == 2
    assert [t.cv_id for t in p1.teams] == [801, 802]
    # 801 is a cached cv_teams row -> hydrated with an icon avatar.
    assert p1.teams[0].is_hydrated is True
    assert p1.teams[0].icon_url == "https://example.com/801.jpg"
    assert p1.teams[1].is_hydrated is False

    p2 = await get_character_detail(
        db_session, cache, 550, teams_page=2, page_size=2
    )
    assert [t.cv_id for t in p2.teams] == [803]


class _StubTeamCache:
    """Minimal ComicVineCache stand-in — get_team returns a row."""

    def __init__(self, team):
        self._team = team

    async def get_team(self, db, cv_id, *, force_refresh=False):
        return self._team


async def test_get_team_detail(db_session):
    # A team page: hero facts + a paginated members grid built from the
    # team's `characters`, enriched from cv_characters.
    db_session.add(
        CvCharacter(
            cv_id=901,
            name="Iron Man",
            raw_payload={
                "id": 901, "name": "Iron Man",
                "image": {"icon_url": "https://example.com/901.jpg"},
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    # A hydrated issue (carrying a cover image) for the team's first
    # appearance — get_team_detail pulls its cover thumbnail.
    db_session.add(
        _issue(
            9100,
            volume_cv_id=None,
            issue_number="1",
            name="Avengers",
            payload={
                "id": 9100,
                "name": "Avengers",
                "image": {"thumb_url": "https://example.com/fa-team.jpg"},
            },
        )
    )
    # A cached volume for the "Volumes" tab — hydrated, so its card
    # picks up a cover / year.
    db_session.add(_volume(770, "Avengers", year=1998))
    # A cached story arc for the "Story arcs" tab — hydrated, so its
    # card picks up an icon.
    db_session.add(
        CvStoryArc(
            cv_id=7100,
            name="Secret Invasion",
            raw_payload={
                "id": 7100, "name": "Secret Invasion",
                "image": {"icon_url": "https://example.com/7100.jpg"},
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    team = CvTeam(
        cv_id=850,
        name="Avengers",
        raw_payload={
            "id": 850,
            "name": "Avengers",
            "deck": "Earth's Mightiest Heroes.",
            "description": "<p>A team.</p>",
            "publisher": {"id": 10, "name": "Marvel"},
            "image": {
                "screen_large_url": "https://example.com/avengers-banner.jpg",
            },
            # CV gives aliases as one newline-separated string.
            "aliases": "The Mighty Avengers\nEarth's Mightiest\nThe Mighty Avengers",
            "first_appeared_in_issue": {
                "id": 9100, "name": "Avengers", "issue_number": "1",
            },
            "issues_disbanded_in": [
                {"id": 9200, "name": "Disassembled", "issue_number": "503"},
            ],
            "characters": [
                {"id": 901, "name": "Iron Man"},
                {"id": 902, "name": "Thor"},
                {"id": 903, "name": "Hulk"},
            ],
            # CV team friends / enemies are characters, like the
            # character page's allies / foes.
            "character_friends": [
                {"id": 901, "name": "Iron Man"},
                {"id": 950, "name": "Spider-Man"},
                {"id": 951, "name": "Captain America"},
            ],
            "character_enemies": [
                {"id": 960, "name": "Ultron"},
            ],
            "volume_credits": [
                {"id": 771, "name": "New Avengers"},
                {"id": 770, "name": "Avengers"},
                {"id": 772, "name": "West Coast Avengers"},
            ],
            "story_arc_credits": [
                {"id": 7100, "name": "Secret Invasion"},
                # CV namespaces some arcs with a quoted parent book.
                {"id": 7200, "name": '"Avengers" Disassembled'},
            ],
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(team)
    await db_session.commit()

    detail = await get_team_detail(
        db_session, _StubTeamCache(team), 850, page=1, page_size=2
    )
    assert detail is not None
    assert detail.name == "Avengers"
    assert detail.deck == "Earth's Mightiest Heroes."
    assert detail.publisher_cv_id == 10
    assert detail.members_total == 3
    assert detail.members_page_count == 2
    assert [m.cv_id for m in detail.members] == [901, 902]
    # 901 is cached -> hydrated with an icon; 902 is not.
    assert detail.members[0].is_hydrated is True
    assert detail.members[0].icon_url == "https://example.com/901.jpg"
    assert detail.members[1].is_hydrated is False

    # CV landscape screen image -> the banner hero.
    assert detail.banner_url == "https://example.com/avengers-banner.jpg"
    # The "General Information" card payload.
    info = detail.info
    # Aliases split from the newline string, trimmed and de-duped.
    assert info.aliases == ["The Mighty Avengers", "Earth's Mightiest"]
    assert info.first_appearance is not None
    assert info.first_appearance.cv_id == 9100
    assert info.first_appearance.issue_number == "1"
    # The first-appearance issue's cached cover thumbnail is pulled in.
    assert info.first_appearance.cover_url == "https://example.com/fa-team.jpg"
    assert info.first_appearance.is_hydrated is True
    # issues_disbanded_in -> TeamInfo.disbanded_in (issue refs).
    assert [d.cv_id for d in info.disbanded_in] == [9200]
    assert info.disbanded_in[0].name == "Disassembled"
    assert info.disbanded_in[0].issue_number == "503"

    # "Friends" / "Enemies" tabs — characters, each its own pager.
    assert detail.friends_total == 3
    assert detail.friends_page_count == 2
    assert [f.cv_id for f in detail.friends] == [901, 950]
    # 901 is cached -> hydrated with an icon; 950 is not.
    assert detail.friends[0].is_hydrated is True
    assert detail.friends[0].icon_url == "https://example.com/901.jpg"
    assert detail.friends[1].is_hydrated is False
    assert detail.enemies_total == 1
    assert [e.cv_id for e in detail.enemies] == [960]
    assert detail.enemies[0].is_hydrated is False

    # "Volumes" tab — credited volumes, sorted by name, paged.
    assert detail.volumes_total == 3
    assert detail.volumes_page_count == 2
    assert [v.cv_id for v in detail.volumes] == [770, 771]
    # 770 is cached -> hydrated with a cover / year; 771 is not.
    assert detail.volumes[0].is_hydrated is True
    assert detail.volumes[0].cover_url is not None
    assert detail.volumes[0].year == 1998
    assert detail.volumes[1].is_hydrated is False

    # "Story arcs" tab — arcs sorted by name, with CV's quoted parent
    # book split off into the card badge.
    assert detail.story_arcs_total == 2
    assert [a.cv_id for a in detail.story_arcs] == [7200, 7100]
    assert detail.story_arcs[0].name == "Disassembled"
    assert detail.story_arcs[0].badge == "Avengers"
    # 7100 is cached -> hydrated with an icon.
    assert detail.story_arcs[1].cv_id == 7100
    assert detail.story_arcs[1].is_hydrated is True

    d2 = await get_team_detail(
        db_session, _StubTeamCache(team), 850, page=2, page_size=2
    )
    assert [m.cv_id for m in d2.members] == [903]

    # The friends pager is independent of the members pager.
    d3 = await get_team_detail(
        db_session, _StubTeamCache(team), 850, page=1, page_size=2,
        friends_page=2,
    )
    assert [f.cv_id for f in d3.friends] == [951]

    # The volumes alphabet-bar filter narrows by the volume's name.
    dv = await get_team_detail(
        db_session, _StubTeamCache(team), 850, page=1, page_size=2,
        volumes_letter="W",
    )
    assert dv.volumes_total == 3 and dv.volumes_filtered_count == 1
    assert [v.cv_id for v in dv.volumes] == [772]  # West Coast Avengers

    # The story-arc alphabet filter narrows by the cleaned arc name.
    da = await get_team_detail(
        db_session, _StubTeamCache(team), 850, page=1, page_size=2,
        arcs_letter="S",
    )
    assert da.story_arcs_total == 2 and da.story_arcs_filtered_count == 1
    assert [a.cv_id for a in da.story_arcs] == [7100]  # Secret Invasion


async def test_get_team_detail_unknown_returns_none(db_session):
    class _NoneCache:
        async def get_team(self, db, cv_id, *, force_refresh=False):
            return None

    detail = await get_team_detail(db_session, _NoneCache(), 99999)
    assert detail is None


class _StubPersonCache:
    """Minimal ComicVineCache stand-in — get_person returns a row."""

    def __init__(self, person):
        self._person = person

    async def get_person(self, db, cv_id, *, force_refresh=False):
        return self._person


async def test_get_creator_detail(db_session):
    # A creator's CV payload carries volume_credits (not issue_credits)
    # — the page shows them as a paginated volume list with per-volume
    # issue counts, sorted by volume name, enriched from cv_volumes.
    await _make_library(db_session)  # volumes 100 (Saga) / 200 (X-Men)
    # A cached created-character and story-arc — for the avatar tabs.
    db_session.add_all(
        [
            CvCharacter(
                cv_id=601,
                name="Yorick Brown",
                raw_payload={
                    "id": 601, "name": "Yorick Brown",
                    "image": {"icon_url": "https://example.com/601.jpg"},
                },
                fetched_at=datetime.now(tz=UTC),
            ),
            CvStoryArc(
                cv_id=7001,
                name="The Cure",
                raw_payload={
                    "id": 7001, "name": "The Cure",
                    "image": {"icon_url": "https://example.com/7001.jpg"},
                },
                fetched_at=datetime.now(tz=UTC),
            ),
        ]
    )
    person = CvPerson(
        cv_id=700,
        name="Brian K. Vaughan",
        raw_payload={
            "id": 700,
            "name": "Brian K. Vaughan",
            "deck": "A writer.",
            "volume_credits": [
                {"id": 200, "name": "X-Men"},
                {"id": 100, "name": "Saga"},
                {"id": 999, "name": "Zeta"},
            ],
            "created_characters": [
                {"id": 601, "name": "Yorick Brown"},
                {"id": 602, "name": "Agent 355"},
            ],
            "story_arc_credits": [
                {"id": 7001, "name": '"Avengers" Disassembled'},
                {"id": 7002, "name": "Civil War"},
            ],
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(person)
    await db_session.commit()
    cache = _StubPersonCache(person)

    # 3 volumes, 2 per page -> 2 pages. Sorted by name: Saga, X-Men, Zeta.
    p1 = await get_creator_detail(db_session, cache, 700, page=1, page_size=2)
    assert p1 is not None
    assert p1.name == "Brian K. Vaughan"
    assert p1.total == 3
    assert p1.page == 1 and p1.page_count == 2
    assert [v.cv_id for v in p1.volume_credits] == [100, 200]
    # Saga (100) is cached by _make_library -> hydrated, year + cover.
    saga = p1.volume_credits[0]
    assert saga.name == "Saga"
    assert saga.is_hydrated is True
    assert saga.year == 2012
    assert saga.cover_url is not None

    p2 = await get_creator_detail(db_session, cache, 700, page=2, page_size=2)
    assert [v.cv_id for v in p2.volume_credits] == [999]
    # 999 isn't cached -> not hydrated, no cover.
    assert p2.volume_credits[0].is_hydrated is False
    assert p2.volume_credits[0].cover_url is None

    # Alphabet-bar letter filter — narrows by the volume's first letter.
    s = await get_creator_detail(db_session, cache, 700, letter="S")
    assert s.total == 3 and s.filtered_count == 1
    assert [v.cv_id for v in s.volume_credits] == [100]  # Saga
    q = await get_creator_detail(db_session, cache, 700, letter="Q")
    assert q.total == 3 and q.filtered_count == 0
    assert q.volume_credits == []

    # Created characters tab — sorted by name ("Agent 355" before
    # "Yorick Brown"), so the cached 601 is now second.
    d = await get_creator_detail(db_session, cache, 700)
    assert [c.cv_id for c in d.created_characters] == [602, 601]
    assert d.created_characters_total == 2
    assert d.created_characters_filtered_count == 2  # no letter filter
    assert d.created_characters[1].is_hydrated is True  # 601 cached
    assert d.created_characters[1].icon_url == "https://example.com/601.jpg"
    assert d.created_characters[0].is_hydrated is False  # 602 not cached

    # Story arcs tab — sorted by the *parsed* name ("Civil War" before
    # "Disassembled"); 7001's quoted "<book>" prefix becomes the badge.
    assert [a.cv_id for a in d.story_arcs] == [7002, 7001]
    assert d.story_arcs_total == 2
    assert d.story_arcs[0].name == "Civil War"
    assert d.story_arcs[0].badge is None
    arc = d.story_arcs[1]
    assert arc.cv_id == 7001
    assert arc.name == "Disassembled"
    assert arc.badge == "Avengers"
    assert arc.is_hydrated is True
    assert arc.icon_url == "https://example.com/7001.jpg"

    # Alphabet filters narrow each tab by the (parsed) name's letter.
    fc = await get_creator_detail(db_session, cache, 700, characters_letter="A")
    assert [c.cv_id for c in fc.created_characters] == [602]  # Agent 355
    assert fc.created_characters_total == 2
    assert fc.created_characters_filtered_count == 1
    assert fc.created_characters_letter == "A"
    fa = await get_creator_detail(db_session, cache, 700, arcs_letter="D")
    assert [a.cv_id for a in fa.story_arcs] == [7001]  # Disassembled
    assert fa.story_arcs_filtered_count == 1


async def test_get_creator_detail_no_credits(db_session):
    person = CvPerson(
        cv_id=701,
        name="Newcomer",
        raw_payload={"id": 701, "name": "Newcomer"},
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(person)
    await db_session.commit()
    detail = await get_creator_detail(db_session, _StubPersonCache(person), 701)
    assert detail is not None
    assert detail.total == 0
    assert detail.volume_credits == []
    assert detail.page_count == 1


async def test_get_character_detail_pagination(db_session):
    await _make_library(db_session)
    character = CvCharacter(
        cv_id=501,
        name="Marko",
        raw_payload={
            "id": 501, "name": "Marko", "issue_credits": [{"id": 1001}]
        },
        fetched_at=datetime.now(tz=UTC),
        volumes_scraped_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    # Three scraped volume appearances.
    db_session.add_all(
        [
            _char_volume(501, 100, "Saga", position=0),
            _char_volume(501, 200, "X-Men", position=1),
            _char_volume(501, 300, "Zatanna", position=2),
        ]
    )
    await db_session.commit()
    cache = _StubCharacterCache(character)

    # Three volume cards; page_size 2 — two pages, sorted by name.
    p1 = await get_character_detail(db_session, cache, 501, page=1, page_size=2)
    assert p1.appearance_volumes_total == 3
    assert p1.filtered_count == 3
    assert p1.page == 1 and p1.page_count == 2
    assert [v.name for v in p1.appearance_volumes] == ["Saga", "X-Men"]

    p2 = await get_character_detail(db_session, cache, 501, page=2, page_size=2)
    assert p2.page == 2 and p2.page_count == 2
    assert [v.name for v in p2.appearance_volumes] == ["Zatanna"]

    # An out-of-range page clamps to the last page.
    p9 = await get_character_detail(db_session, cache, 501, page=9, page_size=2)
    assert p9.page == 2


async def test_get_character_detail_sorts_by_volume(db_session):
    await _make_library(db_session)
    character = CvCharacter(
        cv_id=503,
        name="Crossover",
        raw_payload={"id": 503, "name": "Crossover", "issue_credits": []},
        fetched_at=datetime.now(tz=UTC),
        volumes_scraped_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    # Stored in scrape order; the service sorts the cards by name.
    db_session.add_all(
        [
            _char_volume(503, 200, "X-Men", position=0),
            _char_volume(503, 100, "Saga", position=1),
        ]
    )
    await db_session.commit()

    detail = await get_character_detail(
        db_session, _StubCharacterCache(character), 503
    )
    assert [v.name for v in detail.appearance_volumes] == ["Saga", "X-Men"]


async def test_get_character_detail_letter_filter(db_session):
    await _make_library(db_session)
    character = CvCharacter(
        cv_id=502,
        name="Cameo",
        raw_payload={"id": 502, "name": "Cameo", "issue_credits": []},
        fetched_at=datetime.now(tz=UTC),
        volumes_scraped_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    db_session.add_all(
        [
            _char_volume(502, 100, "Saga", position=0),
            _char_volume(502, 200, "X-Men", position=1),
        ]
    )
    await db_session.commit()
    cache = _StubCharacterCache(character)

    # Letter S → only the Saga card; the full volume total stays at 2.
    s = await get_character_detail(db_session, cache, 502, letter="S")
    assert s.appearance_volumes_total == 2 and s.filtered_count == 1
    assert [v.name for v in s.appearance_volumes] == ["Saga"]

    # Letter X → only the X-Men card.
    x = await get_character_detail(db_session, cache, 502, letter="X")
    assert x.filtered_count == 1
    assert [v.name for v in x.appearance_volumes] == ["X-Men"]

    # A letter with no matching volume → empty page, total intact.
    q = await get_character_detail(db_session, cache, 502, letter="Q")
    assert q.appearance_volumes_total == 2 and q.filtered_count == 0
    assert q.appearance_volumes == []


# ---- 11F: supplements --------------------------------------------------


async def test_attach_supplement(db_session):
    """attach_supplement rewrites a file's match row to a SUPPLEMENT
    resolution pointed at a CV volume, clearing the other targets."""
    await _make_library(db_session)  # CV volume 100, issues 1001-1003
    f = _file(f"{500:064x}")
    db_session.add(f)
    await db_session.flush()
    # Pre-state: a confirmed CV-issue match.
    db_session.add(_match(f.id, 1001, status=MatchStatus.CONFIRMED))
    await db_session.commit()

    ok = await attach_supplement(
        db_session,
        file_id=f.id,
        volume_cv_id=100,
        supplement_type="cover_gallery",
        attached_by=None,
    )
    assert ok is True
    fm = await db_session.get(FileMatch, f.id)
    assert fm.status == MatchStatus.SUPPLEMENT.value
    assert fm.source == MatchSource.MANUAL.value
    assert fm.supplement_volume_cv_id == 100
    assert fm.supplement_type == "cover_gallery"
    # The other polymorphic targets are cleared (single-target invariant).
    assert fm.issue_cv_id is None
    assert fm.local_issue_id is None
    assert fm.confidence is None


async def test_attach_supplement_unknown_file(db_session):
    # No file_matches row for the id -> nothing to rewrite.
    ok = await attach_supplement(
        db_session,
        file_id=uuid.uuid4(),
        volume_cv_id=100,
        supplement_type="cover_gallery",
        attached_by=None,
    )
    assert ok is False


async def test_attach_supplement_bonus_content(db_session):
    """``bonus_content`` is the second supplement type the duplicate
    inspector exposes — covers sketch archives, behind-the-scenes
    pages, scripts, and any other archive that belongs on the volume
    but isn't the issue. Service-side it's just another label
    flowing into the open-ended ``supplement_type`` column."""
    from app.services.local import SUPPLEMENT_TYPE_LABELS, SUPPLEMENT_TYPES

    # The vocabulary the duplicates dropdown reads from must include
    # this kind; the failure mode would be the route's validation
    # rejecting a value the UI is offering.
    assert "bonus_content" in dict(SUPPLEMENT_TYPES)
    assert SUPPLEMENT_TYPE_LABELS["bonus_content"] == "Bonus content"

    await _make_library(db_session)
    f = _file(f"{501:064x}")
    db_session.add(f)
    await db_session.flush()
    db_session.add(_match(f.id, 1001, status=MatchStatus.AUTO))
    await db_session.commit()

    ok = await attach_supplement(
        db_session,
        file_id=f.id,
        volume_cv_id=100,
        supplement_type="bonus_content",
        attached_by=None,
    )
    assert ok is True
    fm = await db_session.get(FileMatch, f.id)
    assert fm.status == MatchStatus.SUPPLEMENT.value
    assert fm.supplement_type == "bonus_content"
    assert fm.issue_cv_id is None


async def test_list_volume_supplements(db_session):
    """list_volume_supplements returns a volume's supplement files with
    their filename + type label, scoped to that volume."""
    db_session.add_all([_volume(100, "Saga"), _volume(200, "X-Men")])
    files = [_file(f"{510 + i:064x}") for i in range(3)]
    db_session.add_all(files)
    await db_session.flush()
    paths = [
        "/library/saga-covers.cbz",
        "/library/saga-sketches.cbz",
        "/library/xmen-covers.cbz",
    ]
    for f, path in zip(files, paths, strict=True):
        db_session.add(_location(f.id, path))
        db_session.add(_bare_match(f.id))
    await db_session.commit()
    for f, vol in ((files[0], 100), (files[1], 100), (files[2], 200)):
        await attach_supplement(
            db_session, file_id=f.id, volume_cv_id=vol,
            supplement_type="cover_gallery", attached_by=None,
        )

    sups = await list_volume_supplements(db_session, 100)
    assert {s.filename for s in sups} == {
        "saga-covers.cbz", "saga-sketches.cbz",
    }
    assert all(s.supplement_type == "cover_gallery" for s in sups)
    assert all(s.type_label == "Cover gallery" for s in sups)
    # Scoped to the volume — 200's supplement isn't in 100's list.
    other = await list_volume_supplements(db_session, 200)
    assert [s.filename for s in other] == ["xmen-covers.cbz"]
    # A volume with no supplements -> empty.
    assert await list_volume_supplements(db_session, 999) == []


async def test_get_volume_detail_surfaces_supplements(db_session):
    """get_volume_detail carries the volume's supplements."""
    db_session.add(_volume(100, "Saga"))
    f = _file(f"{520:064x}")
    db_session.add(f)
    await db_session.flush()
    db_session.add(_location(f.id, "/library/saga-extras.cbz"))
    db_session.add(_bare_match(f.id))
    await db_session.commit()
    await attach_supplement(
        db_session, file_id=f.id, volume_cv_id=100,
        supplement_type="cover_gallery", attached_by=None,
    )

    detail = await get_volume_detail(db_session, 100)
    assert detail is not None
    assert [s.filename for s in detail.supplements] == ["saga-extras.cbz"]
    assert detail.supplements[0].type_label == "Cover gallery"


# ---- 11D: attach-group-to-existing-local-volume -----------------------
#
# ``attach_local_group`` is the bulk counterpart of
# ``create_local_entry``'s ``existing_volume_id`` branch — a whole
# review-queue series group joins an existing local volume in one
# submit. The tests below stand up pending file_matches rows whose
# filenames parse into a common series key (so the live queue groups
# them together the way the route does) and exercise:
#   - happy path: all numbers free, every file flips to LOCAL.
#   - existing-issue collision: a number is already on the target volume.
#   - in-batch duplicate: two files in the group submit the same number.
#   - drained group / missing volume: soft failures return None.


async def _pending_file(db_session, sha: str, *, filename: str):
    """One pending file_matches row with a filename that parses into
    ``parsed_series`` — enough scaffolding to surface in the review
    queue's grouping pass."""
    f = _file(sha)
    db_session.add(f)
    await db_session.flush()
    db_session.add(_location(f.id, f"/library/{filename}"))
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
    return f


async def test_attach_local_group_happy_path(db_session):
    """Three pending files in the 'My Indie Series' group attach to an
    existing local volume that already has issue #100. All three
    file_matches rows flip to LOCAL, three new local_issues rows hang
    off the target volume, no conflicts."""
    # Existing local volume: a single, non-colliding issue.
    lv, _existing = await _make_local_volume(
        db_session, "My Indie Series", ["100"], sha_offset=900,
    )
    # The pending group — three files, none of whose numbers clash.
    files = []
    for i, n in enumerate([1, 2, 3]):
        files.append(
            await _pending_file(
                db_session,
                f"{700 + i:064x}",
                filename=f"My Indie Series {n:03d}.cbz",
            )
        )
    await db_session.commit()

    outcome = await attach_local_group(
        db_session,
        series_key="My Indie Series",
        target_volume_id=lv.id,
        file_issue_numbers={
            str(f.id): str(n)
            for f, n in zip(files, [1, 2, 3], strict=True)
        },
        file_issue_names={},
        created_by=None,
    )
    assert outcome is not None
    result, conflicts = outcome
    assert conflicts == []
    assert result.issue_count == 3
    assert result.skipped_count == 0
    assert result.local_volume_id == lv.id

    # Every file_matches row points at a local_issues row under lv.
    for f in files:
        fm = await db_session.get(FileMatch, f.id)
        assert fm.status == MatchStatus.LOCAL.value
        assert fm.source == MatchSource.LOCAL.value
        assert fm.issue_cv_id is None
        assert fm.local_issue_id is not None
        issue = await db_session.get(LocalIssue, fm.local_issue_id)
        assert issue.local_volume_id == lv.id

    # The target volume now has 4 issues (1 existing + 3 new).
    rows = (
        await db_session.execute(
            select(LocalIssue.issue_number).where(
                LocalIssue.local_volume_id == lv.id
            )
        )
    ).scalars().all()
    assert sorted(rows) == ["1", "100", "2", "3"]


async def test_attach_local_group_conflict_with_existing_issue(db_session):
    """A submitted issue number already on the target volume → soft
    failure: no writes, conflicts list points at the offending file."""
    lv, _existing = await _make_local_volume(
        db_session, "My Indie Series", ["1"], sha_offset=900,
    )
    f_ok = await _pending_file(
        db_session, f"{710:064x}", filename="My Indie Series 002.cbz",
    )
    f_conflict = await _pending_file(
        db_session, f"{711:064x}", filename="My Indie Series 001.cbz",
    )
    await db_session.commit()

    outcome = await attach_local_group(
        db_session,
        series_key="My Indie Series",
        target_volume_id=lv.id,
        file_issue_numbers={
            str(f_ok.id): "2",
            str(f_conflict.id): "1",  # collides with the existing #1
        },
        file_issue_names={},
        created_by=None,
    )
    assert outcome is not None
    result, conflicts = outcome
    assert result.issue_count == 0  # NO writes happened
    assert len(conflicts) == 1
    assert conflicts[0].file_id == f_conflict.id
    assert conflicts[0].issue_number == "1"
    assert conflicts[0].reason == "existing"

    # The non-conflicting file is unchanged — neither row was written.
    fm_ok = await db_session.get(FileMatch, f_ok.id)
    assert fm_ok.status == MatchStatus.PENDING.value
    assert fm_ok.local_issue_id is None
    fm_conf = await db_session.get(FileMatch, f_conflict.id)
    assert fm_conf.status == MatchStatus.PENDING.value


async def test_attach_local_group_conflict_is_case_insensitive(db_session):
    """Normalisation: ``"1"`` on the new batch vs ``"1 "`` /
    ``"01"`` already on the target should still register as a clash.
    Defends against the obvious foot-gun where the existing volume's
    issue number string differs only in trim/case from the submitted
    one — the file would otherwise sail through validation and create a
    second 'issue 1' row that looks identical to the user."""
    lv, _existing = await _make_local_volume(
        db_session, "My Indie Series", ["1 "], sha_offset=900,
    )
    f = await _pending_file(
        db_session, f"{720:064x}", filename="My Indie Series 001.cbz",
    )
    await db_session.commit()

    outcome = await attach_local_group(
        db_session,
        series_key="My Indie Series",
        target_volume_id=lv.id,
        file_issue_numbers={str(f.id): "1"},
        file_issue_names={},
        created_by=None,
    )
    assert outcome is not None
    _result, conflicts = outcome
    assert len(conflicts) == 1
    assert conflicts[0].reason == "existing"


async def test_attach_local_group_duplicate_within_batch(db_session):
    """Two files in the same submission want issue #1 — the second one
    is flagged as ``duplicate`` (the first is left alone; only one of
    the pair is the 'conflict' to fix). No writes happen."""
    lv, _existing = await _make_local_volume(
        db_session, "My Indie Series", [], sha_offset=900,
    )
    f1 = await _pending_file(
        db_session, f"{730:064x}", filename="My Indie Series 001.cbz",
    )
    f2 = await _pending_file(
        db_session, f"{731:064x}", filename="My Indie Series 001b.cbz",
    )
    await db_session.commit()

    outcome = await attach_local_group(
        db_session,
        series_key="My Indie Series",
        target_volume_id=lv.id,
        file_issue_numbers={str(f1.id): "1", str(f2.id): "1"},
        file_issue_names={},
        created_by=None,
    )
    assert outcome is not None
    _result, conflicts = outcome
    assert len(conflicts) == 1
    # The second occurrence is the one flagged; the first is fine
    # (and what the reviewer most likely intended).
    assert conflicts[0].file_id == f2.id
    assert conflicts[0].reason == "duplicate"
    # No file flipped to LOCAL.
    fm1 = await db_session.get(FileMatch, f1.id)
    assert fm1.status == MatchStatus.PENDING.value


async def test_attach_local_group_unnumbered_issues_dont_collide(db_session):
    """Blank/None issue numbers don't collide with anything — multiple
    unnumbered issues can coexist under the same local volume. The
    reviewer empties both issue-number inputs on submit; the resolved
    number for each row falls through to ``""``, which the conflict
    check skips entirely."""
    lv, _existing = await _make_local_volume(
        db_session, "My Indie Series", [], sha_offset=900,
    )
    # Add an explicitly-NULL existing issue too, to be thorough.
    db_session.add(_local_issue(lv.id, issue_number=None))
    # Numbered filenames so comicfn2dict parses ``series="My Indie
    # Series"`` and the group surfaces in the preview — what we're
    # exercising is the *submitted* numbers being blank, not the parsed
    # ones.
    f1 = await _pending_file(
        db_session, f"{740:064x}", filename="My Indie Series 005.cbz",
    )
    f2 = await _pending_file(
        db_session, f"{741:064x}", filename="My Indie Series 006.cbz",
    )
    await db_session.commit()

    outcome = await attach_local_group(
        db_session,
        series_key="My Indie Series",
        target_volume_id=lv.id,
        file_issue_numbers={str(f1.id): "", str(f2.id): ""},
        file_issue_names={},
        created_by=None,
    )
    assert outcome is not None
    result, conflicts = outcome
    assert conflicts == []
    assert result.issue_count == 2


async def test_attach_local_group_drained_group_returns_none(db_session):
    """A group key that doesn't match any pending file (all confirmed in
    another tab / never existed) is a hard failure: None, not an empty
    success result."""
    lv, _existing = await _make_local_volume(
        db_session, "My Indie Series", ["1"], sha_offset=900,
    )
    outcome = await attach_local_group(
        db_session,
        series_key="Nothing Pending Under This Name",
        target_volume_id=lv.id,
        file_issue_numbers={},
        file_issue_names={},
        created_by=None,
    )
    assert outcome is None


async def test_attach_local_group_missing_target_volume_returns_none(db_session):
    """If the picked local_volume_id is gone (deleted between page
    render and submit), the service refuses with None — the caller's
    redirect surface treats it the same as a drained group."""
    outcome = await attach_local_group(
        db_session,
        series_key="My Indie Series",
        target_volume_id=uuid.uuid4(),  # not in the DB
        file_issue_numbers={},
        file_issue_names={},
        created_by=None,
    )
    assert outcome is None


# ---- Review-queue grouping is group-atomic vs the row cap -------------
#
# ``list_pending_groups`` used to fetch the top N rows by confidence and
# group them in Python, which split a high-cardinality volume across
# the cap boundary — confirming a visible member would then shift the
# cap, surface a previously-hidden sibling, and make the group appear
# to grow as the reviewer worked through it. The fix is two-stage:
# cheap path-only pre-pass, pick groups in big-first order until the
# next group would cross the cap, enrich only those file_ids. Single-
# group views (``get_group_reference``, ``preview_volume_confirm``,
# ``preview_local_group``) bypass the cap entirely — they're asking
# "give me everything in series X", and a cap there is a bug.


async def _pending_in_series(
    db_session, *, series_filename_prefix: str, count: int, confidence_seed: int,
    sha_offset: int,
):
    """Build ``count`` PENDING files whose filenames parse to the same
    series, with stepped confidences so the row cap actually has a
    chance to slice through the middle of the group when grouping
    naively."""
    files: list[File] = []
    for i in range(count):
        sha = f"{sha_offset + i:064x}"
        f = _file(sha)
        db_session.add(f)
        files.append(f)
    await db_session.flush()
    for i, f in enumerate(files):
        # Stepped issue numbers so the parser pulls the same series
        # name (the prefix) for every file.
        db_session.add(
            _location(f.id, f"/library/{series_filename_prefix} {i + 1:03d}.cbz")
        )
        # Decreasing confidence so files within the series span the
        # full PENDING band. Without this, a cap-based naive grouping
        # would just slice the lot off cleanly; we want a *mix* of
        # high- and low-confidence members per series.
        conf = round(0.85 - (confidence_seed + i) * 0.005, 3)
        db_session.add(
            FileMatch(
                file_id=f.id,
                issue_cv_id=None,
                confidence=Decimal(str(conf)),
                status=MatchStatus.PENDING,
                source=MatchSource.FILENAME,
                matched_at=datetime.now(tz=UTC),
            )
        )
    return files


async def test_list_pending_groups_keeps_a_big_group_atomic(
    db_session, monkeypatch
):
    """A series whose file count is larger than the row cap should
    still appear as ONE atomic group (every member enriched and
    visible), not chopped in half by the cap boundary. The pre-fix
    code split such a group; this test pins the new behaviour."""
    # Squeeze the cap so a realistic test setup can blow past it.
    monkeypatch.setattr(
        "app.services.review.PENDING_GROUP_ROW_CAP", 5
    )

    # One big series (8 files) + a few small ones. Total 11 > cap=5.
    await _pending_in_series(
        db_session,
        series_filename_prefix="Big Series",
        count=8,
        confidence_seed=0,
        sha_offset=2000,
    )
    await _pending_in_series(
        db_session,
        series_filename_prefix="Small Series",
        count=2,
        confidence_seed=1,
        sha_offset=2100,
    )
    await db_session.commit()

    groups, total, hit_cap = await list_pending_groups(db_session)

    # Always-include-the-first-group: the big one is fully present even
    # though it's by itself larger than the cap.
    big = next(g for g in groups if g.series_key == "Big Series")
    assert big.file_count == 8
    # No partial member set — every file we created is visible.
    assert len(big.rows) == 8

    # The small group is dropped (its inclusion would cross the cap).
    assert all(g.series_key != "Small Series" for g in groups)
    assert hit_cap is True
    assert total == 8


async def test_list_pending_groups_includes_groups_until_cap_then_stops(
    db_session, monkeypatch
):
    """When several small-to-medium groups fit before the cap, every
    one of them comes in whole; the first group that *would* cross
    the cap stops the picker, and any remaining groups are reported
    via ``hit_row_cap``."""
    monkeypatch.setattr(
        "app.services.review.PENDING_GROUP_ROW_CAP", 8
    )

    # Three groups of 3 each — first two fit (3+3=6 ≤ 8), third would
    # push to 9 and is dropped whole.
    await _pending_in_series(
        db_session, series_filename_prefix="Alpha",
        count=3, confidence_seed=0, sha_offset=2200,
    )
    await _pending_in_series(
        db_session, series_filename_prefix="Beta",
        count=3, confidence_seed=10, sha_offset=2300,
    )
    await _pending_in_series(
        db_session, series_filename_prefix="Gamma",
        count=3, confidence_seed=20, sha_offset=2400,
    )
    await db_session.commit()

    groups, total, hit_cap = await list_pending_groups(db_session)
    keys = {g.series_key for g in groups}

    # Two whole groups visible; total file count is the sum of those.
    assert len(groups) == 2
    assert total == 6
    # Every visible group is atomic (3 files each).
    for g in groups:
        assert g.file_count == 3
        assert len(g.rows) == 3
    # The third group ("Gamma") is hidden entirely.
    assert "Gamma" not in keys
    assert hit_cap is True


async def test_list_pending_groups_no_cap_hit_when_everything_fits(
    db_session, monkeypatch
):
    """``hit_row_cap`` only fires when groups got dropped. If every
    pending file fits, the flag stays False even with a tight cap."""
    monkeypatch.setattr(
        "app.services.review.PENDING_GROUP_ROW_CAP", 50
    )
    await _pending_in_series(
        db_session, series_filename_prefix="Alpha",
        count=2, confidence_seed=0, sha_offset=2500,
    )
    await db_session.commit()

    groups, total, hit_cap = await list_pending_groups(db_session)
    assert len(groups) == 1
    assert total == 2
    assert hit_cap is False


async def test_get_group_reference_bypasses_the_row_cap(
    db_session, monkeypatch
):
    """A reviewer landing on the volume-search reference card for a
    big series must see the full count, not the cap-truncated count.
    This is the single-group counterpart of the queue's group-
    atomicity guarantee — single-group fetches never cap."""
    monkeypatch.setattr(
        "app.services.review.PENDING_GROUP_ROW_CAP", 3
    )

    # Big series: 10 files. List view would drop most; single-group
    # view should return every one of them.
    await _pending_in_series(
        db_session, series_filename_prefix="Big Series",
        count=10, confidence_seed=0, sha_offset=2600,
    )
    # An unrelated, also-big series in the background — proves the
    # single-group fetch isn't accidentally including other groups'
    # files in the count.
    await _pending_in_series(
        db_session, series_filename_prefix="Other Series",
        count=10, confidence_seed=20, sha_offset=2700,
    )
    await db_session.commit()

    ref = await get_group_reference(db_session, series_key="Big Series")
    assert ref is not None
    assert ref.series_key == "Big Series"
    assert ref.file_count == 10


async def test_preview_local_group_bypasses_the_row_cap(
    db_session, monkeypatch
):
    """The bulk-create-from-group page must list every file in the
    series, not just the cap-survivors. Otherwise the reviewer would
    create a local volume that's missing issues, and the matcher's
    next pass would silently leave them behind."""
    monkeypatch.setattr(
        "app.services.review.PENDING_GROUP_ROW_CAP", 3
    )

    await _pending_in_series(
        db_session, series_filename_prefix="Indie",
        count=7, confidence_seed=0, sha_offset=2800,
    )
    await db_session.commit()

    preview = await preview_local_group(db_session, "Indie")
    assert preview is not None
    assert preview.file_count == 7
    assert len(preview.files) == 7


# ---- Fix match: re-pick the wrong volume from the volume page ---------


async def test_fix_match_remaps_files_by_issue_number(db_session):
    """Three files matched to volume 100 (the wrong publisher's Saga)
    get re-mapped to volume 200's issues by their parsed issue number.
    Issue numbers in the file names: 1, 2, 3 — and volume 200 has
    issues 1 and 2 only, so the third file is skipped."""
    # Seed volumes with overlapping issue numbers. The CV-cache test
    # helpers already create two publishers (Image=31, Marvel=10) and
    # two volumes — Saga (100, 3 issues) and X-Men (200, 2 issues). We
    # want both to have parallel issue numbers for the fix-match map.
    db_session.add_all([_publisher(31, "Image"), _publisher(10, "Marvel")])
    await db_session.flush()
    db_session.add_all(
        [
            _volume(100, "Saga", year=2012, publisher_cv_id=31, count_of_issues=3),
            _volume(200, "Saga", year=2015, publisher_cv_id=10, count_of_issues=2),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _issue(1001, volume_cv_id=100, issue_number="1"),
            _issue(1002, volume_cv_id=100, issue_number="2"),
            _issue(1003, volume_cv_id=100, issue_number="3"),
            _issue(2001, volume_cv_id=200, issue_number="1"),
            _issue(2002, volume_cv_id=200, issue_number="2"),
        ]
    )
    await db_session.flush()

    # Three files matched to the WRONG volume (100). Filenames must
    # parse to an issue number for the fix-match service to find
    # the corresponding issue in the new volume — that's the same
    # rule volume-confirm uses.
    files = []
    for i, _issue_cv_id in enumerate([1001, 1002, 1003]):
        f = _file(f"{3000 + i:064x}")
        db_session.add(f)
        files.append(f)
    await db_session.flush()
    for i, (f, issue_cv_id) in enumerate(zip(files, [1001, 1002, 1003], strict=True)):
        db_session.add(_location(f.id, f"/library/Saga #{i + 1:03d}.cbz"))
        db_session.add(_match(f.id, issue_cv_id, status=MatchStatus.CONFIRMED))
    await db_session.commit()

    result = await execute_fix_match(
        db_session,
        old_volume_cv_id=100,
        new_volume_cv_id=200,
        matched_by_user_id=None,
    )
    assert result is not None
    # Issues 1 and 2 exist in the new volume; issue 3 doesn't.
    assert result.rematched_count == 2
    assert result.skipped_count == 1

    # The two re-matched rows now point at the new volume's issues.
    fm1 = await db_session.get(FileMatch, files[0].id)
    fm2 = await db_session.get(FileMatch, files[1].id)
    fm3 = await db_session.get(FileMatch, files[2].id)
    assert fm1.issue_cv_id == 2001
    assert fm2.issue_cv_id == 2002
    assert fm1.status == MatchStatus.CONFIRMED.value
    assert fm1.source == MatchSource.MANUAL.value
    # The skipped file stays at the old (wrong) issue — the reviewer
    # can deal with it manually from the file-review path.
    assert fm3.issue_cv_id == 1003


async def test_fix_match_returns_none_when_new_volume_missing(db_session):
    """If the new volume isn't in cache (defensive — the caller is
    supposed to hydrate it first), the service refuses with None
    rather than silently writing nothing."""
    result = await execute_fix_match(
        db_session,
        old_volume_cv_id=100,
        new_volume_cv_id=99999,  # not in DB
        matched_by_user_id=None,
    )
    assert result is None


# ---- exclude_files_by_series ------------------------------------------


async def test_exclude_files_by_series_flips_every_reviewable_file(db_session):
    """All three pending files in the 'Some Indie' group land in the
    listing, then ``exclude_files_by_series`` flips all three
    ``excluded_from_matching`` flags to True. Returns the count flipped
    so the route can surface it in the redirect banner."""
    files = []
    for i, n in enumerate([1, 2, 3]):
        files.append(
            await _pending_file(
                db_session,
                f"{810 + i:064x}",
                filename=f"Some Indie Series {n:03d}.cbz",
            )
        )
    await db_session.commit()

    flipped = await exclude_files_by_series(
        db_session, series_key="Some Indie Series"
    )
    assert flipped == 3
    for f in files:
        await db_session.refresh(f)
        assert f.excluded_from_matching is True


async def test_exclude_files_by_series_leaves_resolved_files_untouched(db_session):
    """AUTO / CONFIRMED rows have already been resolved — they aren't
    reviewable, so the bulk exclude on the same series shouldn't
    touch them. Matches the create-local-volume path's behaviour
    (it also operates on reviewable files only)."""
    # Two pending files in 'Mixed Series' — these get excluded.
    pending_a = await _pending_file(
        db_session, f"{820:064x}", filename="Mixed Series 001.cbz",
    )
    pending_b = await _pending_file(
        db_session, f"{821:064x}", filename="Mixed Series 002.cbz",
    )
    # One AUTO-resolved file in the same parsed series — left alone.
    resolved = _file(f"{822:064x}")
    db_session.add(resolved)
    await db_session.flush()
    db_session.add(_location(resolved.id, "/library/Mixed Series 003.cbz"))
    db_session.add(
        FileMatch(
            file_id=resolved.id,
            issue_cv_id=None,
            confidence=None,
            status=MatchStatus.AUTO,
            source=MatchSource.FILENAME,
            matched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    flipped = await exclude_files_by_series(
        db_session, series_key="Mixed Series"
    )
    assert flipped == 2
    for f in (pending_a, pending_b):
        await db_session.refresh(f)
        assert f.excluded_from_matching is True
    await db_session.refresh(resolved)
    assert resolved.excluded_from_matching is False


async def test_exclude_files_by_series_unknown_series_returns_zero(db_session):
    """No matching group → no rows flipped, returns 0. Belt-and-
    suspenders against a stale series key reaching the route."""
    flipped = await exclude_files_by_series(
        db_session, series_key="Nonexistent Series",
    )
    assert flipped == 0


async def test_exclude_drops_group_from_review_queue(db_session):
    """Regression for the user-reported bug: excluding a series via
    ``exclude_files_by_series`` should make the group disappear from
    the next ``list_pending_groups`` render. The queue queries now
    filter on ``excluded_from_matching`` at both the cheap pre-pass
    and the enriched pass."""
    # Three pending files in one group + one in an unrelated group.
    for i, n in enumerate([1, 2, 3]):
        await _pending_file(
            db_session,
            f"{830 + i:064x}",
            filename=f"Excluded Series {n:03d}.cbz",
        )
    await _pending_file(
        db_session, f"{840:064x}", filename="Kept Series 001.cbz",
    )
    await db_session.commit()

    # Before exclusion both groups are visible.
    groups, _total, _capped = await list_pending_groups(db_session)
    keys = sorted(g.series_key or "" for g in groups)
    assert "Excluded Series" in keys
    assert "Kept Series" in keys

    # Excluding the first series drops it from the next render.
    flipped = await exclude_files_by_series(
        db_session, series_key="Excluded Series"
    )
    assert flipped == 3
    groups, _total, _capped = await list_pending_groups(db_session)
    keys = sorted(g.series_key or "" for g in groups)
    assert "Excluded Series" not in keys
    assert "Kept Series" in keys
