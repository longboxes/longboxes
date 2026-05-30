"""Redis-backed session store.

Per the design doc:

    Login produces a Redis-stored session with a random opaque token.
    Token sent as HttpOnly, Secure (when behind HTTPS), SameSite=Lax cookie.
    Sessions expire after 30 days of inactivity; admin can revoke any session.

Concretely:

- The session token is 32 bytes of ``secrets.token_urlsafe`` (≈ 43 chars).
- Each session lives at key ``session:<token>`` in Redis with the user's UUID
  as its value and a TTL of ``session_ttl_days`` days.
- On every authenticated request the TTL is refreshed (sliding expiration).
- We also keep a ``user_sessions:<user_id>`` Redis set holding the user's
  active tokens, so an admin (post-MVP) can revoke every session for a user
  with a single call to ``revoke_all_for_user``.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from app.config import settings

_SESSION_PREFIX = "session:"
_USER_INDEX_PREFIX = "user_sessions:"


def _ttl_seconds() -> int:
    return settings.session_ttl_days * 24 * 60 * 60


def _session_key(token: str) -> str:
    return f"{_SESSION_PREFIX}{token}"


def _user_index_key(user_id: uuid.UUID | str) -> str:
    return f"{_USER_INDEX_PREFIX}{user_id}"


@dataclass(frozen=True)
class Session:
    token: str
    user_id: uuid.UUID


async def create_session(redis: Redis, user_id: uuid.UUID) -> Session:
    """Create a new session for ``user_id`` and return the opaque token."""
    token = secrets.token_urlsafe(32)
    ttl = _ttl_seconds()
    pipe = redis.pipeline()
    pipe.set(_session_key(token), str(user_id), ex=ttl)
    pipe.sadd(_user_index_key(user_id), token)
    # The user-index set itself shouldn't outlive the longest possible session.
    # Reset its TTL to the session TTL on every login.
    pipe.expire(_user_index_key(user_id), ttl)
    await pipe.execute()
    return Session(token=token, user_id=user_id)


async def get_session(redis: Redis, token: str) -> Session | None:
    """Look up a session by token. Refreshes the TTL on hit (sliding expiry).

    Returns None if the token is missing, malformed, or expired.
    """
    if not token:
        return None
    raw = await redis.get(_session_key(token))
    if raw is None:
        return None
    try:
        user_id = uuid.UUID(raw)
    except ValueError:
        # Corrupt entry — drop it.
        await redis.delete(_session_key(token))
        return None
    # Slide the expiry forward.
    ttl = _ttl_seconds()
    await redis.expire(_session_key(token), ttl)
    await redis.expire(_user_index_key(user_id), ttl)
    return Session(token=token, user_id=user_id)


async def delete_session(redis: Redis, token: str) -> None:
    """Revoke a single session (used on logout)."""
    if not token:
        return
    raw = await redis.get(_session_key(token))
    pipe = redis.pipeline()
    pipe.delete(_session_key(token))
    if raw is not None:
        try:
            user_id = uuid.UUID(raw)
        except ValueError:
            user_id = None
        if user_id is not None:
            pipe.srem(_user_index_key(user_id), token)
    await pipe.execute()


async def revoke_all_for_user(redis: Redis, user_id: uuid.UUID) -> int:
    """Revoke every active session for a user. Returns the count revoked."""
    index_key = _user_index_key(user_id)
    tokens = await redis.smembers(index_key)
    if not tokens:
        return 0
    pipe = redis.pipeline()
    for t in tokens:
        pipe.delete(_session_key(t))
    pipe.delete(index_key)
    await pipe.execute()
    return len(tokens)
