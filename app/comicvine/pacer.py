"""Redis-backed request pacer for the ComicVine API.

ComicVine documents a limit of 200 requests per resource type per hour per
API key. The in-memory ``TokenBucketRateLimiter`` can't enforce that across
a real deployment: every ``match_file`` job builds its own client, so its
bucket resets each job and the worker never actually paces itself. This
pacer fixes that by keeping the rate state in *Redis* — shared by every
job and every process that talks to ComicVine.

It is a GCRA gate. One Redis key per resource type holds a "theoretical
arrival time" (TAT). A request is admitted when it arrives no earlier than
``TAT - burst_tolerance``; admitting one pushes TAT forward by one
``interval``. The effect is a smooth, shared cap a little under CV's real
limit, with a small startup burst so a single job doesn't crawl.

How the waiting works:

* ``acquire`` sleeps through *short* pacing waits inline — that keeps a
  file's whole match inside one job run.
* For a *long* wait (after a real rate-limit cool-down) it raises
  ``ComicVineRateLimitError`` carrying ``retry_after``. The match job
  turns that into a delayed re-enqueue, so the worker pauses cleanly and
  the file resumes on its own — no operator re-run needed.
* ``penalize`` shoves a resource's gate far into the future after a real
  HTTP 429. Every process then backs off for that cool-down window
  without anyone having to coordinate.

Concurrency note: the read-modify-write of a gate key is a plain
GET-then-SET, not a WATCH/MULTI transaction. The only heavy consumer is
the RQ worker, which runs jobs strictly serially, so there is effectively
no concurrent writer. A rare lost update (web request racing the worker)
just lets one extra call through — self-correcting via ``penalize`` if it
ever matters.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis

from app.comicvine.errors import ComicVineRateLimitError

logger = logging.getLogger("longboxes.comicvine.pacer")

# Stay under CV's documented 200/hour/resource. 195 leaves a small
# margin for clock drift / burst rounding without trading away ~8%
# throughput to the previous 180 setting. The penalize-on-429 path
# will still add a cooldown if CV's real-world limit turns out
# tighter than documented for our key — so this is the right
# ceiling to push.
DEFAULT_RATE_PER_HOUR = 195
# Small burst allowance so one match job (which fires up to ~9 CV calls)
# isn't paced to a crawl against an otherwise-idle gate.
DEFAULT_BURST = 8
# Waits at or below this are slept through inside ``acquire``; longer waits
# raise ``ComicVineRateLimitError`` so the caller can re-enqueue itself
# instead of tying up the worker for many minutes.
DEFAULT_MAX_INLINE_WAIT_SECONDS = 45.0
# Cool-down applied to a resource's gate after a real HTTP 429 that didn't
# carry a usable ``Retry-After`` header.
DEFAULT_PENALTY_SECONDS = 900


def reschedule_delay(retry_after: float | None) -> float:
    """Seconds to wait before retrying a rate-limited CV job.

    Floors short/missing values at ``DEFAULT_PENALTY_SECONDS`` and
    adds bounded jitter so a backlog that all rate-limited at once
    doesn't thunder back in lockstep when the cooldown lifts. Used by
    both the matcher and revalidate job paths — keeping the formula
    in one place means a backoff tweak applies uniformly.
    """
    base = retry_after if retry_after and retry_after > 0 else DEFAULT_PENALTY_SECONDS
    base = max(base, 30.0)
    return base + random.uniform(0, min(base * 0.25, 120.0))


# Gate keys expire after this much idle time. A missing key just means
# "no throttle yet", which is the correct fresh state — so letting an idle
# gate lapse is harmless housekeeping.
_KEY_TTL_SECONDS = 2 * 3600
_KEY_PREFIX = "longboxes:cv:gate:"

TimeFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class RedisRatePacer:
    """Shared, Redis-backed GCRA pacer for ComicVine requests.

    Duck-type compatible with ``TokenBucketRateLimiter`` — both expose
    ``acquire(resource)`` and ``penalize(resource, seconds)`` — so a
    ``ComicVineClient`` accepts either as its ``rate_limiter``.
    """

    def __init__(
        self,
        *,
        redis_client: aioredis.Redis | None = None,
        redis_url: str | None = None,
        rate_per_hour: int = DEFAULT_RATE_PER_HOUR,
        burst: int = DEFAULT_BURST,
        max_inline_wait: float = DEFAULT_MAX_INLINE_WAIT_SECONDS,
        time_fn: TimeFn = time.time,
        sleep_fn: SleepFn = asyncio.sleep,
    ) -> None:
        if redis_client is None and redis_url is None:
            raise ValueError("RedisRatePacer needs a redis_client or a redis_url")
        self._redis = redis_client
        self._redis_url = redis_url
        self._owns_redis = redis_client is None
        self._interval = 3600.0 / rate_per_hour
        self._burst_tolerance = burst * self._interval
        self._max_inline_wait = max_inline_wait
        self._time = time_fn
        self._sleep = sleep_fn

    # ---- Connection lifecycle ------------------------------------------

    def _client(self) -> aioredis.Redis:
        if self._redis is None:
            # Lazily connect so the connection is opened inside whatever
            # event loop ``acquire`` first runs in (RQ jobs each get a
            # fresh loop). ``decode_responses`` keeps gate values as str.
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def aclose(self) -> None:
        """Close the Redis connection if this pacer created it."""
        if self._owns_redis and self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    # ---- Public API -----------------------------------------------------

    async def acquire(self, resource: str) -> None:
        """Block until a request slot for ``resource`` is available.

        Sleeps through short pacing waits; raises ``ComicVineRateLimitError``
        once the wait is long enough that the caller should re-enqueue
        rather than hold the worker.
        """
        key = _KEY_PREFIX + resource
        redis = self._client()
        while True:
            now = self._time()
            raw = await redis.get(key)
            # Clamp a stale (past) TAT up to ``now`` — an idle gate must
            # not bank unlimited burst credit.
            tat = max(float(raw), now) if raw else now
            allow_at = tat - self._burst_tolerance
            if now >= allow_at:
                # Conformant — consume a slot by advancing the gate.
                await redis.set(key, repr(tat + self._interval), ex=_KEY_TTL_SECONDS)
                return
            wait = allow_at - now
            if wait > self._max_inline_wait:
                raise ComicVineRateLimitError(
                    f"ComicVine pacing: {resource} gate is {wait:.0f}s out",
                    retry_after=wait,
                )
            # Short wait — sleep it off and re-check (another caller may
            # have moved the gate while we slept).
            await self._sleep(wait)

    async def penalize(self, resource: str, seconds: float) -> None:
        """Push ``resource``'s gate ``seconds`` into the future (cool-down).

        Called after a real HTTP 429. Extend-only — never shortens an
        existing, longer cool-down.
        """
        if seconds <= 0:
            return
        key = _KEY_PREFIX + resource
        redis = self._client()
        now = self._time()
        raw = await redis.get(key)
        tat = max(float(raw), now) if raw else now
        # ``+ burst_tolerance`` so the *effective* wait
        # (TAT - burst_tolerance) is the full ``seconds`` rather than
        # ``seconds`` minus the burst credit.
        target = now + seconds + self._burst_tolerance
        new_tat = max(tat, target)
        await redis.set(key, repr(new_tat), ex=_KEY_TTL_SECONDS)
        logger.warning("ComicVine %s gate penalised: backing off ~%.0fs", resource, seconds)
