"""Admin CV-key + add-volume endpoint tests.

Exercises the admin flow end-to-end through the HTTPX client fixture:
- save the CV API key
- attempt to add a volume with no key configured (clean error)
- add a volume with the key configured (synchronous fetch through respx)
"""

import httpx
import pytest
import respx

from app.auth.passwords import hash_password
from app.comicvine.client import BASE_URL
from app.models import CvVolume, User, UserRole
from app.services.settings import get_cv_api_key, set_cv_api_key

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.comicvine.client.ComicVineClient._sleep_with_backoff",
        staticmethod(_instant),
    )


async def _make_admin_and_login(client, db_session):
    db_session.add(
        User(
            username="admin",
            password_hash=hash_password("adminpass1"),
            role=UserRole.ADMIN,
        )
    )
    await db_session.commit()
    r = await client.post(
        "/login", data={"username": "admin", "password": "adminpass1"}
    )
    assert r.status_code == 303


# ---- /admin/cv-key ------------------------------------------------------


async def test_save_cv_key(client, db_session, monkeypatch):
    """The admin save_cv_key route validates against ComicVine before
    storing (catching typos here rather than three minutes later when
    the matcher starts failing) and persists the cleaned value.

    The route no longer fires a Match-all pass on first save — the
    scanner enqueues every file unconditionally and the match worker
    holds those jobs (rescheduling themselves) until the key is set.
    Saving the key here is enough to unblock the held jobs."""

    async def fake_validate(key, *, http=None):
        return True, None

    monkeypatch.setattr(
        "app.comicvine.client.validate_cv_api_key", fake_validate,
    )

    await _make_admin_and_login(client, db_session)
    r = await client.post("/admin/cv-key", data={"api_key": "  s3cret-key  "})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?cv_key=saved"
    assert await get_cv_api_key(db_session) == "s3cret-key"


async def test_save_cv_key_rejects_empty(client, db_session):
    await _make_admin_and_login(client, db_session)
    r = await client.post("/admin/cv-key", data={"api_key": "   "})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?cv_key=empty"
    assert await get_cv_api_key(db_session) is None


# ---- /admin/volume ------------------------------------------------------


async def test_add_volume_with_no_key_returns_clean_error(client, db_session):
    await _make_admin_and_login(client, db_session)
    r = await client.post("/admin/volume", data={"cv_id": "18166"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?volume_error=no_key"


async def test_add_volume_with_non_integer_id(client, db_session):
    await _make_admin_and_login(client, db_session)
    r = await client.post("/admin/volume", data={"cv_id": "not-a-number"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?volume_error=bad_id"


@respx.mock
async def test_add_volume_happy_path(client, db_session):
    await _make_admin_and_login(client, db_session)
    await set_cv_api_key(db_session, "test-key")
    await db_session.commit()

    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": "OK",
                "status_code": 1,
                "results": {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [
                        {
                            "id": 100,
                            "issue_number": "1",
                            "name": "ch1",
                            "cover_date": "2012-03-14",
                        }
                    ],
                },
                "version": "1.0",
            },
        )
    )
    r = await client.post("/admin/volume", data={"cv_id": "18166"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?volume_added=18166"

    vol = await db_session.get(CvVolume, 18166)
    assert vol is not None
    assert vol.name == "Saga"


@respx.mock
async def test_add_volume_with_invalid_key_surfaces_distinct_error(client, db_session):
    await _make_admin_and_login(client, db_session)
    await set_cv_api_key(db_session, "bogus-key")
    await db_session.commit()
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": "Invalid API Key",
                "status_code": 100,
                "results": [],
                "version": "1.0",
            },
        )
    )
    r = await client.post("/admin/volume", data={"cv_id": "1"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?volume_error=invalid_key"


async def test_add_volume_requires_admin(client, db_session):
    # Sign in as a viewer.
    db_session.add(
        User(
            username="bob",
            password_hash=hash_password("viewerpass1"),
            role=UserRole.VIEWER,
        )
    )
    await db_session.commit()
    r = await client.post(
        "/login", data={"username": "bob", "password": "viewerpass1"}
    )
    assert r.status_code == 303
    r = await client.post("/admin/volume", data={"cv_id": "1"})
    assert r.status_code == 403
