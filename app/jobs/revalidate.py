"""``revalidate_cv_entity`` RQ job — drives the SWR background refresh.

Enqueued by the cache layer whenever a read serves a stale row. The job
fetches the same entity through the client, persists via the cache, so the
*next* read sees fresh data.

Follows the same per-job-engine + per-job-asyncio.run pattern as
``scan_all_libraries_job``: asyncpg connection pools can't be shared across
event loops, and RQ workers spin up a new loop for each job invocation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from redis import Redis
from rq import Queue, get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.comicvine import ComicVineCache, ComicVineClient, RedisRatePacer
from app.comicvine.errors import ComicVineError, ComicVineRateLimitError
from app.comicvine.pacer import reschedule_delay
from app.config import settings as app_settings

logger = logging.getLogger("longboxes.jobs.revalidate")

_QUEUE_NAME = "default"

# Generous ceiling: a revalidate job paces its CV calls through the shared
# RedisRatePacer and the ``volume_issues`` bulk path can fire ~10 calls for
# a large volume, each potentially sleeping out a pacing wait. Well past
# RQ's 180s default.
REVALIDATE_JOB_TIMEOUT = 1200

# Map entity type → ComicVineCache method. Keeping this explicit avoids
# string-to-method indirection in the hot path.
_REFRESH_METHODS = {
    "volume": "get_volume",
    "issue": "get_issue",
    "story_arc": "get_story_arc",
    "publisher": "get_publisher",
    "character": "get_character",
    "person": "get_person",
    "team": "get_team",
    # Bulk path — one CV ``/issues/?filter=volume:<id>`` call hydrates
    # every issue in the volume, replacing N per-issue ``get_issue``
    # round-trips. The ``cv_id`` arg here is the *volume* cv_id, not
    # an issue id.
    "volume_issues": "hydrate_volume_issues",
}


def revalidate_cv_entity_job(entity_type: str, cv_id: int) -> dict:
    """RQ entrypoint. ``entity_type`` is one of the keys in ``_REFRESH_METHODS``."""
    method_name = _REFRESH_METHODS.get(entity_type)
    if method_name is None:
        logger.warning("revalidate_cv_entity got unknown entity_type=%r; skipping", entity_type)
        return {"status": "skipped", "reason": "unknown_entity"}

    async def _run() -> dict:
        engine = create_async_engine(app_settings.database_url, poolclass=NullPool)
        # Same shared, Redis-backed pacer the match jobs use, so SWR
        # revalidation draws from the same per-resource rate budget.
        pacer = RedisRatePacer(redis_url=app_settings.redis_url)
        client = ComicVineClient(rate_limiter=pacer)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            cache = ComicVineCache(client)
            async with session_factory() as db:
                try:
                    await getattr(cache, method_name)(db, cv_id, force_refresh=True)
                except ComicVineRateLimitError:
                    # Let it propagate out of ``_run`` so the outer
                    # scope can reschedule the job. Mirrors the
                    # ``match_file`` pattern: rate-limited jobs become
                    # SCHEDULED for retry rather than silently
                    # FINISHED, so the polling endpoint sees
                    # ``state="scheduled"`` and ``enqueue_revalidate``'s
                    # dedupe no-ops on subsequent ticks (no hammer-
                    # loop on the rate-limit window).
                    raise
                except ComicVineError as e:
                    # Non-rate-limit CV failure (404, network, etc.).
                    # Revalidation is best-effort SWR: the cached row
                    # stays stale and the next read re-triggers a
                    # refresh, so there's nothing to reschedule here.
                    logger.warning("revalidate failed for %s/%s: %s", entity_type, cv_id, e)
                    return {"status": "error", "error": str(e)}
            return {"status": "ok", "entity_type": entity_type, "cv_id": cv_id}
        finally:
            await client.aclose()
            await pacer.aclose()
            await engine.dispose()

    try:
        return asyncio.run(_run())
    except ComicVineRateLimitError as e:
        delay = reschedule_delay(e.retry_after)
        # Reschedule onto whatever queue *this* job came in on (default,
        # interactive, ...). Without this, an interactive-queue
        # revalidate that rate-limited would retry on the match queue
        # and queue behind 19k match jobs — exactly the lag we're
        # splitting workers to avoid. ``get_current_job().origin`` is
        # the queue name the worker pulled this job from; falls back
        # to ``_QUEUE_NAME`` if we somehow ran outside a worker
        # context (tests, repl).
        job = get_current_job()
        origin = job.origin if job is not None else _QUEUE_NAME
        _reschedule_revalidate(entity_type, cv_id, delay, queue=origin)
        logger.warning(
            "revalidate: CV rate-limited; %s/%s re-scheduled in %.0fs on %r",
            entity_type,
            cv_id,
            delay,
            origin,
        )
        return {
            "status": "rescheduled",
            "retry_after_seconds": round(delay),
        }


def _rescheduled_marker_key(entity_type: str, cv_id: int) -> str:
    """Redis key that says ``(entity_type, cv_id)`` has a rescheduled
    retry already in flight. Read by ``enqueue_revalidate`` to skip
    duplicate enqueues during the cooldown window, and by
    ``rescheduled_retry_after`` so the polling endpoint can surface
    the cooldown in the hydration toast."""
    return f"longboxes:revalidate:rescheduled:{entity_type}:{cv_id}"


def _reschedule_revalidate(
    entity_type: str,
    cv_id: int,
    delay_seconds: float,
    *,
    queue: str = _QUEUE_NAME,
) -> None:
    """Re-schedule a rate-limited revalidate. The retry gets a fresh
    (random) RQ job_id rather than re-using the deterministic id —
    RQ's worker writes the FINISHED status to the deterministic id's
    hash after our function returns, which would clobber a SCHEDULED
    entry under the same id. Instead we stash a TTL'd marker in
    Redis so ``enqueue_revalidate`` knows to skip subsequent
    deterministic-id enqueues until the retry has had a chance to
    fire. This is what breaks the hammer-loop: a 3-second poll sees
    the marker and no-ops, instead of re-enqueueing on every tick.

    Mirrors ``app.jobs.match_file._reschedule_match`` (no job_id),
    plus the marker key for dedupe (which match jobs don't need —
    they aren't pulled by a polling loop).

    ``queue`` selects the RQ queue the retry is scheduled onto —
    keeps interactive-lane revalidates on the interactive lane after
    a rate-limit cooldown, instead of dumping them onto the match
    queue."""
    conn = Redis.from_url(app_settings.redis_url)
    queue_obj = Queue(queue, connection=conn)
    queue_obj.enqueue_in(
        timedelta(seconds=delay_seconds),
        revalidate_cv_entity_job,
        entity_type,
        cv_id,
        job_timeout=REVALIDATE_JOB_TIMEOUT,
    )
    # Marker TTL: the retry delay plus a small grace window, so the
    # key expires only after the retry has had time to fire (success
    # or failure). On expiry, future ``enqueue_revalidate`` calls
    # can resume normally; if the retry hit rate-limit again, it
    # will have set a fresh marker before this one expired.
    conn.setex(
        _rescheduled_marker_key(entity_type, cv_id),
        int(delay_seconds) + 60,
        b"1",
    )


def rescheduled_retry_after(entity_type: str, cv_id: int) -> int | None:
    """Seconds until the next rescheduled retry for ``(entity_type,
    cv_id)`` fires, or ``None`` when no rescheduled retry is
    pending. Read by the polling endpoint so the hydration toast
    can render a rate-limit cooldown instead of falling through to
    the generic "N covers loading…" line.
    """
    conn = Redis.from_url(app_settings.redis_url)
    ttl = conn.ttl(_rescheduled_marker_key(entity_type, cv_id))
    # ``ttl`` returns -2 for missing key, -1 for no TTL set, else
    # the remaining seconds. We only care about the positive case.
    if ttl is None or ttl < 0:
        return None
    # Subtract the 60s grace we added on the way in so the value
    # the toast displays is the user-meaningful "time until retry."
    return max(0, int(ttl) - 60)


def revalidate_job_id(entity_type: str, cv_id: int) -> str:
    """Deterministic RQ job id for a (entity_type, cv_id) revalidate.

    Two reasons we want this stable rather than RQ's default random id:

    * **Dedupe.** A page that reloads (or two browsers hitting the same
      Confirm Volume view) would otherwise enqueue duplicate
      ``volume_issues`` jobs for the same volume — every bulk hydration
      runs at most once-per-volume's worth of CV calls, but each
      duplicate still consumes a queue slot. With a fixed id,
      ``enqueue_revalidate`` no-ops when a job is already in flight.

    * **Status lookups.** The Confirm Volume page's polling loop asks
      "where is this volume's hydration job?" by id, so it can show
      the user position-in-queue / running / cooldown state instead
      of a silent spinner.

    Separator note: RQ's ``validate_job_id`` only permits letters,
    digits, underscores, and dashes; ``parse_job_id`` additionally
    splits composite ids on ``:``. So the obvious ``a:b:c`` shape
    would both be rejected and silently truncated. Dashes are the
    safe high-level separator; ``entity_type`` already uses
    underscores (e.g. ``volume_issues``) so the two characters stay
    visually distinct.

    Force ``int()`` coercion on ``cv_id`` so an accidental float
    (e.g. a CV payload that round-tripped through a JSONB column
    as ``Decimal('1138418.0')``) can't produce
    ``revalidate-volume-1138418.0`` — the period would trip RQ's
    job-id validator and crash the parent job. ``int(cv_id)`` raises
    cleanly when given truly bad data, which is preferable to a
    silently mis-keyed enqueue."""
    return f"revalidate-{entity_type}-{int(cv_id)}"


# Job statuses that count as "still in flight" — a second enqueue for
# the same id while in any of these states should no-op rather than
# overwrite. JobStatus.DEFERRED covers explicit ``depends_on`` chains
# (we don't use them today but the contract is the same).
_IN_FLIGHT_STATUSES = frozenset(
    {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.SCHEDULED,
        JobStatus.DEFERRED,
    }
)


def enqueue_revalidate(
    entity_type: str,
    cv_id: int,
    *,
    at_front: bool = False,
    queue: str = _QUEUE_NAME,
) -> None:
    """Module-level helper used by the cache layer as its enqueue callback.

    ``volume_issues`` jumps to the head of the queue automatically.
    It is the bulk-hydration class — one cheap CV call per ~100 issues —
    and its output (per-issue ``image`` / number / date) is what makes
    review and volume pages render correctly. Letting it strand behind
    a long match backlog leaves covers blank for days; promoting it is
    safe because the job set is bounded (one per distinct volume) and
    individually cheap.

    ``at_front`` lets *callers* request the same head-of-queue placement
    for other entity types. The Confirm Volume page's polling endpoint
    uses this for per-issue revalidates so an interactive cover fetch
    isn't stuck behind a many-thousand-job match backlog — a user
    actively staring at a stub cover shouldn't wait three hours for
    the queue to drain. SWR background revalidates from the cache
    layer keep the default ``False`` (FIFO).

    ``queue`` selects the RQ queue.
    The default ``default`` lane is the match worker's; browse-page
    revalidates (volume / character / team / arc page hydration) pass
    ``queue="interactive"`` (or import the
    ``enqueue_revalidate_interactive`` wrapper below) so they land on
    the interactive worker and don't queue behind the match backlog.

    Uses a deterministic job id (see ``revalidate_job_id``) so a fresh
    enqueue is a no-op when the same job is already queued, running, or
    scheduled. A *terminal* (finished / failed / canceled) prior job is
    deleted so this call can start a fresh attempt under the same id —
    that's how a later page-load nudge can retry after a transient
    failure.
    """
    conn = Redis.from_url(app_settings.redis_url)
    queue_obj = Queue(queue, connection=conn)

    # If a rate-limited retry is already scheduled for this entity,
    # don't enqueue another one — the cooldown is still in effect
    # and a duplicate enqueue would just burn another rate-limit
    # window when the worker picks it up. The marker expires
    # automatically a little after the retry would have fired.
    if conn.exists(_rescheduled_marker_key(entity_type, cv_id)):
        return

    # Build the deterministic job id defensively. A corrupted CV
    # payload that put a non-integer in a cv_id field would crash
    # ``revalidate_job_id``'s ``int()`` coercion here; we'd rather
    # lose one revalidate enqueue than crash the parent (match,
    # scan, page-render) that called us. Same shape below for the
    # actual ``queue.enqueue`` call: validate_job_id raising would
    # otherwise propagate out and fail the parent.
    try:
        job_id = revalidate_job_id(entity_type, cv_id)
    except (TypeError, ValueError) as e:
        logger.warning(
            "enqueue_revalidate(%r, %r): bad cv_id, skipping: %s",
            entity_type,
            cv_id,
            e,
        )
        return

    try:
        existing = Job.fetch(job_id, connection=conn)
    except NoSuchJobError:
        existing = None

    want_at_front = entity_type == "volume_issues" or at_front
    if existing is not None:
        try:
            status = existing.get_status()
        except Exception:
            status = None
        if status == JobStatus.QUEUED and want_at_front:
            # The job is queued but at the tail (typically because an
            # earlier caller enqueued it without ``at_front``). The
            # current caller wants it promoted — RQ has no in-place
            # "move to head" API, so delete the record and re-enqueue
            # below. This is the path that matters for the polling
            # endpoint: a user staring at a stub cover whose
            # per-issue revalidate was first enqueued at the tail of
            # a 13k match backlog now gets it pulled to the front.
            try:
                existing.delete()
            except Exception:
                pass
        elif status in _IN_FLIGHT_STATUSES:
            # STARTED (worker is mid-execution; can't preempt) or
            # SCHEDULED (waiting for a cool-down fire-at; promoting
            # would lose the timestamp). Leave it alone.
            return
        else:
            # Terminal state — clear the stale record so the enqueue
            # below can reuse the deterministic id.
            try:
                existing.delete()
            except Exception:
                # If the cleanup fails (race with the worker, say),
                # fall through and let the enqueue overwrite. Worst
                # case is one duplicate, not a stuck enqueue.
                pass

    try:
        queue_obj.enqueue(
            revalidate_cv_entity_job,
            entity_type,
            cv_id,
            job_timeout=REVALIDATE_JOB_TIMEOUT,
            at_front=want_at_front,
            job_id=job_id,
        )
    except ValueError as e:
        # RQ's ``validate_job_id`` raises ValueError on bad ids
        # ("Job ID must only contain letters, numbers, underscores
        # and dashes"). Defensive — the ``int()`` in
        # ``revalidate_job_id`` should already prevent every known
        # cause, but never let an enqueue failure crash the parent
        # job. A missed background revalidate is harmless; a crashed
        # match job is real lost work.
        logger.warning(
            "enqueue_revalidate(%r, %r): queue.enqueue rejected the job id %r: %s",
            entity_type,
            cv_id,
            job_id,
            e,
        )


def enqueue_revalidate_interactive(entity_type: str, cv_id: int, *, at_front: bool = False) -> None:
    """``enqueue_revalidate`` bound to the ``interactive`` queue.

    The browse-page surface (library, volume, character, team, arc,
    issue, review queue, confirm volume, etc.) imports this in place
    of the bare ``enqueue_revalidate`` so every SWR refresh / page-
    triggered hydration lands on the interactive worker. That worker
    drains the interactive lane in seconds even while the match
    worker is grinding through a 19k-file backlog on ``default``.

    Match-side callers (``app/jobs/match_file.py``,
    ``ComicVineCache``'s SWR fires from inside a match job) keep
    using the plain ``enqueue_revalidate`` so their work stays on
    the match queue.

    Same signature as ``enqueue_revalidate`` minus the ``queue``
    parameter — the import-rename trick at browse-route call sites
    needs the rest of the kwargs to line up so an existing
    ``at_front=True`` call doesn't have to change."""
    enqueue_revalidate(entity_type, cv_id, at_front=at_front, queue="interactive")
