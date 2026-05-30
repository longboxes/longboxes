"""End-to-end matcher pipeline tests.

Each test wires up a real ``File`` + ``FileLocation`` + a real CBZ on disk
(via the synthetic builder), seeds the CV cache as needed, and runs
``match_file`` against a respx-mocked ComicVine. Asserts on the resulting
``file_matches`` row + the returned ``MatchResult``.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.comicvine import ComicVineCache, ComicVineClient, ComicVineRateLimitError
from app.comicvine.client import BASE_URL
from app.comicvine.rate_limit import TokenBucketRateLimiter
from app.matcher import match_file
from app.models import (
    ComicInfoStatus,
    CvIssue,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    MatchSource,
    MatchStatus,
)
from app.services.settings import set_cv_api_key
from tests.fixtures import build_cbz, build_comicinfo_full, build_comicinfo_partial

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.comicvine.client.ComicVineClient._sleep_with_backoff",
        staticmethod(_instant),
    )


def _ok(results):
    return {"error": "OK", "status_code": 1, "results": results, "version": "1.0"}


def _fast_cache():
    client = ComicVineClient(
        rate_limiter=TokenBucketRateLimiter(capacity=100, refill_rate_per_second=100.0)
    )
    return client, ComicVineCache(client)


async def _seed_key(db):
    await set_cv_api_key(db, "test-key")
    await db.commit()


async def _make_file_with_location(
    db,
    tmp_path: Path,
    *,
    filename: str = "Saga (2012) #001.cbz",
    comicinfo: str | None = None,
    comicinfo_status: ComicInfoStatus = ComicInfoStatus.NONE,
):
    """Build a CBZ on disk + the matching File + FileLocation rows."""
    cbz_path = tmp_path / filename
    build_cbz(cbz_path, page_count=3, comicinfo=comicinfo)
    file_row = File(
        sha256="a" * 64,
        size_bytes=cbz_path.stat().st_size,
        archive_format="cbz",
        page_count=3,
        comicinfo_status=comicinfo_status,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db.add(file_row)
    await db.flush()
    db.add(
        FileLocation(
            file_id=file_row.id,
            path=str(cbz_path),
            mtime=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
        )
    )
    await db.commit()
    return file_row


# ---- Stage 1: ComicInfo CV ID fast path --------------------------------


@respx.mock
async def test_stage1_full_with_cvid_auto_matches(db_session, tmp_path: Path):
    await _seed_key(db_session)
    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=345678)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
    )
    respx.get(f"{BASE_URL}/issue/4000-345678/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 345678,
                    "name": "ch1",
                    "issue_number": "1",
                    "cover_date": "2012-03-14",
                    "volume": {"id": 18166, "name": "Saga"},
                }
            ),
        )
    )
    # Stage 1 also eagerly hydrates the parent volume on a successful match
    # (low-traffic, high-value — see pipeline.py).
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [{"id": 345678, "issue_number": "1", "name": "ch1"}],
                }
            ),
        )
    )

    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    assert result.status is MatchStatus.AUTO
    assert result.source is MatchSource.COMICINFO_CVID
    assert result.issue_cv_id == 345678
    assert result.confidence == 1.0

    row = await db_session.get(FileMatch, file_row.id)
    assert row is not None
    assert row.status == MatchStatus.AUTO
    assert row.issue_cv_id == 345678

    # Confirm the volume is no longer a stub.
    from app.models import CvVolume

    volume = await db_session.get(CvVolume, 18166)
    assert volume is not None
    assert volume.count_of_issues == 60
    assert not (isinstance(volume.raw_payload, dict) and volume.raw_payload.get("_stub"))


@respx.mock
async def test_stage1_404_falls_through_to_stage2(db_session, tmp_path: Path):
    """ComicInfo says CV ID 99999 but CV returns 404 → fall through to filename."""
    await _seed_key(db_session)
    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=99999)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Saga (2012) #001.cbz",
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
    )
    # CV says the ID doesn't exist.
    respx.get(f"{BASE_URL}/issue/4000-99999/").mock(
        return_value=httpx.Response(
            200,
            json={"status_code": 101, "error": "Not Found", "results": [], "version": "1.0"},
        )
    )
    # Filename fallback: search returns Saga volume; volume fetch returns
    # its issue list; #1 matches and series similarity is perfect.
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200,
            json=_ok([{"id": 18166, "name": "Saga", "start_year": "2012"}]),
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [
                        {"id": 100, "issue_number": "1", "name": "ch1"},
                        {"id": 101, "issue_number": "2", "name": "ch2"},
                    ],
                }
            ),
        )
    )

    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    assert result.status is MatchStatus.AUTO  # fallback succeeded
    assert result.source is MatchSource.FILENAME
    assert result.issue_cv_id == 100  # matched #1


# ---- Lever 1: Stage 1 short-circuit (hyperspeed) -----------------------


class _RevalRecorder:
    """Captures ``(entity_type, cv_id)`` enqueue calls so tests can
    pin which jobs the matcher / cache spawn. Same shape as the
    recorder in tests/test_cv_cache.py — the cache's
    ``enqueue_revalidate`` callback is a plain ``Callable[[str, int], None]``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, entity: str, cv_id: int) -> None:
        self.calls.append((entity, cv_id))


