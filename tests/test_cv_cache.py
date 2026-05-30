"""Tests for the ComicVine cache layer (cache-aside + SWR + stub-issue upsert)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.comicvine import ComicVineCache, ComicVineClient
from app.comicvine.client import BASE_URL
from app.comicvine.rate_limit import TokenBucketRateLimiter
from app.models import CvIssue, CvPublisher, CvSearchCache, CvVolume
from app.services.settings import set_cv_api_key

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.comicvine.client.ComicVineClient._sleep_with_backoff",
        staticmethod(_instant),
    )


class _RevalRecorder:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def __call__(self, entity: str, cv_id: int) -> None:
        self.calls.append((entity, cv_id))


def _ok(results: dict | list) -> dict:
    return {"error": "OK", "status_code": 1, "results": results, "version": "1.0"}


def _fast_client() -> ComicVineClient:
    return ComicVineClient(
        rate_limiter=TokenBucketRateLimiter(capacity=100, refill_rate_per_second=100.0)
    )


async def _seed_key(db):
    await set_cv_api_key(db, "test-key")
    await db.commit()


def _volume_payload(cv_id: int = 18166, issue_count: int = 2) -> dict:
    return {
        "id": cv_id,
        "name": "Saga",
        "start_year": "2012",
        "count_of_issues": issue_count,
        "publisher": {"id": 31, "name": "Image"},
        "issues": [
            {
                "id": 100 + i,
                "issue_number": str(i + 1),
                "name": f"Chapter {i + 1}",
                "cover_date": "2012-03-14",
            }
            for i in range(issue_count)
        ],
    }


# ---- Volume: miss / fresh / stale ---------------------------------------


@respx.mock
async def test_volume_miss_fetches_persists_and_upserts_stub_issues(db_session):
    await _seed_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(200, json=_ok(_volume_payload(issue_count=3)))
    )
    client = _fast_client()
    rec = _RevalRecorder()
    cache = ComicVineCache(client, enqueue_revalidate=rec)
    try:
        vol = await cache.get_volume(db_session, 18166)
    finally:
        await client.aclose()

    assert vol.cv_id == 18166
    assert vol.name == "Saga"
    assert vol.year == 2012
    assert vol.count_of_issues == 3
    assert vol.fetched_at is not None

    # Stub publisher row inserted to keep the FK valid.
    pub = await db_session.get(CvPublisher, 31)
    assert pub is not None
    assert pub.raw_payload.get("_stub") is True

    # Stub issue rows: one per nested issue. ``fetched_at=NULL`` is the
    # "not yet fully hydrated" marker. ``raw_payload`` holds the stub dict
    # from the volume response (id/issue_number/name/cover_date + image),
    # so table thumbnails can render without a per-issue fetch.
    stubs = (await db_session.execute(select(CvIssue))).scalars().all()
    assert len(stubs) == 3
    for s in stubs:
        assert s.volume_cv_id == 18166
        assert s.fetched_at is None
        assert isinstance(s.raw_payload, dict)
        assert s.issue_number in {"1", "2", "3"}

    # _upsert_volume no longer fires a bulk volume_issues hydration
    # on first-touch.
    # Stage 3 of the matcher fetches up to 5 candidate volumes per
    # file and rejects four; under the old rule each one spawned a
    # hydration job for a volume the user would never see. The
    # enqueue now happens in the matcher pipeline on a winning
    # match (see test_matcher.test_stage1_short_circuit_enqueues_volume_issues
    # and test_stage2_4_winner_enqueues_volume_issues).
    assert rec.calls == []


@respx.mock
async def test_volume_fresh_hit_does_not_call_cv(db_session):
    await _seed_key(db_session)
    route = respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(200, json=_ok(_volume_payload()))
    )
    client = _fast_client()
    rec = _RevalRecorder()
    cache = ComicVineCache(client, enqueue_revalidate=rec)
    try:
        await cache.get_volume(db_session, 18166)
        assert route.call_count == 1
        await cache.get_volume(db_session, 18166)
        # Second read served from cache, no extra HTTP call.
        assert route.call_count == 1
        # Neither read enqueues volume_issues — that's the
        # matcher's responsibility now (Lever 2). The cache layer
        # is purely a data-access concern.
        assert rec.calls == []
    finally:
        await client.aclose()


@respx.mock
async def test_volume_stale_hit_serves_cached_and_enqueues_revalidation(db_session):
    await _seed_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(200, json=_ok(_volume_payload()))
    )
    client = _fast_client()
    rec = _RevalRecorder()
    cache = ComicVineCache(client, enqueue_revalidate=rec)
    try:
        await cache.get_volume(db_session, 18166)
        # Forcibly age the row past its TTL.
        vol = await db_session.get(CvVolume, 18166)
        vol.fetched_at = datetime.now(tz=UTC) - timedelta(days=30)
        await db_session.commit()

        # Second read should serve cached and enqueue an SWR
        # ``volume`` revalidate. _upsert_volume no longer enqueues
        # ``volume_issues`` (that moved to the matcher), so the SWR
        # call is the only one we expect.
        await cache.get_volume(db_session, 18166)
        assert rec.calls == [("volume", 18166)]
    finally:
        await client.aclose()


@respx.mock
async def test_force_refresh_bypasses_cache(db_session):
    await _seed_key(db_session)
    route = respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(200, json=_ok(_volume_payload()))
    )
    client = _fast_client()
    cache = ComicVineCache(client)
    try:
        await cache.get_volume(db_session, 18166)
        await cache.get_volume(db_session, 18166, force_refresh=True)
    finally:
        await client.aclose()
    assert route.call_count == 2


# ---- Volume re-fetch preserves hydrated issue payloads ------------------


@respx.mock
async def test_volume_refetch_does_not_clobber_hydrated_issue(db_session):
    """If an issue was hydrated via /issue/X/ and we re-fetch the volume,
    the stub upsert must not wipe the existing full payload."""
    await _seed_key(db_session)
    respx.get(f"{BASE_URL}/volume/4050-1/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 1,
                    "name": "V",
                    "start_year": "2020",
                    "count_of_issues": 1,
                    "publisher": {"id": 99, "name": "P"},
                    "issues": [
                        {
                            "id": 555,
                            "issue_number": "1",
                            "name": "stub-name",
                            "cover_date": "2020-01-01",
                        }
                    ],
                }
            ),
        )
    )
    client = _fast_client()
    cache = ComicVineCache(client)
    try:
        await cache.get_volume(db_session, 1)
        # Pretend the issue was hydrated by an /issue/555/ fetch.
        issue = await db_session.get(CvIssue, 555)
        issue.raw_payload = {"id": 555, "hydrated": True, "description": "real"}
        issue.fetched_at = datetime.now(tz=UTC)
        issue.name = "hydrated-name"
        await db_session.commit()

        # Force-refresh the volume — the stub upsert must NOT downgrade the issue.
        await cache.get_volume(db_session, 1, force_refresh=True)
    finally:
        await client.aclose()

    issue = await db_session.get(CvIssue, 555)
    assert issue.raw_payload == {"id": 555, "hydrated": True, "description": "real"}
    assert issue.name == "hydrated-name"
    assert issue.fetched_at is not None


# ---- Issue: stubs count as misses ---------------------------------------


@respx.mock
async def test_issue_stub_is_treated_as_miss(db_session):
    await _seed_key(db_session)
    # Seed a stub directly (as if a volume fetch had placed it).
    db_session.add(
        CvIssue(
            cv_id=42,
            volume_cv_id=None,
            issue_number="1",
            cover_date=None,
            name="stub",
            raw_payload=None,
            fetched_at=None,
        )
    )
    await db_session.commit()

    respx.get(f"{BASE_URL}/issue/4000-42/").mock(
        return_value=httpx.Response(
            200,
            json=_ok({"id": 42, "name": "Real", "issue_number": "1", "volume": {"id": 99}}),
        )
    )
    client = _fast_client()
    cache = ComicVineCache(client)
    try:
        issue = await cache.get_issue(db_session, 42)
    finally:
        await client.aclose()
    assert issue.fetched_at is not None
    assert issue.raw_payload["name"] == "Real"


# ---- Bulk volume-issue hydration ----------------------------------------


@respx.mock
async def test_bulk_hydrate_stubs_an_uncached_cross_volume(db_session):
    """A bulk ``/issues/`` page can carry an issue whose own ``volume.id``
    is a volume we haven't cached. ``hydrate_volume_issues`` must stub that
    volume first so the ``cv_issues`` insert doesn't trip
    ``cv_issues_volume_cv_id_fkey``."""
    await _seed_key(db_session)

    # Volume 1 — the volume being hydrated — is already cached in full.
    db_session.add(
        CvVolume(
            cv_id=1,
            name="Primary",
            year=2020,
            publisher_cv_id=None,
            count_of_issues=2,
            raw_payload={"id": 1, "name": "Primary"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    envelope = {
        "error": "OK",
        "status_code": 1,
        "version": "1.0",
        "number_of_page_results": 2,
        "number_of_total_results": 2,
        "results": [
            {
                "id": 100,
                "issue_number": "1",
                "name": "A",
                "cover_date": "2020-01-01",
                "volume": {"id": 1, "name": "Primary"},
            },
            # This issue's own volume.id is 2 — a volume NOT in cv_volumes.
            # Pre-fix, the bulk insert FK-violated here.
            {
                "id": 200,
                "issue_number": "1",
                "name": "B",
                "cover_date": "2021-01-01",
                "volume": {"id": 2, "name": "Merged"},
            },
        ],
    }
    respx.get(f"{BASE_URL}/issues/").mock(return_value=httpx.Response(200, json=envelope))

    client = _fast_client()
    cache = ComicVineCache(client)
    try:
        upserted = await cache.hydrate_volume_issues(db_session, 1)
    finally:
        await client.aclose()

    assert upserted == 2
    # Both issues persisted, each pointing at its own volume.
    i1 = await db_session.get(CvIssue, 100)
    i2 = await db_session.get(CvIssue, 200)
    assert i1 is not None and i1.volume_cv_id == 1
    assert i2 is not None and i2.volume_cv_id == 2

    # Volume 2 was auto-stubbed to keep the FK valid; volume 1 is untouched
    # (the stub upsert is on_conflict_do_nothing, so it never downgrades a
    # cached full row).
    v2 = await db_session.get(CvVolume, 2)
    assert v2 is not None
    assert v2.raw_payload.get("_stub") is True
    v1 = await db_session.get(CvVolume, 1)
    assert v1.raw_payload.get("_stub") is None


# ---- Search caching -----------------------------------------------------


@respx.mock
async def test_search_volumes_cached_after_first_call(db_session):
    await _seed_key(db_session)
    route = respx.get(f"{BASE_URL}/volumes/").mock(
        return_value=httpx.Response(200, json=_ok([{"id": 1, "name": "X"}]))
    )
    client = _fast_client()
    cache = ComicVineCache(client)
    try:
        first = await cache.search_volumes(db_session, "x", limit=5)
        second = await cache.search_volumes(db_session, "x", limit=5)
    finally:
        await client.aclose()
    assert route.call_count == 1
    assert first == second
    # The search cache row exists.
    rows = (await db_session.execute(select(CvSearchCache))).scalars().all()
    assert len(rows) == 1
