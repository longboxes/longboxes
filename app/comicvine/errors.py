"""ComicVine client exceptions.

A small hierarchy so callers can catch broadly (``ComicVineError``) or
narrowly (e.g., distinguish "key not configured" from "CV is rate-limiting
us" from "the requested resource doesn't exist").
"""

from __future__ import annotations


class ComicVineError(Exception):
    """Base class for all ComicVine-related errors."""


class ComicVineKeyMissingError(ComicVineError):
    """No API key configured in app_settings. Admin must paste one in."""


class ComicVineApiError(ComicVineError):
    """Generic non-success response from ComicVine.

    Includes the HTTP status code and the CV-level status_code (CV returns a
    success/failure code inside the response body, which can be non-zero
    even when HTTP is 200).
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        cv_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.cv_status = cv_status


class ComicVineNotFoundError(ComicVineApiError):
    """The requested CV resource doesn't exist (CV status 101 or HTTP 404)."""


class ComicVineRateLimitError(ComicVineApiError):
    """We've hit (or are pacing against) CV's rate limit.

    Raised in two situations:

    * the HTTP client exhausted its short in-request retries on a real
      HTTP 420 / 429 or CV status 107, or
    * the shared ``RedisRatePacer`` decided the next request slot is far
      enough out that the caller should wait rather than block.

    ``retry_after`` is the number of seconds the caller should wait before
    trying again — a match job turns this into a delayed re-enqueue.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        cv_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, cv_status=cv_status)
        self.retry_after = retry_after


class ComicVineKeyInvalidError(ComicVineApiError):
    """ComicVine rejected the API key (CV status 100, "Invalid API Key").

    Distinct from ``ComicVineKeyMissingError`` (no key configured at all):
    this fires when a key IS configured but CV doesn't accept it — typically
    a typo on paste, a revoked key, or copy-paste truncation.
    """