async def _seed_cv_issue_with_volume(
    db,
    *,
    cv_id: int,
    volume_cv_id: int,
    number: str = "1",
    bulk_hydrated: bool = False,
) -> None:
    """Pre-populate a hydrated CvVolume + CvIssue so Stage 1 can take
    the short-circuit (Lever 1). The matcher only consults
    ``CvIssue.volume_cv_id`` when deciding whether to short-circuit;
    the volume row is here so the eager volume hydration also
    short-circuits as a cache hit.

    ``bulk_hydrated`` flips the ``_bulk_hydrated: True`` marker that
    ``hydrate_volume_issues`` writes on every row it bulk-fills.
    ``_notify_winner`` skips the ``volume_issues`` enqueue when any
    cv_issues row for the volume carries that marker, so tests that
    care about the enqueue path leave it ``False`` (default) and
    tests that exercise the dedupe path turn it ``True``."""
    raw_payload: dict = {"id": cv_id}
    if bulk_hydrated:
        raw_payload["_bulk_hydrated"] = True
    db.add(
        CvVolume(
            cv_id=volume_cv_id,
            name="Saga",
            year=2012,
            publisher_cv_id=None,
            count_of_issues=60,
            raw_payload={"id": volume_cv_id, "name": "Saga"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db.flush()
    db.add(
        CvIssue(
            cv_id=cv_id,
            volume_cv_id=volume_cv_id,
            issue_number=number,
            name="ch1",
            raw_payload=raw_payload,
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db.commit()


@respx.mock
async def test_stage1_short_circuit_skips_cv_when_issue_cached(db_session, tmp_path: Path):
    """Lever 1 — the headline hyperspeed win. When the ``cv_issues``
    row already exists with a populated ``volume_cv_id``, Stage 1
    must NOT hit ``/issue/<id>/`` on the issue gate (the slow
    ~180/hr resource). The match still lands AUTO; the volume_cv_id
    on the MatchResult is correct because the CvIssue row carries
    it.

    Strategy: pre-seed CvIssue + CvVolume rows, register NO respx
    routes for any CV endpoint, and let respx's default behaviour
    (``passthrough=False`` by decorator) raise on any unmocked
    request. A pass here proves the matcher made zero HTTP calls.
    """
    await _seed_key(db_session)
    await _seed_cv_issue_with_volume(db_session, cv_id=345678, volume_cv_id=18166)

    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=345678)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
    )

    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    assert result.status is MatchStatus.AUTO
    assert result.source is MatchSource.COMICINFO_CVID
    assert result.issue_cv_id == 345678
    # ``calls`` on respx's router lists every recorded request; with
    # no routes registered, any HTTP call would have raised before
    # reaching this assert, so this is belt and braces.
    assert len(respx.calls) == 0


@respx.mock
async def test_stage1_winner_enqueues_volume_issues(db_session, tmp_path: Path):
    """Lever 2 — the matcher fires the bulk ``volume_issues``
    hydration for the winning volume after a successful Stage 1
    match. The cache layer's ``_upsert_volume`` no longer enqueues
    on first-touch; the enqueue lives next to the *winner* so a
    Stage 3 evaluation that fetched 5 candidates doesn't spawn 5
    hydration jobs.

    Stage 1 takes the DB short-circuit (the CvIssue row exists), so
    the only enqueue we expect is ``volume_issues`` for the winning
    volume. No ``volume`` SWR-revalidate either, because the
    pre-seeded row is fresh.
    """
    await _seed_key(db_session)
    await _seed_cv_issue_with_volume(db_session, cv_id=345678, volume_cv_id=18166)

    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=345678)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
    )

    client = ComicVineClient(
        rate_limiter=TokenBucketRateLimiter(capacity=100, refill_rate_per_second=100.0)
    )
    rec = _RevalRecorder()
    cache = ComicVineCache(client, enqueue_revalidate=rec)
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    assert result.status is MatchStatus.AUTO
    assert rec.calls == [("volume_issues", 18166)]


@respx.mock
async def test_stage1_winner_skips_volume_issues_when_already_bulk_hydrated(
    db_session, tmp_path: Path
):
    """Throughput bug fix: each successful match used to re-enqueue
    ``volume_issues`` for the winner's volume even when the bulk
    job had already run, burning one bulk-gate token per file in
    the volume. The first observed run hit ~156 files/hr — exactly
    the bulk gate's ~180/hr cap — because every match in a 10-file
    series triggered a fresh bulk re-hydration.

    The fix: ``_notify_winner`` checks for any cv_issues row under
    the winning volume that already carries the ``_bulk_hydrated:
    True`` marker. If found, it skips the enqueue entirely. This
    test pre-seeds an already-bulk-hydrated row, runs the match,
    and asserts the recorder saw nothing — the matcher correctly
    recognises that the bulk has already done its work.
    """
    await _seed_key(db_session)
    await _seed_cv_issue_with_volume(
        db_session, cv_id=345678, volume_cv_id=18166, bulk_hydrated=True
    )

    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=345678)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
    )

    client = ComicVineClient(
        rate_limiter=TokenBucketRateLimiter(capacity=100, refill_rate_per_second=100.0)
    )
    rec = _RevalRecorder()
    cache = ComicVineCache(client, enqueue_revalidate=rec)
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    assert result.status is MatchStatus.AUTO
    # The volume is already bulk-hydrated — no enqueue, full stop.
    assert rec.calls == []


