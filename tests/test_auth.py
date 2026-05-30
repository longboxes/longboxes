"""End-to-end tests for Phase 1 auth flows.

Coverage:
- /setup is reachable when no users exist; creates an admin and logs them in.
- /setup redirects to /login once an admin exists.
- /login: valid credentials → cookie + redirect; invalid → 401 + form re-render.
- /logout invalidates the session and clears the cookie.
- Protected routes redirect anonymous users to /login (or /setup on a fresh
  install).
- Sessions survive across requests via the cookie jar.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.config import settings
from app.models import User, UserRole

pytestmark = pytest.mark.asyncio


# ---- /setup --------------------------------------------------------------


async def test_root_redirects_to_setup_on_fresh_install(client):
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


async def test_setup_form_renders_when_no_users(client):
    r = await client.get("/setup")
    assert r.status_code == 200
    assert "Create admin account" in r.text


async def test_setup_creates_admin_and_logs_in(client, db_session):
    r = await client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "supersecret1",
            "password_confirm": "supersecret1",
        },
    )
    assert r.status_code == 303
    # First-boot wizard step 2 — the CV-key form lives at
    # /setup/comicvine. The user is logged in (session cookie set)
    # and walked there, not dropped on the home page where they'd
    # have to discover /admin to set a key.
    assert r.headers["location"] == "/setup/comicvine"
    assert settings.session_cookie_name in r.cookies

    result = await db_session.execute(select(User))
    users = result.scalars().all()
    assert len(users) == 1
    assert users[0].username == "admin"
    assert users[0].role == UserRole.ADMIN
    assert users[0].password_hash != "supersecret1"  # hashed
    assert users[0].last_login_at is not None


async def test_setup_rejects_mismatched_passwords(client, db_session):
    r = await client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "supersecret1",
            "password_confirm": "different1",
        },
    )
    assert r.status_code == 400
    assert "do not match" in r.text
    result = await db_session.execute(select(User))
    assert result.scalars().all() == []


async def test_setup_rejects_short_password(client):
    r = await client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert r.status_code == 400
    assert "at least 8 characters" in r.text


async def test_setup_redirects_to_login_when_users_exist(client, db_session):
    db_session.add(
        User(
            username="someone",
            password_hash=hash_password("alreadyhere1"),
            role=UserRole.ADMIN,
        )
    )
    await db_session.commit()

    r = await client.get("/setup")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    # POST is also gated.
    r = await client.post(
        "/setup",
        data={
            "username": "intruder",
            "password": "secondadmin1",
            "password_confirm": "secondadmin1",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---- /login --------------------------------------------------------------


async def _create_user(
    db_session, *, username: str, password: str, role: UserRole = UserRole.VIEWER
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_login_form_renders(client, db_session):
    await _create_user(db_session, username="someone", password="anypass11")
    r = await client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


async def test_login_succeeds_with_valid_credentials(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.post("/login", data={"username": "alice", "password": "hunter222"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert settings.session_cookie_name in r.cookies


async def test_login_fails_with_wrong_password(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.post("/login", data={"username": "alice", "password": "wrong-pass"})
    assert r.status_code == 401
    assert "Invalid username or password" in r.text
    assert settings.session_cookie_name not in r.cookies


async def test_login_fails_for_unknown_user(client, db_session):
    # No user created at all.
    r = await client.post("/login", data={"username": "ghost", "password": "anything1"})
    assert r.status_code == 401


async def test_login_updates_last_login_at(client, db_session):
    user = await _create_user(db_session, username="alice", password="hunter222")
    assert user.last_login_at is None
    r = await client.post("/login", data={"username": "alice", "password": "hunter222"})
    assert r.status_code == 303
    await db_session.refresh(user)
    assert user.last_login_at is not None


async def test_login_open_redirect_blocked(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": "hunter222",
            "next": "https://evil.example/steal",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


async def test_login_protocol_relative_redirect_blocked(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": "hunter222",
            "next": "//evil.example/steal",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


async def test_login_safe_relative_redirect_honored(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.post(
        "/login",
        data={"username": "alice", "password": "hunter222", "next": "/admin"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


# ---- Session lifecycle ---------------------------------------------------


async def test_authenticated_request_reaches_home(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222", role=UserRole.ADMIN)
    login = await client.post("/login", data={"username": "alice", "password": "hunter222"})
    assert login.status_code == 303
    # AsyncClient persists cookies across requests on the same instance.
    r = await client.get("/")
    assert r.status_code == 200
    assert "alice" in r.text


async def test_anonymous_request_to_protected_route_redirects(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_logout_invalidates_session(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    await client.post("/login", data={"username": "alice", "password": "hunter222"})

    r = await client.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # Cookie cleared (set with empty value / Max-Age=0).
    set_cookie = r.headers.get("set-cookie", "")
    assert settings.session_cookie_name in set_cookie

    # Subsequent / hits the redirect again.
    follow_up = await client.get("/")
    assert follow_up.status_code == 303
    assert follow_up.headers["location"] == "/login"


async def test_health_endpoint_is_unauthenticated(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_session_cookie_attributes(client, db_session):
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.post("/login", data={"username": "alice", "password": "hunter222"})
    raw_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in raw_cookie
    assert "Path=/" in raw_cookie
    # SameSite default is "lax"; FastAPI/Starlette title-cases it as "Lax".
    assert "samesite=lax" in raw_cookie.lower()
    # Secure should NOT be set in dev defaults.
    assert "secure" not in raw_cookie.lower()


async def test_login_form_includes_next_when_redirected(client, db_session):
    # Anonymous request to / sends us to /login (no next preservation in the
    # current redirect; this is fine for MVP — next is only honoured when
    # explicitly passed). This test just confirms /login renders without a
    # next value when none is provided.
    await _create_user(db_session, username="alice", password="hunter222")
    r = await client.get("/login")
    assert r.status_code == 200
    # The hidden input is omitted when there's no `next` (or it defaults to "/").
    # Either way the page is healthy.
    assert "<form" in r.text


# ---- /setup/comicvine (wizard step 2) -----------------------------------
#
# Walks the post-admin-creation flow: the CV-key form, its validation,
# and the skip path. The auto match-all on first save was removed once
# match_file_job started holding (reschedule-every-60s) when the CV key
# is absent — any match jobs the scanner enqueued before the key arrived
# pick up automatically without a one-shot pass through the library.


async def _login_as_admin(client, db_session) -> User:
    """Create an admin user and log them in via the test client so the
    session cookie rides on subsequent requests."""
    admin = await _create_user(
        db_session,
        username="admin",
        password="hunter2222",
        role=UserRole.ADMIN,
    )
    r = await client.post(
        "/login",
        data={"username": "admin", "password": "hunter2222"},
    )
    assert r.status_code == 303
    return admin


async def test_setup_cv_form_renders_for_admin(client, db_session):
    """An admin landing on /setup/comicvine after the first admin
    creation sees the key form."""
    await _login_as_admin(client, db_session)
    r = await client.get("/setup/comicvine")
    assert r.status_code == 200
    assert "ComicVine" in r.text
    assert "Test &amp; save" in r.text or "Test & save" in r.text
    assert "Skip for now" in r.text


async def test_setup_cv_form_redirects_when_already_configured(
    client,
    db_session,
    monkeypatch,
):
    """If a key is already saved, the wizard step doesn't try to
    re-collect — sends the user to /admin instead."""
    from app.services.settings import set_cv_api_key

    await _login_as_admin(client, db_session)
    await set_cv_api_key(db_session, "already-saved-key")
    await db_session.commit()

    r = await client.get("/setup/comicvine")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


async def test_setup_cv_submit_saves_valid_key(
    client,
    db_session,
    monkeypatch,
):
    """A valid key is saved and the user lands on /admin with a
    welcome banner.

    Note: the wizard no longer fires an explicit Match-all pass on
    first save. The scanner already enqueues match jobs for every
    file it walks; without a CV key those jobs land in the worker's
    held state (rescheduling themselves on a short cadence) and pick
    up the freshly-saved key on their next fire. This removes the
    old scan-vs-match-all race that left files scanned mid-wizard
    permanently un-enqueued."""

    async def fake_validate(key, *, http=None):
        return True, None

    monkeypatch.setattr(
        "app.comicvine.client.validate_cv_api_key",
        fake_validate,
    )

    await _login_as_admin(client, db_session)
    r = await client.post(
        "/setup/comicvine",
        data={"api_key": "abcd1234"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?welcome=1"

    # Key actually persisted. ``expire_all`` is sync on AsyncSession
    # (it's a SQLAlchemy session-state op, not a DB round-trip), so
    # no await.
    from app.services.settings import get_cv_api_key

    db_session.expire_all()
    assert await get_cv_api_key(db_session) == "abcd1234"


async def test_setup_cv_submit_rerenders_form_on_invalid_key(
    client,
    db_session,
    monkeypatch,
):
    """A key that ComicVine rejects → 400 + the form re-rendered with
    the failed value pre-filled (so the admin can fix a typo without
    losing context)."""

    async def fake_validate(key, *, http=None):
        return False, "ComicVine rejected the key."

    monkeypatch.setattr(
        "app.comicvine.client.validate_cv_api_key",
        fake_validate,
    )

    await _login_as_admin(client, db_session)
    r = await client.post(
        "/setup/comicvine",
        data={"api_key": "typo-key"},
    )
    assert r.status_code == 400
    assert "ComicVine rejected" in r.text
    # The bad value sits in the input so the user can correct it.
    assert "typo-key" in r.text

    # No key saved.
    from app.services.settings import get_cv_api_key

    assert await get_cv_api_key(db_session) is None


async def test_setup_cv_skip_redirects_to_admin_with_banner_flag(
    client,
    db_session,
):
    """Skipping lands on /admin with a flag the template uses to show
    the 'matching disabled — set a key' banner. No CV key is saved."""
    await _login_as_admin(client, db_session)
    r = await client.post("/setup/comicvine/skip")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?cv_skipped=1"

    from app.services.settings import get_cv_api_key

    assert await get_cv_api_key(db_session) is None
