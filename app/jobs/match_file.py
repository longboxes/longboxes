"""``match_file`` job — Phase 4 real implementation.

Enqueued by the scanner for every new file. Runs the matcher pipeline
(stages 1-4 per §10) and persists a ``file_matches`` row.

Same per-job-engine + ``asyncio.run`` + ``NullPool`` pattern as
``scan_all_libraries_job`` and ``revalidate_cv_entity_job``: RQ workers
spin up a fresh event loop per call, so the engine has to be scoped to
that loop.

When no ComicVine API key is configured the job *holds* — it
reschedules itself with ``NO_KEY_RESCHEDULE_SECONDS`` delay rather
than failing. Once the admin pastes a key the pending rescheduled
jobs fire and the matcher runs. This removes the old race where a
scan running concurrently with the setup wizard's match-all pass
could leave any file scanned after the pass permanently stranded.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from redis import Redis
from rq import Queue
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.comicvine import (
    ComicVineCache,
    ComicVineClient,
    ComicVineRateLimitError,
    RedisRatePacer,
)
from app.comicvine.pacer import reschedule_delay
from app.config import settings
from app.jobs.revalidate import enqueue_revalidate
from app.matcher import match_file as run_matcher
from app.models import ComicInfoStatus, File, FileLocation, FileMatch
from app.services.settings import is_cv_configured

logger = logging.getLogger("longboxes.jobs.match_file")

_QUEUE_NAME = "default"

# Generous ceiling for a single match job. A job paces itself through the
# shared RedisRatePacer, so it can spend a few minutes asleep inside
# ``acquire()`` waiting for request slots — well past RQ's 180s default.
# Long rate-limit cool-downs don't sleep here (they re-enqueue instead),
# so this only has to cover ordinary inline pacing.
MATCH_JOB_TIMEOUT = 1800

# Delay between no-CV-key reschedules. Each held match_file job
# reschedules itself at this cadence until the CV key is configured;
# the wake-up is cheap (one settings lookup + reschedule), so a tight
# value keeps the post-key-save latency small. Long enough that
# tens of thousands of held jobs won't saturate the worker with
# busy-checks.
NO_KEY_RESCHEDULE_SECONDS = 60


def match_file_job(file_id: str) -> dict:
    """RQ entrypoint. ``file_id`` arrives as a string (RQ serialises args).

    If ComicVine rate-limits us, the matcher raises ``ComicVineRateLimitError``
    rather than marking the file unmatched. We catch it here and re-enqueue
    the same file after the cool-down — the worker pauses cleanly and the
    file resumes on its own, with no operator re-run needed.
    """
    parsed_id = uuid.UUID(file_id)
    logger.info("Starting match_file job for file_id=%s", parsed_id)

    async def _run() -> dict:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        # Pace ComicVine calls through the shared, Redis-backed gate so
        # every match job across the whole worker stays under CV's limit.
        pacer = RedisRatePacer(redis_url=settings.redis_url)
        client = ComicVineClient(rate_limiter=pacer)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
            async with session_factory() as db:
                # No ComicVine key configured yet — hold this job by
                # rescheduling it. The scanner always enqueues every
                # new file; the worker is the place that knows whether
                # the matcher can actually run. Once the admin pastes
                # a key, the next time this job fires it'll pass the
                # check and run the matcher.
                if not await is_cv_configured(db):
                    _reschedule_match(
                        file_id, NO_KEY_RESCHEDULE_SECONDS
                    )
                    logger.info(
                        "match_file %s held — no ComicVine API key set; "
                        "rescheduled in %.0fs",
                        parsed_id,
                        NO_KEY_RESCHEDULE_SECONDS,
                    )
                    return {
                        "file_id": str(parsed_id),
                        "status": "held_no_cv_key",
                        "retry_in_seconds": NO_KEY_RESCHEDULE_SECONDS,
                    }
                result = await run_matcher(parsed_id, db, cache)
                # The post-match ``volume_issues`` enqueue (Lever 2
                # from the hyperspeed plan) lives inside
                # ``run_matcher`` — it goes through the same
                # ``enqueue_revalidate`` callback the cache layer
                # uses, so the job wrapper doesn't have to wire it
                # up explicitly.
            return {
                "file_id": str(parsed_id),
                "status": str(result.status),
                "source": str(result.source),
                "confidence": result.confidence,
                "issue_cv_id": result.issue_cv_id,
            }
        finally:
            await client.aclose()
            await pacer.aclose()
            await engine.dispose()

    try:
        report = asyncio.run(_run())
    except ComicVineRateLimitError as e:
        delay = reschedule_delay(e.retry_after)
        _reschedule_match(file_id, delay)
        logger.warning(
            "match_file: ComicVine rate-limited; file %s re-enqueued in %.0fs",
            parsed_id,
            delay,
        )
        return {
            "file_id": str(parsed_id),
            "status": "rescheduled",
            "retry_after_seconds": round(delay),
        }
    logger.info("match_file finished: %s", report)
    return report


# Retry budget for ``_reschedule_match`` when Redis is unreachable.
# Without this, a transient Redis blip during the held-job loop would
# leave the file permanently in the failed registry — the only path
# back is the admin "Match all" button. Three retries with linear
# backoff covers ordinary Redis restarts and brief network blips.
_RESCHEDULE_REDIS_RETRIES = 3
_RESCHEDULE_REDIS_BACKOFF_SECONDS = 2.0


def _reschedule_match(file_id: str, delay_seconds: float) -> None:
    """Re-enqueue a match job for ``file_id`` after ``delay_seconds``.

    Uses RQ's delayed-job support (the worker runs ``with_scheduler=True``),
    so the deferred job is persisted in Redis and survives a worker
    restart — nothing is lost while the cool-down runs.

    Redis errors retry up to ``_RESCHEDULE_REDIS_RETRIES`` times with
    linear backoff so a transient blip during the held-job loop
    doesn't strand the file in the failed registry. If every retry
    fails, the exception propagates and RQ marks the parent job
    failed — recoverable from /admin/failed-jobs or the "Match all"
    button.
    """
    import time

    last_exc: Exception | None = None
    for attempt in range(_RESCHEDULE_REDIS_RETRIES):
        try:
            conn = Redis.from_url(settings.redis_url)
            queue = Queue(_QUEUE_NAME, connection=conn)
            queue.enqueue_in(
                timedelta(seconds=delay_seconds),
                match_file_job,
                file_id,
                job_timeout=MATCH_JOB_TIMEOUT,
            )
            return
        except Exception as e:  # broad — RedisConnectionError, TimeoutError, etc.
            last_exc = e
            logger.warning(
                "match_file: _reschedule_match attempt %d/%d failed for %s: %s",
                attempt + 1,
                _RESCHEDULE_REDIS_RETRIES,
                file_id,
                e,
            )
            if attempt + 1 < _RESCHEDULE_REDIS_RETRIES:
                time.sleep(_RESCHEDULE_REDIS_BACKOFF_SECONDS * (attempt + 1))
    # All retries failed — re-raise so RQ marks the job failed and
    # the admin can recover via /admin/failed-jobs.
    assert last_exc is not None
    raise last_exc


def enqueue_match_file(file_id: uuid.UUID) -> None:
    """Enqueue a match job for the given file. Called by the scanner."""
    conn = Redis.from_url(settings.redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    queue.enqueue(match_file_job, str(file_id), job_timeout=MATCH_JOB_TIMEOUT)


async def enqueue_match_all_unmatched_async(db: AsyncSession) -> int:
    """Async version — for use inside an existing FastAPI event loop.

    Takes a pre-existing session rather than creating its own engine. The
    admin route uses this; the sync ``enqueue_match_all_unmatched`` wrapper
    (CLI) calls this through its own ``asyncio.run`` + per-call engine.

    **Ordering — Levers 3 + 4.** The
    matcher's three input tiers don't pay equal CV-call costs:
    ``FULL_WITH_CVID`` lands in Stage 1 (one issue lookup; with Lever 1
    that lookup is usually a DB short-circuit), ``NONE`` runs the full
    Stage 2-4 search-and-score pipeline. Enqueuing them in scan order
    interleaves cheap and expensive jobs, so the user sees no library
    fill until late in the run.

    We sort the enqueue in two layers:

    1. **Tier** — ``FULL_WITH_CVID`` → ``PARTIAL`` → ``NONE``. The
       cheap tier drains first, so on a 22k-file library the operator
       sees ~14k entries appear in the first few hours.
    2. **Series proxy** — within each tier, by the file's
       lexicographically-first current ``file_locations.path``. Comics
       in the same series almost always share a parent directory
       (``/library/Saga (2012)/issue 001.cbz``), so this clusters
       same-series files together and the matcher fetches each
       parent volume once instead of multiple times across an
       interleaved run. No schema migration — the path correlated
       subquery is cheap, and ``File.id`` provides a final stable
       tiebreaker for files without locations.

    Scanner-time enqueue order (``enqueue_match_file`` above) is
    unaffected — that path interleaves with the filesystem walk and
    accepts arbitrary order; the match-all path is where ordering
    pays off in practice.
    """
    # Tier rank — case expression ordered by the matcher's input cost.
    # FULL_WITH_CVID is cheapest (Stage 1), NONE is most expensive
    # (full Stage 2-4 pipeline).
    tier_rank = case(
        (File.comicinfo_status == ComicInfoStatus.FULL_WITH_CVID.value, 0),
        (File.comicinfo_status == ComicInfoStatus.PARTIAL.value, 1),
        (File.comicinfo_status == ComicInfoStatus.NONE.value, 2),
        else_=3,
    )

    # Within-tier path proxy: the lexicographically-first non-missing
    # location of each file. Comics in the same series share a parent
    # directory in almost every library layout (Mylar, Komga,
    # Kapowarr, plain CBZ folders), so sorting by path clusters
    # same-series files. We pull this as a correlated scalar
    # subquery so the existing single ``select(File.id)`` round-trip
    # stays a single round-trip; the index on file_locations.file_id
    # makes the lookup cheap per row.
    series_proxy = (
        select(func.min(FileLocation.path))
        .where(FileLocation.file_id == File.id)
        .where(FileLocation.missing_since.is_(None))
        .correlate(File)
        .scalar_subquery()
    )

    # Outer join: files that have no file_matches row OR whose match row is
    # in a transient state worth retrying.
    stmt = (
        select(File.id)
        .outerjoin(FileMatch, FileMatch.file_id == File.id)
        .where(File.excluded_from_matching.is_(False))
        .where(
            (FileMatch.file_id.is_(None))
            | (FileMatch.status.in_(("unmatched", "pending")))
        )
        # ``File.id`` last so the order is deterministic even when two
        # files share a path proxy (rare — same content at the same
        # path) or land in the same tier with NULL paths.
        .order_by(tier_rank, series_proxy, File.id)
    )
    file_ids = list((await db.execute(stmt)).scalars())

    conn = Redis.from_url(settings.redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    for fid in file_ids:
        queue.enqueue(match_file_job, str(fid), job_timeout=MATCH_JOB_TIMEOUT)
    logger.info("enqueue_match_all_unmatched: queued %d job(s)", len(file_ids))
    return len(file_ids)


def enqueue_match_all_unmatched() -> int:
    """Sync wrapper for CLI / RQ contexts (no outer event loop).

    Creates its own engine + asyncio.run() per call. Calling this from
    inside an already-running event loop (e.g., a FastAPI route handler)
    raises ``RuntimeError: asyncio.run() cannot be called from a running
    event loop`` — those contexts should use
    ``enqueue_match_all_unmatched_async`` directly.
    """

    async def _run() -> int:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as db:
                return await enqueue_match_all_unmatched_async(db)
        finally:
            await engine.dispose()

    return asyncio.run(_run())
