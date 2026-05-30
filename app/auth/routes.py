"""Authentication routes: /setup, /login, /logout.

GET handlers render forms; POST handlers validate, mutate state, and either
re-render the form with an error or 303-redirect to the next page.

303 (See Other) is the right redirect after a successful POST: the browser
issues a fresh GET to the target so a refresh doesn't replay the form.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import sessions
from app.auth.dependencies import (
    DbSessionDep,
    RedisDep,
    RequireSetupPendingDep,
    RequireUserDep,
)
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.config import settings
from app.models import User, UserRole
from app.templates_env import templates

logger = logging.getLogger("longboxes.auth")

router = APIRouter()


# ---- Helpers -------------------------------------------------------------


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
    )


def _safe_next(value: str | None) -> str:
    """Only allow same-site redirect targets — drop schemes, hosts, protocol-relative.

    Prevents an open-redirect via ``?next=https://evil.example/``.
    """
    if not value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not value.startswith("/"):
        return "/"
    # Block protocol-relative URLs like //evil.example/path which urlsplit
    # leaves as netloc-only when the scheme is missing.
    if value.startswith("//"):
        return "/"
    return value


async def _users_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(User))
    return result.scalar_one()


# ---- /setup --------------------------------------------------------------


@router.get("/setup")
async def setup_form(
    request: Request,
    _: RequireSetupPendingDep,
):
    return templates.TemplateResponse(request, "setup.html", {})


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    _: RequireSetupPendingDep,
    db: DbSessionDep,
    redis: RedisDep,
):
    username = username.strip()
    error: str | None = None
    if not username:
        error = "Username is required."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != password_confirm:
        error = "Passwords do not match."

    if error:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": error, "username": username},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Race-safe re-check inside the same transaction. If another request
    # bootstrapped first, fall through to /login rather than create a 2nd admin.
    if await _users_count(db) > 0:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    user = User(
        username=username,
        password_hash=hash_password(password),
        role=UserRole.ADMIN,
        last_login_at=datetime.now(tz=UTC),
    )
    db.add(user)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(user)
    logger.info("First-run admin created: %s", user.username)

    session = await sessions.create_session(redis, user.id)
    # Step 2 of the first-boot wizard: prompt for the ComicVine API
    # key. Matching is gated on that key everywhere downstream (the
    # scanner skips ``enqueue_match`` without it; ``match_file_job``
    # bails early if a stale job slips through), and the admin
    # discovery story of "find /admin → scroll to settings" is poor
    # for a first-run. Sending them straight to the key form keeps
    # the install narrative short: create user → paste key → done.
    response = RedirectResponse(url="/setup/comicvine", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, session.token)
    return response


# ---- /setup/comicvine (wizard step 2) -----------------------------------
#
# Renders the CV key form right after admin creation. Validates the key
# against ComicVine with one cheap call before saving (so a typo is
# caught immediately, not three minutes later when the matcher starts
# failing). "Skip for now" lands the admin on /admin?cv_skipped=1 with
# a persistent banner; they can paste a key later from the admin home.


@router.get("/setup/comicvine")
async def setup_cv_form(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
):
    """The CV-key wizard step. Admin-only — non-admins shouldn't be
    able to land here (they wouldn't have created the admin in the
    first place). If a key is already configured, send the user to
    /admin instead of re-rendering the wizard."""
    from app.services.settings import is_cv_configured

    if user.role != UserRole.ADMIN:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if await is_cv_configured(db):
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "setup_comicvine.html",
        {"user": user},
    )


@router.post("/setup/comicvine")
async def setup_cv_submit(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    api_key: Annotated[str, Form()] = "",
):
    """Validate, save, and (if files are already scanned) fire a
    Match-all pass so newly-registered files don't sit unmatched.

    The validation round-trip catches a typo before it stores a bad
    key; on failure we re-render with an inline error rather than
    redirect, so the pasted (and probably typo'd) value is still in
    the field for fixing."""
    from app.comicvine.client import validate_cv_api_key
    from app.services.settings import set_cv_api_key

    if user.role != UserRole.ADMIN:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    cleaned = api_key.strip()
    ok, error_msg = await validate_cv_api_key(cleaned)
    if not ok:
        return templates.TemplateResponse(
            request,
            "setup_comicvine.html",
            {"user": user, "error": error_msg, "api_key": cleaned},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await set_cv_api_key(db, cleaned)
    await db.commit()

    # Match jobs the scanner already enqueued during the key-less
    # window are sitting in the worker's held state — each one
    # reschedules itself on a short cadence (see
    # ``app/jobs/match_file.NO_KEY_RESCHEDULE_SECONDS``) and picks
    # up the freshly-saved key on its next fire. No explicit
    # match-all-unmatched pass needed here. If a previous scan
    # ran on an older build (no matching enqueues at all for those
    # files), the admin "Match all" button on /admin re-queues
    # every un-matched file.
    return RedirectResponse(
        url="/admin?welcome=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/setup/comicvine/skip")
async def setup_cv_skip(user: RequireUserDep):
    """Skip the key step. The user lands on /admin with a banner;
    matching stays disabled until they fill the key in via the admin
    settings form (which calls the same save_cv_key route).

    Admin-only; the wizard step itself is admin-only too — this is
    just symmetric and defensive."""
    if user.role != UserRole.ADMIN:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/admin?cv_skipped=1", status_code=status.HTTP_303_SEE_OTHER)


# ---- /login --------------------------------------------------------------


@router.get("/login")
async def login_form(request: Request, next: str | None = None):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next)},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: DbSessionDep,
    redis: RedisDep,
    next: Annotated[str | None, Form()] = None,
):
    username = username.strip()
    target = _safe_next(next)

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    # Always perform a verify call — even when the user doesn't exist —
    # to keep the timing side-channel between "no such user" and "wrong
    # password" small. Verifying against a known-bad hash is the standard
    # mitigation; argon2 is slow enough that the constant-time argument is
    # less critical than usual but it's still cheap to do right.
    if user is None:
        verify_password(password, _DUMMY_HASH)
        ok = False
    else:
        ok = verify_password(password, user.password_hash)

    if not ok or user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Invalid username or password.",
                "username": username,
                "next": target,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Opportunistic re-hash if defaults have moved on since this hash was created.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = datetime.now(tz=UTC)
    await db.commit()

    session = await sessions.create_session(redis, user.id)
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, session.token)
    return response


# ---- /logout -------------------------------------------------------------


@router.post("/logout")
async def logout(
    request: Request,
    _: RequireUserDep,
    redis: RedisDep,
):
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await sessions.delete_session(redis, token)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(response)
    return response


# A pre-computed argon2id hash of the empty string. Verifying against this on
# the "no such user" path keeps the timing similar to the "user exists, wrong
# password" path. Generated once at import time rather than in the hot path.
_DUMMY_HASH = hash_password("")