@respx.mock
async def test_pending_match_does_not_enqueue_volume_issues(db_session, tmp_path: Path):
    """A PENDING result has no confirmed winner, so the matcher
    must NOT enqueue ``volume_issues`` — that would defeat Lever 2
    by spawning hydration for a volume the human reviewer hasn't
    confirmed. The Confirm Volume / file_confirm paths own their
    own enqueue when a human picks a winner.
    """
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        # Filename is a mush of words that won't match Saga cleanly;
        # the matcher should drop into PENDING territory rather than
        # AUTO. We don't actually need to verify PENDING exactly here;
        # we just need a non-AUTO result.
        filename="Unrecognisable Title 003.cbz",
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    # CV returns no search results — Stage 2-4 lands in UNMATCHED.
    respx.get(f"{BASE_URL}/search/").mock(return_value=httpx.Response(200, json=_ok([])))

    client = ComicVineClient(
        rate_limiter=TokenBucketRateLimiter(capacity=100, refill_rate_per_second=100.0)
    )
    rec = _RevalRecorder()
    cache = ComicVineCache(client, enqueue_revalidate=rec)
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    assert result.status is not MatchStatus.AUTO
    # No volume was confirmed, so no volume_issues enqueue.
    volume_issue_calls = [c for c in rec.calls if c[0] == "volume_issues"]
    assert volume_issue_calls == []


# ---- Stages 2 + 3 + 4 --------------------------------------------------


