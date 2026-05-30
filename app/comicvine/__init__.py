"""ComicVine integration: HTTP client, rate limiter, retry, cache layer.

Public API:
    ``ComicVineClient``    — low-level httpx wrapper. Rate-limited, retries
                              on 420/429. Returns parsed JSON.
    ``ComicVineCache``     — cache-aside + SWR layer over the CV tables in
                              app/models/cv.py. Routes use this, not the
                              client, except in tests/admin where we want
                              to bypass the cache.
    ``ComicVineError``     — base exception; common subclasses cover key
                              missing, rate-limit, network failure, and
                              4xx/5xx responses.
"""

from app.comicvine.cache import ComicVineCache
from app.comicvine.client import ComicVineClient
from app.comicvine.errors import (
    ComicVineApiError,
    ComicVineError,
    ComicVineKeyInvalidError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)
from app.comicvine.pacer import RedisRatePacer

__all__ = [
    "ComicVineApiError",
    "ComicVineCache",
    "ComicVineClient",
    "ComicVineError",
    "ComicVineKeyInvalidError",
    "ComicVineKeyMissingError",
    "ComicVineNotFoundError",
    "ComicVineRateLimitError",
    "RedisRatePacer",
]
