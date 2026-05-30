"""Tests for the Redis-backed ComicVine request pacer.

The pacer is exercised against the per-test ``fake_redis`` instance and a
``FakeClock`` so the GCRA maths is deterministic and no real time passes:
the clock only advances when ``acquire`` sleeps.
"""

import pytest

from app.comicvine.errors import ComicVineRateLimitError
from app.comicvine.pacer import DEFAULT_PENALTY_SECONDS, RedisRatePacer

pytestmark = pytest.mark.asyncio


class FakeClock:
    """A controllable clock. ``sleep`` advances time; ``time`` reads it."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _pacer(fake_redis, clock, **kw) -> RedisRatePacer:
    """A pacer wired to the test redis + clock. 180/hour → 20s interval."""
    params = dict(
        rate_per_hour=180,
        burst=8,
        max_inline_wait=45.0,
    )
    params.update(kw)
    return RedisRatePacer(
        redis_client=fake_redis,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
        **params,
    )


async def test_burst_is_free_then_the_gate_paces(fake_redis):
    """The first few calls pass instantly; sustained calls get paced."""
    clock = FakeClock()
    pacer = _pacer(fake_redis, clock)

    start = clock.t
    for _ in range(8):
        await pacer.acquire("volume")
    # The burst allowance means no pacing sleep yet.
    assert clock.t == start

    # Well past the burst now — the gate must have made us wait.
    before = clock.t
    for _ in range(8):
        await pacer.acquire("volume")
    assert clock.t > before


async def test_long_pacing_wait_raises_instead_of_blocking(fake_redis):
    """When the next slot is further out than max_inline_wait, acquire
    raises ComicVineRateLimitError rather than sleeping."""
    clock = FakeClock()
    # 1s inline cap, but the interval is ~20s — so once the burst is spent
    # the wait exceeds the cap and acquire raises.
    pacer = _pacer(fake_redis, clock, burst=2, max_inline_wait=1.0)

    with pytest.raises(ComicVineRateLimitError) as exc:
        for _ in range(20):
            await pacer.acquire("issue")
    assert exc.value.retry_after is not None
    assert exc.value.retry_after > 1.0


async def test_penalize_then_acquire_raises_with_the_cooldown(fake_redis):
    """A penalised gate makes acquire raise, surfacing ~the full cooldown."""
    clock = FakeClock()
    pacer = _pacer(fake_redis, clock)

    await pacer.penalize("volume", 600)
    with pytest.raises(ComicVineRateLimitError) as exc:
        await pacer.acquire("volume")
    assert exc.value.retry_after == pytest.approx(600, abs=2)


async def test_penalize_is_extend_only(fake_redis):
    """A shorter penalty must not shrink an existing, longer cooldown."""
    clock = FakeClock()
    pacer = _pacer(fake_redis, clock)

    await pacer.penalize("volume", 600)
    await pacer.penalize("volume", 60)  # shorter — must be ignored
    with pytest.raises(ComicVineRateLimitError) as exc:
        await pacer.acquire("volume")
    assert exc.value.retry_after == pytest.approx(600, abs=2)


async def test_gates_are_independent_per_resource(fake_redis):
    """Penalising one resource leaves the others untouched."""
    clock = FakeClock()
    pacer = _pacer(fake_redis, clock)

    await pacer.penalize("volume", 600)
    start = clock.t
    # 'issue' has its own gate — unaffected by the 'volume' penalty.
    await pacer.acquire("issue")
    assert clock.t == start


async def test_acquire_resumes_once_the_cooldown_elapses(fake_redis):
    """After enough wall time passes, a penalised gate admits again."""
    clock = FakeClock()
    pacer = _pacer(fake_redis, clock)

    await pacer.penalize("volume", 600)
    clock.t += 700  # the cooldown window has passed
    # No raise, no sleep — the gate is back open.
    await pacer.acquire("volume")


async def test_default_penalty_is_a_sane_window(fake_redis):
    """The fallback cooldown is on the order of CV's hourly window."""
    assert 300 <= DEFAULT_PENALTY_SECONDS <= 3600