@respx.mock
async def test_stage2_no_comicinfo_filename_match(db_session, tmp_path: Path):
    """No ComicInfo at all — pure filename parsing."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Saga (2012) #001.cbz",
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200,
            json=_ok([{"id": 18166, "name": "Saga", "start_year": "2012"}]),
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [{"id": 100, "issue_number": "1", "name": "ch1"}],
                }
            ),
        )
    )
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    assert result.status is MatchStatus.AUTO
    assert result.source is MatchSource.FILENAME
    assert result.issue_cv_id == 100
    assert result.confidence >= 0.85


@respx.mock
async def test_stage3_no_search_results_unmatched(db_session, tmp_path: Path):
    """Series that returns zero search results → unmatched."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Asdfqwerty (2099) #001.cbz",
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    respx.get(f"{BASE_URL}/search/").mock(return_value=httpx.Response(200, json=_ok([])))
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    assert result.status is MatchStatus.UNMATCHED


@respx.mock
async def test_stage3_no_matching_issue_number_unmatched(db_session, tmp_path: Path):
    """Volume found but no issue with that number → gate kicks in."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Saga (2012) #999.cbz",
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200,
            json=_ok([{"id": 18166, "name": "Saga", "start_year": "2012"}]),
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [{"id": 100, "issue_number": "1", "name": "ch1"}],
                }
            ),
        )
    )
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    assert result.status is MatchStatus.UNMATCHED


@respx.mock
async def test_pending_when_year_is_off(db_session, tmp_path: Path):
    """Filename year diverges enough from CV year to drop confidence into the
    pending band (0.50 < conf < 0.85). Series matches perfectly so we land in
    the pending range rather than unmatched."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Saga (1990) #001.cbz",  # filename says 1990
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200,
            json=_ok([{"id": 18166, "name": "Saga", "start_year": "2012"}]),
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",  # CV says 2012
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [{"id": 100, "issue_number": "1", "name": "ch1"}],
                }
            ),
        )
    )
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    # 22-year gap → year_score = 0, but series_score = 1.0, so
    # confidence = 0.65 * 1.0 + 0.35 * 0 = 0.65 → PENDING.
    assert result.status is MatchStatus.PENDING
    assert 0.50 <= result.confidence < 0.85
    assert len(result.candidates) >= 1


@respx.mock
async def test_path_year_disambiguates_popular_long_running_series(db_session, tmp_path: Path):
    """The Wonder Woman 1987 case — the user-reported PENDING bug
    where Stage 3 was suggesting the 1987 vol 2 of a long-running
    series for files clearly belonging to a more recent volume.

    Root cause: filenames in the Mylar / Komga / Kapowarr layout
    often omit the year because the folder carries it
    (``/library/Wonder Woman (2011)/Wonder Woman 023.cbz``).
    Without a year signal, the prefilter's year-distance
    tiebreaker neutralises and CV's relevance ranking surfaces
    the popular old volume for every modern issue. The fix:
    walk the parent directories for a ``(YYYY)`` tag and feed
    it into the same year fallback chain as the filename's own
    year tag.

    This test seeds two CV volumes (1987 and 2011) both named
    "Wonder Woman", both carrying issue #23. Without the path-year
    fix the matcher would tie on series + neutral-year and pick
    the first-returned candidate (1987). With the fix, the
    ``(2011)`` folder tag drives prefilter sort and year-score
    toward the 2011 volume, which wins cleanly.
    """
    await _seed_key(db_session)

    # Build the file under a year-tagged subfolder. ``_make_file_with_location``
    # places at ``tmp_path / filename`` — we need a deeper layout.
    subdir = tmp_path / "Wonder Woman (2011)"
    subdir.mkdir()
    cbz_path = subdir / "Wonder Woman 023.cbz"
    build_cbz(cbz_path, page_count=3, comicinfo=None)
    file_row = File(
        sha256="b" * 64,
        size_bytes=cbz_path.stat().st_size,
        archive_format="cbz",
        page_count=3,
        comicinfo_status=ComicInfoStatus.NONE,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db_session.add(file_row)
    await db_session.flush()
    db_session.add(
        FileLocation(
            file_id=file_row.id,
            path=str(cbz_path),
            mtime=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    # CV returns both Wonder Woman volumes. Order matters: 1987
    # comes first to mimic CV's actual relevance ranking (it's the
    # most-referenced WW volume). Without path_year the matcher
    # would tie on series and pick 1987 in this order; with
    # path_year the 2011 candidate wins by year proximity.
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                [
                    {"id": 1234, "name": "Wonder Woman", "start_year": "1987"},
                    {"id": 5678, "name": "Wonder Woman", "start_year": "2011"},
                ]
            ),
        )
    )
    # Both volumes carry issue #23 — gate doesn't auto-disqualify
    # either side.
    respx.get(f"{BASE_URL}/volume/4050-1234/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 1234,
                    "name": "Wonder Woman",
                    "start_year": "1987",
                    "count_of_issues": 600,
                    "publisher": {"id": 10, "name": "DC"},
                    "issues": [
                        {
                            "id": 9100,
                            "issue_number": "23",
                            "name": "Issue 23",
                            "cover_date": "1988-12-01",
                        }
                    ],
                }
            ),
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-5678/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 5678,
                    "name": "Wonder Woman",
                    "start_year": "2011",
                    "count_of_issues": 52,
                    "publisher": {"id": 10, "name": "DC"},
                    "issues": [
                        {
                            "id": 9200,
                            "issue_number": "23",
                            "name": "Issue 23",
                            "cover_date": "2013-10-01",
                        }
                    ],
                }
            ),
        )
    )

    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    # 2011 volume's issue wins — the path-year tag in
    # ``Wonder Woman (2011)/`` pulled the prefilter and score
    # toward the modern volume.
    assert result.issue_cv_id == 9200


