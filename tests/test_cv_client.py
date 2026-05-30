"""Tests for the ComicVine HTTP client.

Uses respx for HTTP mocking. Sleeps are monkeypatched away so retry tests
don't burn wall time.
"""

import httpx
import pytest
import respx

from app.comicvine import ComicVineClient
from app.comicvine.client import BASE_URL
from app.comicvine.errors import (
    ComicVineApiError,
    ComicVineKeyInvalidError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)
from app.comicvine.pacer import DEFAULT_PENALTY_SECONDS
from app.comicvine.rate_limit import TokenBucketRateLimiter
from app.services.settings import set_cv_api_key

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Replace the client's backoff sleep with a no-op so retry tests are fast."""

    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.comicvine.client.ComicVineClient._sleep_with_backoff",
        staticmethod(_instant),
    )


def _ok_envelope(results: dict | list) -> dict:
    return {
        "error": "OK",
        "limit": 1,
        "offset": 0,
        "status_code": 1,
        "results": results,
        "version": "1.0",
    }


async def _set_key(db_session):
    await set_cv_api_key(db_session, "test-api-key")
    await db_session.commit()


def _fast_client() -> ComicVineClient:
    """Client with a generous rate limiter so tests don't pause."""
    return ComicVineClient(
        rate_limiter=TokenBucketRateLimiter(capacity=100, refill_rate_per_second=100.0)
    )


# ---- Happy path ---------------------------------------------------------


@respx.mock
async def test_get_volume_returns_parsed_results(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-12345/").mock(
        return_value=httpx.Response(
            200,
            json=_ok_envelope({"id": 12345, "name": "Saga", "start_year": "2012"}),
        )
    )
    client = _fast_client()
    try:
        result = await client.get_volume(db_session, 12345)
    finally:
        await client.aclose()
    assert result["name"] == "Saga"
    assert result["id"] == 12345


@respx.mock
async def test_get_issue_returns_parsed_results(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/issue/4000-99/").mock(
        return_value=httpx.Response(200, json=_ok_envelope({"id": 99, "name": "issue title"}))
    )
    client = _fast_client()
    try:
        result = await client.get_issue(db_session, 99)
    finally:
        await client.aclose()
    assert result["id"] == 99


# ---- Error paths --------------------------------------------------------


async def test_missing_api_key_raises(db_session):
    # No key set.
    client = _fast_client()
    try:
        with pytest.raises(ComicVineKeyMissingError):
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()


@respx.mock
async def test_invalid_key_via_cv_status_100(db_session):
    await _set_key(db_session)
    route = respx.get(f"{BASE_URL}/volume/4050-1/").mock(
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
    client = _fast_client()
    try:
        with pytest.raises(ComicVineKeyInvalidError):
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()
    # No retries on an invalid key — retrying won't make CV change its mind.
    assert route.call_count == 1


@respx.mock
async def test_not_found_via_cv_status_101(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-99999/").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": "Object Not Found",
                "status_code": 101,
                "results": [],
                "version": "1.0",
            },
        )
    )
    client = _fast_client()
    try:
        with pytest.raises(ComicVineNotFoundError):
            await client.get_volume(db_session, 99999)
    finally:
        await client.aclose()


@respx.mock
async def test_http_429_retries_then_succeeds(db_session):
    await _set_key(db_session)
    route = respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=_ok_envelope({"id": 1, "name": "v"})),
        ]
    )
    client = _fast_client()
    try:
        result = await client.get_volume(db_session, 1)
    finally:
        await client.aclose()
    assert result["id"] == 1
    assert route.call_count == 3


@respx.mock
async def test_429_exhausts_retries(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )
    client = _fast_client()
    try:
        with pytest.raises(ComicVineRateLimitError) as exc:
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()
    # No Retry-After header → retry_after falls back to the default cooldown
    # so the caller still has a sane delay to re-enqueue against.
    assert exc.value.retry_after == DEFAULT_PENALTY_SECONDS


@respx.mock
async def test_429_retry_after_header_is_surfaced(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "777"},
            json={"error": "rate limited"},
        )
    )
    client = _fast_client()
    try:
        with pytest.raises(ComicVineRateLimitError) as exc:
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()
    assert exc.value.retry_after == 777.0


@respx.mock
async def test_429_exhaustion_penalizes_the_limiter(db_session):
    """On a sustained 429 the client tells its rate limiter to cool the
    resource's gate down — that's how every other job/process backs off."""
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "654"},
            json={"error": "rate limited"},
        )
    )

    class _SpyLimiter:
        def __init__(self):
            self.penalized: list[tuple[str, float]] = []

        async def acquire(self, resource):
            return None

        async def penalize(self, resource, seconds):
            self.penalized.append((resource, seconds))

    spy = _SpyLimiter()
    client = ComicVineClient(rate_limiter=spy)
    try:
        with pytest.raises(ComicVineRateLimitError):
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()
    assert spy.penalized == [("volume", 654.0)]


@respx.mock
async def test_cv_status_107_rate_limit(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": "Filter Error",
                "status_code": 107,
                "results": [],
                "version": "1.0",
            },
        )
    )
    client = _fast_client()
    try:
        with pytest.raises(ComicVineRateLimitError):
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()


@respx.mock
async def test_500_retries_then_raises(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(return_value=httpx.Response(500, text="oops"))
    client = _fast_client()
    try:
        with pytest.raises(ComicVineApiError):
            await client.get_volume(db_session, 1)
    finally:
        await client.aclose()


# ---- Search -------------------------------------------------------------


@respx.mock
async def test_search_volumes_returns_envelope(db_session):
    await _set_key(db_session)
    respx.get(f"{BASE_URL}/volumes/").mock(
        return_value=httpx.Response(
            200,
            json=_ok_envelope([{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]),
        )
    )
    client = _fast_client()
    try:
        envelope = await client.search_volumes(db_session, "saga")
    finally:
        await client.aclose()
    assert envelope["status_code"] == 1
    assert len(envelope["results"]) == 2
