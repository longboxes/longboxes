"""Async Redis client.

A single module-level client is created lazily on first use and re-used for
the lifetime of the process. ``redis.asyncio`` connections are themselves
multiplexed onto a connection pool, so this is the right shape for FastAPI.

The sync RQ worker (``app.worker``) creates its own sync client; that's
deliberate — RQ is sync.
"""

from __future__ import annotations

from redis.asyncio import Redis, from_url

from app.config import settings

_client: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide async Redis client."""
    global _client
    if _client is None:
        _client = from_url(settings.redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Close the connection pool. Called from the FastAPI lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
