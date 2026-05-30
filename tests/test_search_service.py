"""Tests for app.services.search — library and CV search.

Builds small in-memory cache rows (volumes / issues / characters /
creators / teams / arcs / local volumes), files matched to a subset,
and asserts:

- Each kind returns hits for matching names.
- Ranking puts starts-with matches above contains matches and short
  names above long ones for ties.
- ``owned`` is True for volumes/issues with a matched file and False
  otherwise; local volumes are always True.
- ``limit_per_kind`` caps each kind's list.
- A query under ``MIN_QUERY_LENGTH`` returns an empty results object
  without touching the database.
- Case-insensitive matching.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.models import (
    ComicInfoStatus,
    CvCharacter,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvStoryArc,
    CvTeam,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    LocalVolume,
    MatchSource,
    MatchStatus,
)
from app.services.search import (
    MIN_QUERY_LENGTH,
    cv_search_catalogue,
    search_library,
)

pytestmark = pytest.mark.asyncio


# ---- Tiny model factories ----------------------------------------------


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
    year: int = 2012,
    publisher_cv_id: int | None = None,
) -> CvVolume:
    return CvVolume(
        cv_id=cv_id,
        name=name,
        year=year,
        publisher_cv_id=publisher_cv_id,
        count_of_issues=12,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": {"thumb_url": f"https://example.com/{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
    )


def _issue(cv_id: int, volume_cv_id: int, name: str, number: str = "1") -> CvIssue:
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=volume_cv_id,
        issue_number=number,
        cover_date=date(2012, 1, 1),
        name=name,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": {"thumb_url": f"https://example.com/issue-{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
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


def _character(cv_id: int, name: str) -> CvCharacter:
    return CvCharacter(
        cv_id=cv_id,
        name=name,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": {"icon_url": f"https://example.com/c-{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
    )


def _person(cv_id: int, name: str) -> CvPerson:
    return CvPerson(
        cv_id=cv_id,
        name=name,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": {"icon_url": f"https://example.com/p-{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
    )


def _team(cv_id: int, name: str) -> CvTeam:
    return CvTeam(
        cv_id=cv_id,
        name=name,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": {"icon_url": f"https://example.com/t-{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
    )


def _arc(cv_id: int, name: str) -> CvStoryArc:
    return CvStoryArc(
        cv_id=cv_id,
        name=name,
        raw_payload={
            "id": cv_id,
            "name": name,
            "image": {"thumb_url": f"https://example.com/arc-{cv_id}.jpg"},
        },
        fetched_at=datetime.now(tz=UTC),
    )


# ---- Tests -------------------------------------------------------------


async def test_short_query_returns_empty(db_session):
    """One char under the threshold => no DB hit, empty results."""
    db_session.add(_volume(1, "X-Men"))
    await db_session.commit()

    results = await search_library(db_session, "x")
    assert results.is_empty
    assert results.query == "x"


async def test_finds_volumes_by_name(db_session):
    pub = _publisher(31, "Image")
    v1 = _volume(101, "Saga", publisher_cv_id=31)
    v2 = _volume(102, "X-Men")
    db_session.add_all([pub, v1, v2])
    await db_session.commit()

    results = await search_library(db_session, "sag")
    names = [h.name for h in results.volumes]
    assert "Saga" in names
    assert "X-Men" not in names
    # Subtitle picks up the publisher + year.
    saga = next(h for h in results.volumes if h.name == "Saga")
    assert "Image" in saga.subtitle
    assert "2012" in saga.subtitle


async def test_owned_volume_ranks_above_unowned(db_session):
    """A volume the user has a file for should be flagged owned and sort
    above a same-name un-owned volume."""
    # Two volumes named "Saga", different cv_ids; only one has a match.
    v_owned = _volume(201, "Saga")
    v_unowned = _volume(202, "Saga Tales")
    db_session.add_all([v_owned, v_unowned])
    await db_session.flush()  # volumes must exist before issue FKs them
    db_session.add(_issue(2001, 201, "Issue Title"))
    await db_session.flush()

    f = _file("a" * 64)
    db_session.add(f)
    await db_session.flush()
    db_session.add_all([_location(f.id, "/lib/saga01.cbz"), _match(f.id, 2001)])
    await db_session.commit()

    results = await search_library(db_session, "saga")
    assert len(results.volumes) == 2
    # First hit should be the owned one — owned: true puts it on top.
    assert results.volumes[0].name == "Saga"
    assert results.volumes[0].owned is True
    assert results.volumes[1].name == "Saga Tales"
    assert results.volumes[1].owned is False


async def test_starts_with_ranks_above_contains(db_session):
    """Among owned-equal volumes, prefix matches outrank substring
    matches."""
    v_prefix = _volume(301, "Hawkeye")
    v_contains = _volume(302, "Marvel's Hawkeye Adventures")
    db_session.add_all([v_prefix, v_contains])
    await db_session.commit()

    results = await search_library(db_session, "hawk")
    assert [h.name for h in results.volumes] == [
        "Hawkeye",
        "Marvel's Hawkeye Adventures",
    ]


async def test_limit_per_kind_caps_each_list(db_session):
    db_session.add_all([_volume(400 + i, f"Saga Volume {i}") for i in range(10)])
    await db_session.commit()

    results = await search_library(db_session, "saga", limit_per_kind=3)
    assert len(results.volumes) == 3


async def test_local_volumes_searched_and_always_owned(db_session):
    db_session.add(LocalVolume(name="My Homebrew Comics", publisher_name="Self"))
    await db_session.commit()

    results = await search_library(db_session, "homebrew")
    assert len(results.local_volumes) == 1
    hit = results.local_volumes[0]
    assert hit.kind == "local_volume"
    assert hit.owned is True
    assert hit.detail_url.startswith("/local/volume/")


async def test_issues_only_returned_when_owned(db_session):
    """An issue with no file_matches row should NOT appear, even if its
    name matches. An issue with an AUTO match should appear."""
    db_session.add(_volume(500, "Some Volume"))
    await db_session.flush()  # volume must exist before issue FKs to it
    db_session.add_all(
        [
            _issue(5001, 500, "Origin of Wolverine"),
            _issue(5002, 500, "Origin of Cyclops"),
        ]
    )
    await db_session.flush()

    f = _file("b" * 64)
    db_session.add(f)
    await db_session.flush()
    db_session.add_all([_location(f.id, "/lib/x.cbz"), _match(f.id, 5001)])
    await db_session.commit()

    results = await search_library(db_session, "origin")
    names = [h.name for h in results.issues]
    assert names == ["Origin of Wolverine"]


async def test_characters_creators_teams_arcs(db_session):
    db_session.add_all(
        [
            _character(600, "Wolverine"),
            _character(601, "Wonder Woman"),
            _person(700, "Brian K. Vaughan"),
            _team(800, "X-Men"),
            _arc(900, "Wolverine: Old Man Logan"),
        ]
    )
    await db_session.commit()

    results = await search_library(db_session, "wol")
    assert {h.name for h in results.characters} == {"Wolverine"}
    assert {h.name for h in results.creators} == set()
    assert {h.name for h in results.teams} == set()
    assert {h.name for h in results.arcs} == {"Wolverine: Old Man Logan"}

    results = await search_library(db_session, "vaughan")
    assert [h.name for h in results.creators] == ["Brian K. Vaughan"]


async def test_finds_credit_stubs_in_owned_issues(db_session):
    """Characters / creators / teams / arcs mentioned by an owned issue's
    raw_payload credits should appear in search even when nothing has
    hydrated the matching cv_characters etc. row yet — that's the
    common case before anyone visits the detail page."""
    db_session.add(_volume(1100, "Saga of Stubs"))
    await db_session.flush()  # volume must exist before issue FKs to it
    db_session.add(
        CvIssue(
            cv_id=11001,
            volume_cv_id=1100,
            issue_number="1",
            cover_date=date(2012, 1, 1),
            name="The Stubs Begin",
            raw_payload={
                "id": 11001,
                "name": "The Stubs Begin",
                "image": {},
                "character_credits": [
                    {"id": 50001, "name": "Stubcharacter"},
                    {"id": 50002, "name": "Otherperson"},
                ],
                "person_credits": [
                    {"id": 50101, "name": "Stubcreator", "role": "writer"},
                ],
                "team_credits": [
                    {"id": 50201, "name": "Stubteam"},
                ],
                "story_arc_credits": [
                    {"id": 50301, "name": "Stubarc"},
                ],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()

    f = _file("c" * 64)
    db_session.add(f)
    await db_session.flush()
    db_session.add_all([_location(f.id, "/lib/stubs.cbz"), _match(f.id, 11001)])
    await db_session.commit()

    results = await search_library(db_session, "stub")
    assert {h.name for h in results.characters} == {"Stubcharacter"}
    assert next(h for h in results.characters).detail_url == "/character/50001"
    assert {h.name for h in results.creators} == {"Stubcreator"}
    assert next(h for h in results.creators).detail_url == "/creator/50101"
    assert {h.name for h in results.teams} == {"Stubteam"}
    assert next(h for h in results.teams).detail_url == "/team/50201"
    assert {h.name for h in results.arcs} == {"Stubarc"}
    assert next(h for h in results.arcs).detail_url == "/arc/50301"


async def test_hydrated_rows_dedupe_with_credit_stubs(db_session):
    """A character that's BOTH hydrated AND mentioned in an owned issue
    should appear once, with the hydrated row's data (icon URL)."""
    db_session.add_all(
        [
            _character(60001, "Hydrowolf"),
            _volume(1200, "Hydra Vol"),
        ]
    )
    await db_session.flush()  # volume must exist before issue FKs to it
    db_session.add(
        CvIssue(
            cv_id=12001,
            volume_cv_id=1200,
            issue_number="1",
            cover_date=date(2012, 1, 1),
            name=None,
            raw_payload={
                "id": 12001,
                "image": {},
                "character_credits": [{"id": 60001, "name": "Hydrowolf"}],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()

    f = _file("d" * 64)
    db_session.add(f)
    await db_session.flush()
    db_session.add_all([_location(f.id, "/lib/hydra.cbz"), _match(f.id, 12001)])
    await db_session.commit()

    results = await search_library(db_session, "hydrowolf")
    assert len(results.characters) == 1
    # Hydrated row wins — it has a cover_url from raw_payload.image.icon_url.
    h = results.characters[0]
    assert h.name == "Hydrowolf"
    assert h.cover_url is not None


async def test_arc_names_apply_parse_arc_name(db_session):
    """CV's ``"<book>" <arc>`` prefix should be stripped from the
    display name and surfaced as the subtitle — matches the
    rendering on /character, /creator, /team's arc tabs."""
    db_session.add_all(
        [
            _arc(7001, '"Avengers" Dark Reign'),
            _arc(7002, '"Avengers/X-Men" Avengers vs. X-Men'),
            _arc(7003, "Plain Arc Name"),
        ]
    )
    await db_session.commit()

    results = await search_library(db_session, "aven")
    by_id = {int(h.key): h for h in results.arcs}
    # Quoted prefix peels off into the subtitle.
    dark_reign = by_id[7001]
    assert dark_reign.name == "Dark Reign"
    assert dark_reign.subtitle == "Avengers"
    # Slashes inside the prefix survive intact.
    avx = by_id[7002]
    assert avx.name == "Avengers vs. X-Men"
    assert avx.subtitle == "Avengers/X-Men"

    # An arc with no quoted prefix passes through untouched and gets
    # no subtitle (no parent book to show).
    results2 = await search_library(db_session, "plain")
    plain = next(h for h in results2.arcs if int(h.key) == 7003)
    assert plain.name == "Plain Arc Name"
    assert plain.subtitle == ""


async def test_credit_stubs_only_from_owned_issues(db_session):
    """A character mentioned only in an UN-owned issue's payload should
    NOT appear — it would surface entries the user has no library
    connection to and quickly become noise."""
    db_session.add(_volume(1300, "Lonely Vol"))
    await db_session.flush()  # volume must exist before issue FKs to it
    # Issue exists with a credit list, but no file_matches row.
    db_session.add(
        CvIssue(
            cv_id=13001,
            volume_cv_id=1300,
            issue_number="1",
            cover_date=date(2012, 1, 1),
            name=None,
            raw_payload={
                "id": 13001,
                "image": {},
                "character_credits": [{"id": 70001, "name": "Ghostperson"}],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    results = await search_library(db_session, "ghost")
    assert results.characters == []


async def test_case_insensitive(db_session):
    db_session.add(_volume(1001, "Saga"))
    await db_session.commit()

    assert (await search_library(db_session, "SAGA")).volumes
    assert (await search_library(db_session, "sAgA")).volumes


async def test_min_query_length_constant_matches_service(db_session):
    """Sanity: the threshold is documented + exported."""
    assert MIN_QUERY_LENGTH >= 1


async def test_more_available_flags_when_overflow(db_session):
    """Asking for ``limit_per_kind=3`` against 5 matching rows should
    return 3 hits *and* set ``more_available`` for that kind — what
    drives the search-page's "View all" link."""
    db_session.add_all([_volume(7700 + i, f"Saga Vol {i}") for i in range(5)])
    await db_session.commit()

    r = await search_library(db_session, "saga", limit_per_kind=3)
    assert len(r.volumes) == 3
    assert "volumes" in r.more_available


async def test_more_available_unset_when_under_cap(db_session):
    """At-or-under the cap → no overflow → no "View all" link."""
    db_session.add_all([_volume(7800 + i, f"Saga Vol {i}") for i in range(2)])
    await db_session.commit()

    r = await search_library(db_session, "saga", limit_per_kind=3)
    assert len(r.volumes) == 2
    assert "volumes" not in r.more_available


async def test_only_kind_restricts_to_one_section(db_session):
    """``only_kind="characters"`` runs only the character query — the
    backbone of the /search?kind=... view."""
    db_session.add_all([_volume(7900, "Wolverine"), _character(7901, "Wolverine")])
    await db_session.commit()

    r = await search_library(db_session, "wol", limit_per_kind=10, only_kind="characters")
    assert r.volumes == []
    assert [h.name for h in r.characters] == ["Wolverine"]


async def test_only_kind_unknown_falls_back_to_all(db_session):
    """Unknown ``only_kind`` (URL tampering / typo) falls back to the
    full search rather than 404'ing or returning empty."""
    db_session.add(_character(8001, "Wolverine"))
    await db_session.commit()

    r = await search_library(db_session, "wol", limit_per_kind=10, only_kind="frobnitz")
    assert [h.name for h in r.characters] == ["Wolverine"]


# ---- cv_search_catalogue ----------------------------------------------


class _FakeCvCache:
    """Minimal stand-in for ``ComicVineCache``. ``cv_search_catalogue``
    only calls ``.search`` on it, returning the envelope. We avoid
    hitting CV entirely."""

    def __init__(self, envelope: dict):
        self._envelope = envelope
        self.calls: list[dict] = []

    async def search(self, db, query, *, resources, limit, force_refresh=False):
        self.calls.append({"query": query, "resources": resources, "limit": limit})
        return self._envelope


async def test_cv_search_partitions_by_resource_type(db_session):
    """A mixed CV envelope is partitioned into per-kind SearchHit
    lists keyed by ``resource_type``."""
    envelope = {
        "results": [
            {
                "resource_type": "volume",
                "id": 4050,
                "name": "Avengers",
                "start_year": "1963",
                "publisher": {"id": 31, "name": "Marvel"},
                "image": {"thumb_url": "https://example.com/v.jpg"},
            },
            {
                "resource_type": "issue",
                "id": 6001,
                "name": "Origin",
                "issue_number": "1",
                "volume": {"id": 4050, "name": "Avengers"},
                "image": {"thumb_url": "https://example.com/i.jpg"},
            },
            {
                "resource_type": "character",
                "id": 1234,
                "name": "Hawkeye",
                "image": {"icon_url": "https://example.com/c.jpg"},
            },
            {
                "resource_type": "person",
                "id": 5555,
                "name": "Brian K. Vaughan",
                "image": {"icon_url": "https://example.com/p.jpg"},
            },
            {
                "resource_type": "team",
                "id": 7777,
                "name": "X-Men",
                "image": {"icon_url": "https://example.com/t.jpg"},
            },
            {
                "resource_type": "story_arc",
                "id": 9001,
                "name": '"Avengers" Dark Reign',
                "image": {"thumb_url": "https://example.com/a.jpg"},
            },
        ]
    }
    cache = _FakeCvCache(envelope)

    results = await cv_search_catalogue(db_session, cache, "avengers", limit_per_kind=10)

    assert len(cache.calls) == 1
    assert cache.calls[0]["resources"] == "volume,issue,character,person,team,story_arc"

    # Volume — publisher + year baked into subtitle, link to local page.
    vol = results.volumes[0]
    assert vol.name == "Avengers"
    assert "Marvel" in vol.subtitle and "1963" in vol.subtitle
    assert vol.detail_url == "/volume/4050"
    assert vol.owned is False  # CV results aren't ownership-aware

    # Issue — volume name + issue number in subtitle.
    iss = results.issues[0]
    assert iss.name == "Origin"
    assert "Avengers" in iss.subtitle and "#1" in iss.subtitle
    assert iss.detail_url == "/issue/6001"

    # CV's "person" maps to our "creator" UI kind. Same for "story_arc"
    # → "arc".
    assert results.creators[0].name == "Brian K. Vaughan"
    assert results.creators[0].detail_url == "/creator/5555"
    assert results.arcs[0].name == "Dark Reign"  # quoted prefix peeled off
    assert results.arcs[0].subtitle == "Avengers"
    assert results.arcs[0].detail_url == "/arc/9001"

    assert results.characters[0].name == "Hawkeye"
    assert results.teams[0].name == "X-Men"

    # Local volumes are not a CV concept — empty.
    assert results.local_volumes == []


async def test_cv_search_short_query_bypasses_cv(db_session):
    """A query under MIN_QUERY_LENGTH must not touch CV (rate budget)."""
    cache = _FakeCvCache({"results": []})
    results = await cv_search_catalogue(db_session, cache, "a", limit_per_kind=10)
    assert cache.calls == []
    assert results.is_empty


async def test_cv_search_overflow_sets_more_available(db_session):
    """If CV returns more volumes than the cap, ``more_available``
    flags it so the page can mark the section as 'view all' (or just
    show the cap)."""
    envelope = {
        "results": [
            {
                "resource_type": "volume",
                "id": 5000 + i,
                "name": f"Saga Vol {i}",
                "image": {},
            }
            for i in range(12)
        ]
    }
    cache = _FakeCvCache(envelope)
    results = await cv_search_catalogue(db_session, cache, "saga", limit_per_kind=5)
    assert len(results.volumes) == 5
    assert "volumes" in results.more_available


async def test_cv_search_only_kind_narrows_resources(db_session):
    """``only_kind='characters'`` must hit CV with a single-resource
    query (``resources='character'``) — the drill-down should spend
    its entire rate budget on one bucket."""
    cache = _FakeCvCache({"results": []})
    await cv_search_catalogue(db_session, cache, "wol", limit_per_kind=10, only_kind="characters")
    assert cache.calls[-1]["resources"] == "character"
    # And the limit is the smaller per-bucket probe, not the wider
    # multi-resource fan-out.
    assert cache.calls[-1]["limit"] <= 11


async def test_cv_search_only_kind_unknown_falls_back_to_all(db_session):
    """An unrecognised ``only_kind`` falls back to the multi-resource
    call, mirroring the library search's tolerance for URL-tampered
    kind values."""
    cache = _FakeCvCache({"results": []})
    await cv_search_catalogue(db_session, cache, "wol", limit_per_kind=10, only_kind="frobnitz")
    assert cache.calls[-1]["resources"] == "volume,issue,character,person,team,story_arc"


async def test_cv_search_skips_bad_rows(db_session):
    """Rows without an integer id, or with an unknown resource_type,
    are dropped silently — CV's envelope is occasionally noisy."""
    envelope = {
        "results": [
            {"resource_type": "volume"},  # missing id
            {"resource_type": "volume", "id": "not-int", "name": "x"},
            {"resource_type": "unknown_thing", "id": 1, "name": "x"},
            {"resource_type": "volume", "id": 99, "name": "Real Vol", "image": {}},
        ]
    }
    cache = _FakeCvCache(envelope)
    results = await cv_search_catalogue(db_session, cache, "real", limit_per_kind=10)
    assert [h.name for h in results.volumes] == ["Real Vol"]