@respx.mock
async def test_search_pool_wide_enough_for_long_tail_volume(db_session, tmp_path: Path):
    """The Wonder Woman New 52 case from the user-reported review queue:
    files with a year tag in the filename (parsed year 2012) were still
    matching to the 1987 Wonder Woman vol 2 because the 2011 New 52 vol
    wasn't in CV's top 10 search results for the name "Wonder Woman".
    CV's relevance ranking surfaces the long-running canonical vol
    first, and popular titles like Batman / Wonder Woman / Detective
    Comics have 30+ same-named volumes (annuals, limited series,
    crossover specials), pushing modern volumes past the cap.

    Bumping ``SEARCH_RESULT_LIMIT`` from 10 to 50 widens the pool the
    prefilter sorts from. The prefilter's year-distance tiebreaker
    then surfaces the year-aligned candidate even when CV ranked it
    far down its own list.

    This test seeds a CV search response with 11 same-named "Wonder
    Woman" volumes — the year-aligned 2011 vol sits at position 12
    (index 11). The old ``SEARCH_RESULT_LIMIT = 10`` would drop it;
    the new 50 keeps it in. The matcher should pick the 2011 vol's
    issue #5 by year proximity."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Wonder Woman 005 (2012).cbr",
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )

    # 12 candidates, all named "Wonder Woman", all returned by CV in
    # decreasing relevance order. The target (2011, year_dist=1) is at
    # the tail. Filler volumes occupy positions 0-10 with implausibly
    # old start_years so they don't accidentally score better.
    search_results = [
        {"id": 1000 + i, "name": "Wonder Woman", "start_year": str(1942 + i)} for i in range(11)
    ]
    # Position 12 (index 11): the year-aligned 2011 vol — what the
    # matcher should actually pick.
    search_results.append({"id": 5678, "name": "Wonder Woman", "start_year": "2011"})
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(200, json=_ok(search_results))
    )

    # Only the top-5-by-prefilter get full /volume/X/ fetches. With
    # the bigger search pool, the prefilter's year-distance sort puts
    # the 2011 vol at position 0 (year_dist=1) — every other candidate
    # has year_dist >= 19. We only need to mock the volumes that
    # actually get fetched. Mock all of them defensively just so a
    # respx ``no route`` error doesn't mask the real bug.
    for stub in search_results:
        respx.get(f"{BASE_URL}/volume/4050-{stub['id']}/").mock(
            return_value=httpx.Response(
                200,
                json=_ok(
                    {
                        "id": stub["id"],
                        "name": stub["name"],
                        "start_year": stub["start_year"],
                        "count_of_issues": 52,
                        "publisher": {"id": 10, "name": "DC"},
                        "issues": [
                            {
                                "id": 90000 + stub["id"],
                                "issue_number": "5",
                                "name": "Issue 5",
                                "cover_date": (f"{int(stub['start_year']) + 1}-06-01"),
                            }
                        ],
                    }
                ),
            )
        )

    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()

    # 2011 vol's issue #5 should win — year_dist=1 beats every other
    # candidate's year_dist >= 19. Under the old cap of 10 the 2011
    # vol wasn't in the prefilter pool at all and one of the older
    # candidates would have won by tiebreaker.
    assert result.issue_cv_id == 95678  # 90000 + 5678
    assert result.status is MatchStatus.AUTO


@respx.mock
async def test_partial_comicinfo_boosts_filename_match(db_session, tmp_path: Path):
    """Partial ComicInfo (series/number/year present, no CV ID) is treated as
    authoritative input to Stage 3, so the matcher uses the ComicInfo values
    even if the filename is gibberish."""
    await _seed_key(db_session)
    xml = build_comicinfo_partial(series="Saga", number="1", year=2012)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="weirdly-named-file.cbz",
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.PARTIAL,
    )
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200,
            json=_ok([{"id": 18166, "name": "Saga", "start_year": "2012"}]),
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [{"id": 100, "issue_number": "1", "name": "ch1"}],
                }
            ),
        )
    )
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    assert result.status is MatchStatus.AUTO
    assert result.confidence >= 0.85


@respx.mock
async def test_excluded_file_skipped(db_session, tmp_path: Path):
    """If a job somehow runs on an excluded file, it doesn't write a row."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    file_row.excluded_from_matching = True
    await db_session.commit()
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    assert result.status is MatchStatus.UNMATCHED
    # No file_matches row written (matcher short-circuited).
    row = await db_session.get(FileMatch, file_row.id)
    assert row is None


