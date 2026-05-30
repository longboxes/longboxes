"""Tests for the worker-topology split.

Two surfaces under test:

1. ``app.worker._parse_queue_names`` — the env-var → queue-list
   helper. Pure function, exhaustively unit-tested for whitespace,
   empty-string and ordering cases. The match worker, the non-match
   worker, and any future custom shape all walk through this so a
   regression here changes deployment behavior silently.
2. ``app.jobs.revalidate.enqueue_revalidate``'s ``queue`` parameter
   and the ``enqueue_revalidate_interactive`` wrapper — assert that
   the queue the job lands on actually changes when the kwarg or
   the wrapper is used. Backed by fakeredis so the test runs without
   a real RQ broker.
"""

from __future__ import annotations

import pytest
from fakeredis import FakeStrictRedis

from app.jobs.revalidate import (
    enqueue_revalidate,
    enqueue_revalidate_interactive,
)
from app.worker import _parse_queue_names

# ``asyncio_mode = "auto"`` in pyproject.toml routes the async tests
# below through pytest-asyncio without us tagging them — and a
# module-level ``pytestmark = pytest.mark.asyncio`` would
# incorrectly tag the sync ``_parse_queue_names`` tests as async,
# producing PytestWarning noise on every run.


# ---- _parse_queue_names ------------------------------------------------


def test_parse_single_default():
    """The bare default — what the match worker container ships with."""
    assert _parse_queue_names("default") == ["default"]


def test_parse_comma_separated_preserves_order():
    """RQ uses queue order as priority — the worker drains the first
    listed queue before checking the next. So order must be preserved
    exactly through the parse."""
    assert _parse_queue_names("interactive,scan") == ["interactive", "scan"]


def test_parse_strips_whitespace_around_entries():
    """``WORKER_QUEUES=interactive, scan`` with a stray space after
    the comma is a normal shell habit; we shouldn't trip on it."""
    assert _parse_queue_names("interactive,  scan ") == [
        "interactive",
        "scan",
    ]


def test_parse_drops_empty_entries():
    """A trailing comma or doubled comma — common shell editing
    artifact — shouldn't introduce a phantom empty-string queue
    that RQ would then error on."""
    assert _parse_queue_names("default,") == ["default"]
    assert _parse_queue_names("default,,scan") == ["default", "scan"]


def test_parse_empty_string_falls_back_to_default():
    """A missing env var or empty value shouldn't yield an empty list
    (a worker with no queues to listen on); fall back to the match
    queue so the worker still does *something* useful."""
    assert _parse_queue_names("") == ["default"]
    assert _parse_queue_names("   ") == ["default"]


def test_parse_handles_single_queue_with_whitespace():
    """A single-queue value with leading/trailing whitespace —
    common when the env var was set in a YAML quoted string."""
    assert _parse_queue_names("  scan  ") == ["scan"]


# ---- enqueue_revalidate queue routing ----------------------------------


class _FakeRedisFactory:
    """Drop-in replacement for ``Redis.from_url`` that hands every
    caller the same ``FakeStrictRedis`` instance.

    ``enqueue_revalidate`` builds its connection from the URL inside
    the function (it doesn't accept an injection point), so the
    cleanest patch is to swap ``Redis.from_url`` for a callable that
    returns our fake. One shared instance across calls means the
    test can read every queue's state from the same Redis."""

    def __init__(self) -> None:
        self.conn = FakeStrictRedis()

    def __call__(self, *_args, **_kwargs):
        return self.conn


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch every place ``enqueue_revalidate`` reaches for Redis."""
    factory = _FakeRedisFactory()
    monkeypatch.setattr(
        "app.jobs.revalidate.Redis.from_url", factory
    )
    return factory.conn


def _queue_len(conn, name: str) -> int:
    """Count jobs sitting in the queue ``name`` according to RQ's
    Redis key layout. ``rq:queue:<name>`` is a Redis list that the
    worker LPOPs from."""
    return conn.llen(f"rq:queue:{name}")


async def test_enqueue_revalidate_defaults_to_default_queue(fake_redis):
    """The bare ``enqueue_revalidate`` — the match path's import —
    lands on the ``default`` queue. This pin is the safety net: a
    refactor that silently changes the default would route match-side
    enqueues onto the wrong worker without anyone noticing."""
    enqueue_revalidate("volume", 12345)
    assert _queue_len(fake_redis, "default") == 1
    assert _queue_len(fake_redis, "interactive") == 0


async def test_enqueue_revalidate_with_queue_kwarg_routes_correctly(
    fake_redis,
):
    """Explicit ``queue="interactive"`` puts the job on the
    interactive queue. Mirrors what the wrapper does and what
    callers can do directly when they want a non-default lane."""
    enqueue_revalidate("issue", 7777, queue="interactive")
    assert _queue_len(fake_redis, "interactive") == 1
    assert _queue_len(fake_redis, "default") == 0


async def test_interactive_wrapper_routes_to_interactive(fake_redis):
    """``enqueue_revalidate_interactive`` is what browse routes import
    under the ``enqueue_revalidate`` symbol — the import-rename
    trick keeps every existing call site unchanged while pinning
    them all to the interactive lane. This test proves the wrapper
    actually does the routing rather than just forwarding to the
    default."""
    enqueue_revalidate_interactive("character", 1001)
    assert _queue_len(fake_redis, "interactive") == 1
    assert _queue_len(fake_redis, "default") == 0


async def test_interactive_wrapper_preserves_at_front(fake_redis):
    """``at_front=True`` is the head-of-queue placement used by the
    Confirm Volume page's polling endpoint. The wrapper must forward
    it to the underlying enqueue — losing it would strand polling
    revalidates at the tail behind any other interactive work."""
    # First push a sentinel so we can tell at_front actually went to the head.
    enqueue_revalidate_interactive("volume", 1)
    enqueue_revalidate_interactive("issue", 2, at_front=True)
    # Two jobs total in interactive; ordering checked by reading the
    # raw list — RQ's LPUSH/RPUSH means the at_front one is at index 0
    # (the next to be popped).
    assert _queue_len(fake_redis, "interactive") == 2
    head = fake_redis.lindex("rq:queue:interactive", 0)
    # The job id format is deterministic via revalidate_job_id; the
    # head should be the "issue:2" job, not the "volume:1" one. We
    # decode the bytes here just to surface a useful failure
    # message if this ever regresses.
    head_str = head.decode() if isinstance(head, bytes) else head
    assert "issue" in head_str or "2" in head_str, (
        f"at_front=True didn't reach the head of the queue; head={head_str!r}"
    )
