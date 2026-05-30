"""Smoke tests for the library browse routes — auth gating, 404s, and
basic rendering. The service-layer tests cover the query correctness."""

from datetime import UTC, datetime

import pytest

from app.auth.passwords import hash_password
from app.models import CvVolume, User, UserRole

pytestmark = pytest.mark.asyncio


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


async def test_library_requires_auth(client, db_session):
    # Need a user to exist so anonymous lands on /login, not /setup.
    db_session.add(
        User(
            username="someone",
            password_hash=hash_password("anything1"),
            role=UserRole.VIEWER,
        )
    )
    await db_session.commit()
    r = await client.get("/library")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_library_renders_empty_state(client, db_session):
    await _login_viewer(client, db_session)
    r = await client.get("/library")
    assert r.status_code == 200
    # Empty-state message text.
    assert "No volumes match" in r.text or "0 volume" in r.text


async def test_volume_404_for_unknown(client, db_session):
    await _login_viewer(client, db_session)
    r = await client.get("/volume/99999")
    assert r.status_code == 404


async def test_issue_404_for_unknown(client, db_session):
    await _login_viewer(client, db_session)
    # No CV key set, no rows in cv_issues; the cache.get_issue call will
    # raise (no key) which the route swallows → returns None → 404.
    r = await client.get("/issue/99999")
    assert r.status_code == 404


# ---- /volume-credits/hydration ----------------------------------------


async def test_volume_credits_hydration_empty_input(client, db_session):
    await _login_viewer(client, db_session)
    r = await client.get("/volume-credits/hydration")
    assert r.status_code == 200
    assert r.json() == {"swaps": [], "completed_ids": []}


async def test_volume_credits_hydration_skips_stub_volumes(client, db_session):
    """A volume row still flagged ``_stub: True`` in raw_payload is not
    hydrated — the poll should keep it pending (neither swap nor
    completed_id)."""
    await _login_viewer(client, db_session)
    db_session.add(
        CvVolume(
            cv_id=8001,
            name="Stub Volume",
            year=None,
            count_of_issues=None,
            raw_payload={"id": 8001, "_stub": True, "name": "Stub Volume"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()
    r = await client.get(
        "/volume-credits/hydration", params={"ids": "8001"}
    )
    assert r.status_code == 200
    assert r.json() == {"swaps": [], "completed_ids": []}


async def test_volume_credits_hydration_returns_swap_when_hydrated(
    client, db_session
):
    """A non-stub volume returns a swap targeting
    ``volume-credit-<cv_id>`` with the credit link param baked in."""
    await _login_viewer(client, db_session)
    db_session.add(
        CvVolume(
            cv_id=8101,
            name="Hydrated Volume",
            year=2014,
            count_of_issues=24,
            raw_payload={
                "id": 8101,
                "name": "Hydrated Volume",
                "image": {"thumb_url": "https://example.com/h.jpg"},
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    r = await client.get(
        "/volume-credits/hydration",
        params={"ids": "8101", "credit": "character:21599"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["completed_ids"] == [8101]
    assert len(data["swaps"]) == 1
    swap = data["swaps"][0]
    assert swap["target_id"] == "volume-credit-8101"
    # The link target must carry the credit-filter query param so
    # clicking through still narrows /volume to this character's
    # issues — matches the original-render behavior.
    assert "/volume/8101?credit=character:21599" in swap["html"]
    # Hydrated rows omit the data-pending-id markers; otherwise the
    # JS would re-add them to its pending set and loop forever.
    assert 'data-pending-id="8101"' not in swap["html"]
    assert 'data-hydrated="false"' not in swap["html"]
    # The hydrated cover URL is present.
    assert "example.com/h.jpg" in swap["html"]


async def test_volume_credits_hydration_handles_missing_credit_param(
    client, db_session
):
    """No ``credit`` query param → swap link is bare ``/volume/{id}``
    (used by surfaces that don't apply a credit filter)."""
    await _login_viewer(client, db_session)
    db_session.add(
        CvVolume(
            cv_id=8201,
            name="Plain Volume",
            year=2020,
            count_of_issues=6,
            raw_payload={"id": 8201, "name": "Plain Volume", "image": {}},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    r = await client.get(
        "/volume-credits/hydration", params={"ids": "8201"}
    )
    assert r.status_code == 200
    swap = r.json()["swaps"][0]
    assert "/volume/8201" in swap["html"]
    assert "?credit=" not in swap["html"]
