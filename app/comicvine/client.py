"""Async ComicVine HTTP client.

What this layer does:
- Reads the API key from ``app_settings`` (refused if absent).
- Goes through the per-resource token-bucket rate limiter before each call.
- Retries with exponential backoff on HTTP 420/429 and CV-level rate-limit
  status codes; gives up after a few attempts and raises ``ComicVineRateLimitError``.
- Maps non-200 / CV-error-status responses to typed exceptions.
- Returns the parsed JSON ``results`` payload (the useful inner object).

What it does NOT do:
- Cache anything. The cache layer (``app.comicvine.cache``) wraps this and
  decides when to call.
- Persist anything. Same separation.

ComicVine response envelope:
    {
        "error": "OK",
        "limit": 1, "offset": 0, "number_of_page_results": 1,
        "number_of_total_results": 1, "status_code": 1,
        "results": { ... resource ... },
        "version": "1.0"
    }
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.comicvine.errors import (
    ComicVineApiError,
    ComicVineKeyInvalidError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)
from app.comicvine.pacer import DEFAULT_PENALTY_SECONDS
from app.comicvine.rate_limit import TokenBucketRateLimiter
from app.services.settings import get_cv_api_key

logger = logging.getLogger("longboxes.comicvine.client")

BASE_URL = "https://comicvine.gamespot.com/api"
# ComicVine asks API consumers to set a descriptive UA per their docs.
USER_AGENT = "Longboxes/0.1 (+https://github.com/longboxes/longboxes)"

# Retry policy for 420 / 429 and CV-internal rate-limit codes.
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.0  # 1, 2, 4, 8 seconds (plus jitter)

# CV status codes we treat specially. Full list:
# https://comicvine.gamespot.com/api/documentation
CV_STATUS_OK = 1
CV_STATUS_INVALID_API_KEY = 100
CV_STATUS_OBJECT_NOT_FOUND = 101
CV_STATUS_RATE_LIMIT_EXCEEDED = 107


# ---- Public client ------------------------------------------------------


class ComicVineClient:
    """Async ComicVine HTTP client. One instance per app/worker is fine."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            # ComicVine returns 301s for paths without trailing slashes (and
            # potentially for other URL drift). We always include the slash
            # below, but enabling follow_redirects keeps us robust if CV ever
            # changes endpoint shapes.
            follow_redirects=True,
        )
        self._owns_http = http is None
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter()
        self._base_url = base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---- Public typed methods ------------------------------------------

    # ---- Resource type IDs ----
    # ComicVine encodes resource type in the URL as a "4XXX-<id>" prefix.
    # Verified prefixes (against live CV or the Salesforce adapter):
    #   4000 = issue       (standard; in every ComicInfo Web URL)
    #   4005 = character   (Apex adapter)
    #   4045 = story_arc   (live smoke test: /story_arc/4045-55679/)
    #   4050 = volume      (live smoke test)
    #   4060 = team        (Apex adapter)
    # Unverified — smoke-test before relying on them:
    #   4010 = publisher
    #   4040 = person
    # If a get_* method below fails with ComicVineNotFoundError on a CV ID
    # that you've manually confirmed exists, the resource ID is the most
    # likely culprit — cross-check against the URL on comicvine.gamespot.com.

    async def get_volume(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /volume/4050-<id>/. Returns the ``results`` payload."""
        return await self._get_resource(db, "volume", f"4050-{cv_id}")

    async def get_issue(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /issue/4000-<id>/."""
        return await self._get_resource(db, "issue", f"4000-{cv_id}")

    async def get_publisher(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /publisher/4010-<id>/. **Unverified resource ID** — see note above."""
        return await self._get_resource(db, "publisher", f"4010-{cv_id}")

    async def get_person(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /person/4040-<id>/. **Unverified resource ID** — see note above."""
        return await self._get_resource(db, "person", f"4040-{cv_id}")

    async def get_character(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /character/4005-<id>/."""
        return await self._get_resource(db, "character", f"4005-{cv_id}")

    async def get_team(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /team/4060-<id>/.

        The payload includes a ``characters`` list (the team's members)
        and ``teams`` is itself a ref on the character resource — see
        ``get_team_detail`` in ``app/services/library.py``."""
        return await self._get_resource(db, "team", f"4060-{cv_id}")

    async def get_story_arc(self, db: AsyncSession, cv_id: int) -> dict:
        """GET /story_arc/4045-<id>/. Returns the ``results`` payload.

        The payload includes an ``issues`` list spanning every member issue
        across every volume — see ``get_volume_detail`` in
        ``app/services/library.py`` for how that's used to populate arc
        stripes without per-issue hydration."""
        return await self._get_resource(db, "story_arc", f"4045-{cv_id}")

    async def search_volumes(
        self,
        db: AsyncSession,
        query: str,
        *,
        limit: int = 25,
    ) -> dict:
        """GET /volumes/?filter=name:<query>. Returns the full envelope so the
        cache layer can hash the request key off the params."""
        params = {"filter": f"name:{query}", "limit": str(limit)}
        return await self._request(db, "search", "volumes/", params=params)

    async def search(
        self,
        db: AsyncSession,
        query: str,
        *,
        resources: str = "volume",
        limit: int = 25,
    ) -> dict:
        """GET /search/?query=<query>&resources=<resources>.

        ComicVine's full-text search endpoint. Distinct from
        ``search_volumes`` above, which hits ``/volumes/?filter=name:``
        — a near-literal list filter. ``/search/`` does fuzzy,
        multi-word matching, so it handles subtitled or reordered
        series names (e.g. "Avengers No More Bullying") that the
        name filter would miss.

        ``resources`` narrows the result types (``volume``, ``issue``,
        ``character`` ...); pass a comma-separated list for several.
        Returns the full envelope so the cache layer can hash the
        request key off the params.
        """
        params = {
            "query": query,
            "resources": resources,
            "limit": str(limit),
        }
        return await self._request(db, "search", "search/", params=params)

    async def list_issues_by_volume(
        self,
        db: AsyncSession,
        volume_cv_id: int,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """GET /issues/?filter=volume:<id>&offset=N&limit=M.

        Bulk-list every issue in a volume — one paginated API call replaces
        N per-issue ``get_issue`` round-trips when a fresh volume needs all
        its stub rows upgraded to fully-hydrated records. Returns the full
        envelope (``results`` is a list, plus pagination metadata) so the
        cache layer can walk pages.

        ``limit`` caps at 100 per ComicVine's defaults; pass a higher
        ``offset`` for subsequent pages.
        """
        params = {
            "filter": f"volume:{volume_cv_id}",
            "offset": str(offset),
            "limit": str(limit),
        }
        return await self._request(db, "issue", "issues/", params=params)

    # ---- Internals ------------------------------------------------------

    async def _get_resource(self, db: AsyncSession, resource_type: str, resource_id: str) -> dict:
        """Single-resource GET. Path is /{resource_type}/{4xxx-id}/.

        Trailing slash is required: CV returns 301 → trailing-slash form
        otherwise, which works but adds a round trip.
        """
        envelope = await self._request(
            db,
            resource_type,
            f"{resource_type}/{resource_id}/",
        )
        results = envelope.get("results")
        if not isinstance(results, dict) or not results:
            raise ComicVineApiError(
                f"empty results for {resource_type}/{resource_id}",
                cv_status=envelope.get("status_code"),
            )
        return results

    async def _request(
        self,
        db: AsyncSession,
        resource_type: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict:
        """Make a rate-limited, retrying GET. Returns the parsed envelope."""
        api_key = await get_cv_api_key(db)
        if not api_key:
            raise ComicVineKeyMissingError(
                "ComicVine API key is not configured. "
                "Set it in the admin UI before making CV calls."
            )

        url = f"{self._base_url}/{path.lstrip('/')}"
        request_params: dict[str, str] = {
            "api_key": api_key,
            "format": "json",
        }
        if params:
            request_params.update(params)

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            await self._rate_limiter.acquire(resource_type)
            try:
                response = await self._http.get(url, params=request_params)
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    await self._sleep_with_backoff(attempt)
                    continue
                raise ComicVineApiError(f"network error: {e}") from e

            # Rate limit at the HTTP layer.
            if response.status_code in (420, 429):
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "CV rate limit (HTTP %s) on %s; retry %d/%d",
                        response.status_code,
                        path,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    await self._sleep_with_backoff(attempt)
                    continue
                # Retries exhausted — this is a sustained rate limit, not
                # a blip. Penalise the shared gate so every other job and
                # process backs off too, and surface ``retry_after`` so
                # the caller can re-enqueue itself after the cool-down.
                cooldown = _parse_retry_after(response) or DEFAULT_PENALTY_SECONDS
                await self._rate_limiter.penalize(resource_type, cooldown)
                raise ComicVineRateLimitError(
                    f"ComicVine rate limit exceeded after {MAX_RETRIES} retries",
                    http_status=response.status_code,
                    retry_after=cooldown,
                )

            if response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "CV server error %s on %s; retry %d/%d",
                        response.status_code,
                        path,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    await self._sleep_with_backoff(attempt)
                    continue
                raise ComicVineApiError(
                    f"ComicVine returned HTTP {response.status_code}",
                    http_status=response.status_code,
                )

            if response.status_code >= 400:
                raise ComicVineApiError(
                    f"ComicVine returned HTTP {response.status_code}",
                    http_status=response.status_code,
                )

            try:
                envelope = response.json()
            except ValueError as e:
                raise ComicVineApiError(f"ComicVine returned non-JSON body: {e}") from e

            cv_status = envelope.get("status_code")
            if cv_status == CV_STATUS_OK:
                return envelope
            if cv_status == CV_STATUS_INVALID_API_KEY:
                # Don't retry: a new key won't materialize on retry.
                raise ComicVineKeyInvalidError(
                    "ComicVine rejected the API key (status 100). "
                    "Re-paste the key in the admin UI.",
                    http_status=response.status_code,
                    cv_status=cv_status,
                )
            if cv_status == CV_STATUS_OBJECT_NOT_FOUND:
                raise ComicVineNotFoundError(
                    f"ComicVine resource not found: {path}",
                    http_status=response.status_code,
                    cv_status=cv_status,
                )
            if cv_status == CV_STATUS_RATE_LIMIT_EXCEEDED:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "CV rate limit (status %d) on %s; retry %d/%d",
                        cv_status,
                        path,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    await self._sleep_with_backoff(attempt)
                    continue
                cooldown = _parse_retry_after(response) or DEFAULT_PENALTY_SECONDS
                await self._rate_limiter.penalize(resource_type, cooldown)
                raise ComicVineRateLimitError(
                    "ComicVine rate limit exceeded after retries",
                    http_status=response.status_code,
                    cv_status=cv_status,
                    retry_after=cooldown,
                )

            error_msg = envelope.get("error", "(no error message)")
            raise ComicVineApiError(
                f"ComicVine error: {error_msg}",
                http_status=response.status_code,
                cv_status=cv_status,
            )

        # Shouldn't be reachable, but make the type checker happy.
        raise ComicVineApiError("exhausted retries without a response") from last_exc

    @staticmethod
    async def _sleep_with_backoff(attempt: int) -> None:
        """Exponential backoff with jitter: 1, 2, 4, 8 sec ± 25%."""
        base = BACKOFF_BASE_SECONDS * (2**attempt)
        jitter = base * 0.25 * (2 * random.random() - 1)
        await asyncio.sleep(max(0.0, base + jitter))


async def validate_cv_api_key(
    candidate: str, *, http: httpx.AsyncClient | None = None
) -> tuple[bool, str | None]:
    """Make a single CV call against ``candidate`` and report validity.

    Returns ``(True, None)`` when CV accepts the key (HTTP 200 + CV
    ``status_code`` == 1). Returns ``(False, message)`` for an empty
    input, an invalid-key response (CV status 100), a network failure,
    or any other unexpected envelope.

    Standalone — does **not** read from the settings table, does
    **not** go through the rate pacer. The setup wizard and the admin
    "save key" form use this to confirm the key works *before*
    committing it; piping the candidate through ``ComicVineClient``
    would require swapping the stored key out and back.

    Picks the ``/types/`` endpoint — the catalogue of CV resource
    type IDs, free of per-resource pacing concerns and small enough
    to keep the validation call cheap (~few hundred bytes).
    """
    cleaned = (candidate or "").strip()
    if not cleaned:
        return False, "Paste a key to test."

    owns_client = http is None
    client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        try:
            response = await client.get(
                f"{BASE_URL}/types/",
                params={"api_key": cleaned, "format": "json"},
            )
        except httpx.HTTPError as e:
            return (
                False,
                f"Couldn't reach ComicVine to validate the key: {e}",
            )

        if response.status_code != 200:
            return (
                False,
                f"ComicVine returned HTTP {response.status_code} — "
                "the network looks fine but the request was rejected.",
            )

        try:
            envelope = response.json()
        except ValueError:
            return False, "ComicVine returned a non-JSON response."

        cv_status = envelope.get("status_code")
        if cv_status == CV_STATUS_OK:
            return True, None
        if cv_status == CV_STATUS_INVALID_API_KEY:
            return (
                False,
                "ComicVine rejected the key. Double-check the value "
                "on your ComicVine account's API page.",
            )
        return (
            False,
            f"Unexpected ComicVine status {cv_status} during validation; the key may not be valid.",
        )
    finally:
        if owns_client:
            await client.aclose()


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header into seconds; None if absent/unusable.

    Only the delta-seconds form is handled. The HTTP-date form is ignored
    — callers fall back to ``DEFAULT_PENALTY_SECONDS`` when this returns
    None, which is a fine approximation for CV's hourly window.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except (ValueError, AttributeError):
        return None
    return seconds if seconds > 0 else None