# ---- Issue-number normalisation ---------------------------------------


@respx.mock
async def test_leading_zeros_in_filename_match_cv_unpadded(db_session, tmp_path: Path):
    """Filename "001" must match CV's "1" — leading-zero normalisation."""
    await _seed_key(db_session)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        filename="Saga (2012) #007.cbz",
        comicinfo=None,
        comicinfo_status=ComicInfoStatus.NONE,
    )
    respx.get(f"{BASE_URL}/search/").mock(
        return_value=httpx.Response(
            200, json=_ok([{"id": 18166, "name": "Saga", "start_year": "2012"}])
        )
    )
    respx.get(f"{BASE_URL}/volume/4050-18166/").mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                {
                    "id": 18166,
                    "name": "Saga",
                    "start_year": "2012",
                    "count_of_issues": 60,
                    "publisher": {"id": 31, "name": "Image"},
                    "issues": [{"id": 107, "issue_number": "7", "name": "ch7"}],
                }
            ),
        )
    )
    client, cache = _fast_cache()
    try:
        result = await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    assert result.status is MatchStatus.AUTO
    assert result.issue_cv_id == 107


# ---- Rate-limit handling -----------------------------------------------


@respx.mock
async def test_rate_limit_propagates_out_of_the_matcher(db_session, tmp_path: Path):
    """A sustained CV rate limit raises ComicVineRateLimitError out of the
    matcher rather than being swallowed as ``unmatched``.

    The match job relies on this: it catches the exception to re-enqueue
    the file after a cool-down. A file caught by a rate limit must NOT get
    a file_matches row — that would record a non-answer as a final state.
    """
    await _seed_key(db_session)
    xml = build_comicinfo_full(series="Saga", number="1", year=2012, cv_issue_id=345678)
    file_row = await _make_file_with_location(
        db_session,
        tmp_path,
        comicinfo=xml,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
    )
    # CV rate-limits every attempt for the Stage 1 issue lookup.
    respx.get(f"{BASE_URL}/issue/4000-345678/").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )
    client, cache = _fast_cache()
    try:
        with pytest.raises(ComicVineRateLimitError):
            await match_file(file_row.id, db_session, cache)
    finally:
        await client.aclose()
    # No row written — the file stays eligible for a clean retry.
    assert await db_session.get(FileMatch, file_row.id) is None


# Pure-function helper tests live in ``tests/test_matcher_helpers.py``
# (sync, no pytest-asyncio mark) so they don't trip the module-level
# ``pytestmark`` here.
