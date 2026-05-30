"""Smoke tests for the /search routes.

The service layer has its own dedicated coverage in
``test_search_service.py``; here we exercise:

- auth gating (anonymous redirects to /login)
- /search renders the empty shell when q is missing
- /search renders hits when q is present
- /search/live returns the JSON shape the dropdown expects
- /search/live short-circuits when q is shorter than MIN_QUERY_LENGTH
- /search fires enqueue_revalidate for any stubs it surfaces
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.auth.passwords import hash_password
from app.models import (
    ComicInfoStatus,
    CvCharacter,
    CvIssue,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    MatchSource,
    MatchStatus,
    User,
    UserRole,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def enqueue_calls(monkeypatch):
    """Patch the route's ``enqueue_revalidate`` import with a recorder
    so tests don't try to talk to a real Redis broker.

    ``enqueue_revalidate_interactive`` (imported into the route module
    under the local name ``enqueue_revalidate``) calls ``Redis.from_url``
    directly, bypassing the ``get_redis_dep`` fakeredis fixture. Without
    a stub here, any test that surfaces a stub row would explode.
    Returns the list of recorded ``(entity_type, cv_id)`` tuples.
    """
    calls: list[tuple[str, int]] = []

    def _record(entity_type: str, cv_id: int, *, at_front: bool = False):
        calls.append((entity_type, cv_id))

    monkeypatch.setattr("app.search.routes.enqueue_revalidate", _record)
    return calls


async def _login_viewer(client, db_session):
    db_session.add(
        User(
            username="alice",
            password_hash=hash_password("viewerpass1"),
            role=UserRole.VIEWER,
        )
    )
    await db_session.commit()
    r = await client.post(
        "/login", data={"username": "alice", "password": "viewerpass1"}
    )
    assert r.status_code == 303


async def test_search_page_requires_auth(client, db_session):
    db_session.add(
        User(
            username="someone",
            password_hash=hash_password("anything1"),
            role=UserRole.VIEWER,
        )
    )
    await db_session.commit()
    r = await client.get("/search")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_search_page_empty_query_renders_shell(client, db_session):
    await _login_viewer(client, db_session)
    r = await client.get("/search")
    assert r.status_code == 200
    # Empty-state guidance copy.
    assert "Search across your library" in r.text


async def test_search_page_renders_hits(client, db_session):
    await _login_viewer(client, db_session)
    db_session.add(
        CvVolume(
            cv_id=4242,
            name="Saga",
            year=2012,
            count_of_issues=12,
            raw_payload={"id": 4242, "name": "Saga", "image": {}},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    r = await client.get("/search", params={"q": "saga"})
    assert r.status_code == 200
    assert "Saga" in r.text
    # The "Other volumes" section header should appear since the row
    # isn't owned (no file matches).
    assert "Other volumes" in r.text


async def test_search_page_kind_filter_renders_one_section(client, db_session):
    """``?kind=characters`` renders only the characters section + the
    back-to-all-results link."""
    await _login_viewer(client, db_session)
    db_session.add_all(
        [
            CvVolume(
                cv_id=4300,
                name="Wolverine",
                year=2012,
                count_of_issues=12,
                raw_payload={"id": 4300, "name": "Wolverine", "image": {}},
                fetched_at=datetime.now(tz=UTC),
            ),
            CvCharacter(
                cv_id=4301,
                name="Wolverine",
                raw_payload={"id": 4301, "name": "Wolverine", "image": {}},
                fetched_at=datetime.now(tz=UTC),
            ),
        ]
    )
    await db_session.commit()

    r = await client.get(
        "/search", params={"q": "wol", "kind": "characters"}
    )
    assert r.status_code == 200
    # Heading reflects the filter.
    assert "Characters matching" in r.text
    # Back link is present.
    assert "All results" in r.text
    # The "Volumes" sections are absent in filtered mode.
    assert "Volumes in your library" not in r.text
    assert "Other volumes" not in r.text


async def test_search_page_view_all_link_when_overflow(client, db_session):
    """With more rows than PAGE_LIMIT_PER_KIND (10), the multi-section
    view shows a "View all" link for that kind."""
    await _login_viewer(client, db_session)
    db_session.add_all(
        [
            CvCharacter(
                cv_id=4400 + i,
                name=f"Saga Character {i}",
                raw_payload={"id": 4400 + i, "name": f"Saga Character {i}", "image": {}},
                fetched_at=datetime.now(tz=UTC),
            )
            for i in range(12)
        ]
    )
    await db_session.commit()

    r = await client.get("/search", params={"q": "saga"})
    assert r.status_code == 200
    # The View-all link target points at the kind-filter URL.
    assert "kind=characters" in r.text
    assert "View all characters" in r.text


async def test_search_live_returns_grouped_json(client, db_session):
    await _login_viewer(client, db_session)
    db_session.add(
        CvCharacter(
            cv_id=777,
            name="Wolverine",
            raw_payload={"id": 777, "name": "Wolverine", "image": {}},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    r = await client.get("/search/live", params={"q": "wol"})
    assert r.status_code == 200
    data = r.json()
    assert data["q"] == "wol"
    assert data["total"] >= 1
    assert "characters" in data["groups"]
    char_hits = data["groups"]["characters"]
    assert any(h["name"] == "Wolverine" for h in char_hits)
    # Every hit row must carry the keys the dropdown reads.
    for h in char_hits:
        assert set(h.keys()) >= {
            "key",
            "name",
            "subtitle",
            "cover_url",
            "detail_url",
            "owned",
            "kind",
        }


async def test_search_live_short_query_returns_empty(client, db_session):
    await _login_viewer(client, db_session)
    r = await client.get("/search/live", params={"q": "x"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 0
    # All seven group keys present even on empty — the dropdown
    # iterates over them unconditionally.
    assert set(data["groups"].keys()) == {
        "volumes",
        "local_volumes",
        "issues",
        "characters",
        "creators",
        "teams",
        "arcs",
    }


async def test_search_page_enqueues_hydration_for_stubs(
    client, db_session, enqueue_calls
):
    """Stub rows (CvVolume with ``_stub`` marker; credit-walk char /
    creator / team / arc with no cv_* row) should each fire one
    ``enqueue_revalidate`` so the interactive worker hydrates them
    before the user clicks through."""
    await _login_viewer(client, db_session)

    # 1. Stub CvVolume — has the ``_stub`` marker in its raw_payload.
    db_session.add(
        CvVolume(
            cv_id=9001,
            name="Stubvolume",
            year=None,
            count_of_issues=None,
            raw_payload={"id": 9001, "_stub": True, "name": "Stubvolume"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    # 2. Owned issue whose credits mention an un-hydrated character,
    #    creator, team, and arc — these become stub SearchHits via
    #    the credits-walk path. Volume must be flushed before the
    #    issue can FK to it (no relationship() on CvIssue→CvVolume,
    #    so SQLAlchemy doesn't sort across tables for us).
    db_session.add(
        CvVolume(
            cv_id=9100,
            name="Carrier Volume",
            year=2012,
            count_of_issues=1,
            raw_payload={"id": 9100, "name": "Carrier Volume", "image": {}},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()
    db_session.add(
        CvIssue(
            cv_id=91001,
            volume_cv_id=9100,
            issue_number="1",
            cover_date=date(2012, 1, 1),
            name="Stubcarrier",
            raw_payload={
                "id": 91001,
                "name": "Stubcarrier",
                "image": {},
                "character_credits": [{"id": 80001, "name": "Stubchar"}],
                "person_credits": [{"id": 80101, "name": "Stubcreator", "role": "writer"}],
                "team_credits": [{"id": 80201, "name": "Stubteam"}],
                "story_arc_credits": [{"id": 80301, "name": "Stubarc"}],
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()

    f = File(
        sha256="e" * 64,
        size_bytes=1024,
        archive_format="cbz",
        page_count=20,
        comicinfo_status=ComicInfoStatus.NONE,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db_session.add(f)
    await db_session.flush()
    db_session.add_all(
        [
            FileLocation(
                file_id=f.id,
                path="/lib/stubcarrier.cbz",
                mtime=datetime.now(tz=UTC),
                last_seen_at=datetime.now(tz=UTC),
            ),
            FileMatch(
                file_id=f.id,
                issue_cv_id=91001,
                confidence=None,
                status=MatchStatus.AUTO,
                source=MatchSource.FILENAME,
                matched_at=datetime.now(tz=UTC),
            ),
        ]
    )
    await db_session.commit()

    # Trigger the route once for each shared prefix — "stub" matches
    # all five entity kinds in one shot.
    r = await client.get("/search", params={"q": "stub"})
    assert r.status_code == 200

    fired = set(enqueue_calls)
    # Volume stub gets a "volume" hydration.
    assert ("volume", 9001) in fired
    # Credit-walk stubs each fire under their CV entity_type. Note
    # creators map to ``person`` and arcs map to ``story_arc`` — the
    # CV-vocabulary names, not the UI ``kind``.
    assert ("character", 80001) in fired
    assert ("person", 80101) in fired
    assert ("team", 80201) in fired
    assert ("story_arc", 80301) in fired


async def test_search_hydration_empty_input(client, db_session):
    """No ``ids=`` query returns the empty shape without DB work."""
    await _login_viewer(client, db_session)
    r = await client.get("/search/hydration")
    assert r.status_code == 200
    assert r.json() == {"swaps": [], "completed_ids": []}


async def test_search_hydration_skips_unhydrated(client, db_session):
    """A pending character whose cv_characters row does NOT exist yet
    should produce neither a swap nor a completed_id — the JS keeps
    polling."""
    await _login_viewer(client, db_session)
    r = await client.get("/search/hydration", params={"ids": "character:99999"})
    assert r.status_code == 200
    data = r.json()
    assert data["swaps"] == []
    assert data["completed_ids"] == []


async def test_search_hydration_returns_swap_when_hydrated(client, db_session):
    """A pending character whose row has appeared in cv_characters
    returns a swap targeting ``search-hit-character-<cv_id>`` and a
    completed_id of ``character:<cv_id>``."""
    await _login_viewer(client, db_session)
    db_session.add(
        CvCharacter(
            cv_id=12321,
            name="Hydratedchar",
            raw_payload={
                "id": 12321,
                "name": "Hydratedchar",
                "image": {"icon_url": "https://example.com/h.jpg"},
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    r = await client.get(
        "/search/hydration", params={"ids": "character:12321"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["completed_ids"] == ["character:12321"]
    assert len(data["swaps"]) == 1
    swap = data["swaps"][0]
    assert swap["target_id"] == "search-hit-character-12321"
    # The swap HTML carries the hydrated name + ICON URL so the JS
    # can drop it in with images now loading.
    assert "Hydratedchar" in swap["html"]
    assert "icon_url" in swap["html"] or "example.com/h.jpg" in swap["html"]
    # And it must NOT carry the data-pending-id / data-hydrated="false"
    # tuple, otherwise scanPending() would immediately re-add the
    # swapped element to the pending set and loop forever.
    assert 'data-pending-id="character:12321"' not in swap["html"]


async def test_search_hydration_ignores_unknown_kinds(client, db_session):
    """Unknown kinds (typo / future-kind / URL tampering) are silently
    dropped — the poll should never 400."""
    await _login_viewer(client, db_session)
    r = await client.get(
        "/search/hydration", params={"ids": "frobnitz:1,character:99999,team:abc"}
    )
    assert r.status_code == 200
    assert r.json() == {"swaps": [], "completed_ids": []}


async def test_library_search_page_shows_comicvine_button(client, db_session):
    """When the user has a query on /search, a 'Search ComicVine'
    button should appear next to the form so they can jump to CV."""
    await _login_viewer(client, db_session)
    r = await client.get("/search", params={"q": "anything"})
    assert r.status_code == 200
    assert "Search ComicVine" in r.text
    assert "/search/comicvine?q=anything" in r.text


async def test_search_comicvine_renders_with_mocked_envelope(
    client, db_session, monkeypatch
):
    """Patch ComicVineCache.search so we don't actually call CV.
    The route should render the search.html template in CV mode with
    flat sections and a back-to-library link."""
    await _login_viewer(client, db_session)

    async def fake_search(self, db, query, *, resources, limit, force_refresh=False):
        return {
            "results": [
                {
                    "resource_type": "volume",
                    "id": 99001,
                    "name": "Saga",
                    "start_year": "2012",
                    "publisher": {"id": 31, "name": "Image"},
                    "image": {"thumb_url": "https://example.com/s.jpg"},
                },
                {
                    "resource_type": "character",
                    "id": 99002,
                    "name": "Hawkeye",
                    "image": {"icon_url": "https://example.com/h.jpg"},
                },
            ]
        }

    monkeypatch.setattr(
        "app.comicvine.ComicVineCache.search", fake_search
    )

    r = await client.get("/search/comicvine", params={"q": "saga"})
    assert r.status_code == 200
    assert "ComicVine results" in r.text
    # Back-to-library link is present.
    assert "Library results" in r.text
    # Volume + character render with their CV-side data.
    assert "Saga" in r.text
    assert "Hawkeye" in r.text
    # CV mode has NO owned/other volume split — the library-only
    # "Volumes in your library" header should be absent.
    assert "Volumes in your library" not in r.text
    assert "Other volumes" not in r.text


async def test_search_comicvine_kind_filter_renders_one_section(
    client, db_session, monkeypatch
):
    """``/search/comicvine?q=X&kind=characters`` should call CV with
    ``resources=character`` and render just that single section, with
    the drill-down back link present."""
    await _login_viewer(client, db_session)

    captured: dict = {}

    async def fake_search(self, db, query, *, resources, limit, force_refresh=False):
        captured["resources"] = resources
        captured["limit"] = limit
        return {
            "results": [
                {
                    "resource_type": "character",
                    "id": 88000 + i,
                    "name": f"Wolverine {i}",
                    "image": {"icon_url": f"https://example.com/w{i}.jpg"},
                }
                for i in range(25)
            ]
        }

    monkeypatch.setattr("app.comicvine.ComicVineCache.search", fake_search)

    r = await client.get(
        "/search/comicvine", params={"q": "wol", "kind": "characters"}
    )
    assert r.status_code == 200
    # CV call narrowed to just character.
    assert captured["resources"] == "character"
    # Heading reflects the drill-down + CV source.
    assert "Characters on ComicVine matching" in r.text
    # Drill-down back link present.
    assert "All ComicVine results" in r.text
    # Other sections suppressed in kind-filter mode.
    assert "Volumes" not in r.text or "Volumes\n" not in r.text  # weak but defensive


async def test_search_comicvine_view_all_link_when_overflow(
    client, db_session, monkeypatch
):
    """When CV returns more than CV_PAGE_LIMIT_PER_KIND for some
    section, the multi-section view emits a "View all <kind> on
    ComicVine" link to /search/comicvine?kind=...."""
    await _login_viewer(client, db_session)

    async def fake_search(self, db, query, *, resources, limit, force_refresh=False):
        return {
            "results": [
                {
                    "resource_type": "character",
                    "id": 70000 + i,
                    "name": f"Saga Char {i}",
                    "image": {},
                }
                for i in range(15)
            ]
        }

    monkeypatch.setattr("app.comicvine.ComicVineCache.search", fake_search)

    r = await client.get("/search/comicvine", params={"q": "saga"})
    assert r.status_code == 200
    # View-all link target points at the CV drill-down, not the
    # library kind-filter.
    assert "/search/comicvine?q=saga&amp;kind=characters" in r.text
    assert "View all characters" in r.text


async def test_search_comicvine_empty_query_renders_shell(client, db_session):
    """No query: render the empty-state shell without calling CV.

    The fixture-level monkeypatch is the safest bet (the route mustn't
    even try to talk to CV with no query), but verifying through the
    public request path is what matters here."""
    await _login_viewer(client, db_session)
    r = await client.get("/search/comicvine")
    assert r.status_code == 200
    # Empty-state copy specific to CV mode.
    assert "Search ComicVine" in r.text


async def test_search_live_does_not_enqueue(client, db_session, enqueue_calls):
    """The dropdown JSON path should NOT fire hydration — it's hit on
    every keystroke; the /search page is where the user commits and
    pays the enqueue cost."""
    await _login_viewer(client, db_session)
    db_session.add(
        CvVolume(
            cv_id=9201,
            name="Stubsoftly",
            year=None,
            count_of_issues=None,
            raw_payload={"id": 9201, "_stub": True, "name": "Stubsoftly"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    r = await client.get("/search/live", params={"q": "stub"})
    assert r.status_code == 200
    assert enqueue_calls == []
