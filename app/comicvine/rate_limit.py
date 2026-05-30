"""Async token-bucket rate limiter, keyed by resource type.

ComicVine's documented limit is 200 requests per resource type per hour per
API key (§8). We model this as one token bucket per resource (issue, volume,
person, character, story_arc, search, ...), each refilling at the same
average rate but tracked independently.

The bucket implementation is a leaky token bucket:
- Each bucket has a capacity (initial burst) and a refill rate (tokens/sec).
- ``acquire(resource)`` waits until at least one token is available, then
  consumes it.
- Concurrency is enforced per-resource by an asyncio.Lock so two concurrent
  acquires don't double-spend the same token.

This is in-memory, per-process state. With one worker process and one web
process, that's fine — CV's API key budget is per-key, not per-IP, so any
slop just costs a single 429 we'll retry through.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Defaults: 200 / hour = ~0.0556 tokens/sec. Capacity 10 lets us absorb
# small bursts without immediately blocking. These are deliberately
# conservative; the matcher will hit ``volume`` and ``issue`` hardest.
DEFAULT_CAPACITY = 10
DEFAULT_REFILL_RATE_PER_SECOND = 200 / 3600


@dataclass
class _Bucket:
    capacity: int
    refill_rate: float  # tokens/sec
    tokens: float = field(init=False)
    updated_at: float = field(init=False)
    lock: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()
        self.lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.updated_at = now

    async def acquire(self) -> None:
        """Block until a token is available; consume one."""
        async with self.lock:
            while True:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                # How long until we have one full token?
                missing = 1.0 - self.tokens
                wait = missing / self.refill_rate
                # Release the lock while waiting so other acquires for the
                # same bucket get a fair chance once tokens arrive.
                await asyncio.sleep(wait)


class TokenBucketRateLimiter:
    """One bucket per resource name, lazily created on first use."""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        refill_rate_per_second: float = DEFAULT_REFILL_RATE_PER_SECOND,
    ) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate_per_second
        self._buckets: dict[str, _Bucket] = {}
        # Guards bucket creation. Each bucket has its own lock for acquire().
        self._registry_lock = asyncio.Lock()

    async def acquire(self, resource: str) -> None:
        bucket = self._buckets.get(resource)
        if bucket is None:
            async with self._registry_lock:
                bucket = self._buckets.get(resource)
                if bucket is None:
                    bucket = _Bucket(
                        capacity=self._capacity,
                        refill_rate=self._refill_rate,
                    )
                    self._buckets[resource] = bucket
        await bucket.acquire()

    async def penalize(self, resource: str, seconds: float) -> None:
        """No-op cool-down hook.

        Present so this limiter is duck-type compatible with
        ``RedisRatePacer`` (which a ``ComicVineClient`` calls after a 429).
        The in-memory bucket has no shared, cross-process cool-down to
        apply, so there's nothing useful to do here — production paces
        through ``RedisRatePacer`` instead; this limiter is for tests.
        """
        return None
