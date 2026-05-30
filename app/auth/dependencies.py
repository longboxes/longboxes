"""FastAPI dependencies for authentication and role gating.

Public dependencies (declared as ``Annotated`` type aliases for the
preferred FastAPI 0.100+ style):

- ``CurrentUserDep``         — Optional[User]; the logged-in user or None.
- ``RequireUserDep``         — User; redirects to /login if anonymous.
- ``RequireAdminDep``        — User; 403 if the user isn't an admin.
- ``RequireSetupPendingDep`` — used by /setup; redirects to /login if any
                               user already exists.
- ``RequireSetupCompleteDep`` — used by every other route; redirects to
                                 /setup on a fresh install with no users.

Redirects use a 303 (See Other) so that POSTs land on a GET. We use a
custom ``RedirectSignal`` exception (caught by an exception handler in
``app.main``) instead of returning a ``RedirectResponse``, because
dependencies that *raise* must use exceptions — returned responses
short-circuit only when the dependency function is the route handler
itself.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import sessions
from app.config import settings
from app.db import SessionLocal
from app.models import User, UserRole
from app.redis_client import get_redis

# ---- Plumbing ------------------------------------------------------------


async def get_db() -> AsyncIterator[AsyncSession]:
    """Per-request DB session. Mirrors ``app.db.get_session`` but kept here
    to avoid a circular import once auth grows more dependents."""
    async with SessionLocal() as session:
        yield session


def get_redis_dep() -> Redis:
    return get_redis()


# Plumbing aliases — used internally by the auth dependencies below.
DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis_dep)]


# ---- Helpers -------------------------------------------------------------


class RedirectSignal(Exception):
    """Raised by dependencies to short-circuit a request with a redirect.

    FastAPI catches Response subclasses RETURNED from dependencies, but it
    does NOT catch Response objects thrown via raise. The exception handler
    registered in ``app.main`` converts this exception into a 303 response.
    """

    def __init__(self, location: str) -> None:
        self.location = location


async def _users_exist(db: AsyncSession) -> bool:
    result = await db.execute(select(func.count()).select_from(User))
    count = result.scalar_one()
    return count > 0


async def _load_user_from_request(
    request: Request,
    db: AsyncSession,
    redis: Redis,
) -> User | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    session = await sessions.get_session(redis, token)
    if session is None:
        return None
    result = await db.execute(select(User).where(User.id == session.user_id))
    return result.scalar_one_or_none()


# ---- Public dependency callables ----------------------------------------


async def current_user(
    request: Request,
    db: DbSessionDep,
    redis: RedisDep,
) -> User | None:
    """Best-effort: return the logged-in user, or None if anonymous."""
    return await _load_user_from_request(request, db, redis)


async def require_user(
    request: Request,
    db: DbSessionDep,
    redis: RedisDep,
) -> User:
    """Require an authenticated user. Redirects to /login (or /setup on a
    fresh install) if not authenticated."""
    user = await _load_user_from_request(request, db, redis)
    if user is not None:
        return user
    target = "/login" if await _users_exist(db) else "/setup"
    raise RedirectSignal(target)


async def require_admin(
    user: Annotated[User, Depends(require_user)],
) -> User:
    """Require an authenticated *admin*. 403 for non-admins."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return user


async def require_setup_pending(db: DbSessionDep) -> None:
    """Used by /setup: only allow when no users exist yet.

    If the system is already bootstrapped, send the user to /login.
    """
    if await _users_exist(db):
        raise RedirectSignal("/login")


async def require_setup_complete(db: DbSessionDep) -> None:
    """Used by routes that should not be reachable on a fresh install.

    If there are no users yet, send the request to /setup.
    """
    if not await _users_exist(db):
        raise RedirectSignal("/setup")


# ---- Public Annotated aliases for routes --------------------------------
# Routes import these and write `user: RequireUserDep` instead of
# `user: User = Depends(require_user)`. This is the recommended FastAPI
# 0.100+ style and also sidesteps the B008 lint that flags Depends() in
# default arguments.

CurrentUserDep = Annotated[User | None, Depends(current_user)]
RequireUserDep = Annotated[User, Depends(require_user)]
RequireAdminDep = Annotated[User, Depends(require_admin)]
RequireSetupPendingDep = Annotated[None, Depends(require_setup_pending)]
RequireSetupCompleteDep = Annotated[None, Depends(require_setup_complete)]
