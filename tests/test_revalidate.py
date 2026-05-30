"""Tests for the revalidate-enqueue policy.

Locks down two behaviours:

* Queue ordering — ``volume_issues`` (bulk hydration) jumps to the
  head, every other entity type stays FIFO at the tail.
* Deterministic-id dedupe — re-enqueueing a job that's already in
  flight is a no-op; a terminal (failed / finished) prior job under
  the same id is cleared and replaced so a later page-load nudge can
  retry.
"""

import fakeredis
from rq import Queue
from rq.job import Job, JobStatus
from rq.registry import FailedJobRegistry

from app.jobs import revalidate


def _patch_redis(monkeypatch, conn):
    """Force ``enqueue_revalidate`` (and ``Job.fetch``) to use the
    fakeredis connection instead of building a real one from the
    configured URL."""
    monkeypatch.setattr(
        revalidate,
        "Redis",
        type("R", (), {"from_url": staticmethod(lambda url: conn)}),
    )


def test_volume_issues_jumps_to_front_of_queue(monkeypatch):
    """``volume_issues`` is the bulk-hydration class — small, finite, and
    what makes review/volume pages show real covers. Promoting it to the
    head of the queue keeps a long match backlog from stranding it."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    revalidate.enqueue_revalidate("volume", 100)
    revalidate.enqueue_revalidate("volume_issues", 200)
    revalidate.enqueue_revalidate("issue", 300)

    queue = Queue("default", connection=conn)
    args_in_queue_order = [queue.fetch_job(jid).args for jid in queue.job_ids]

    assert args_in_queue_order == [
        ("volume_issues", 200),
        ("volume", 100),
        ("issue", 300),
    ]


def test_non_volume_issues_stays_at_tail(monkeypatch):
    """Sanity-check the negative case: at_front is gated on entity_type
    and isn't blanket-on for every revalidate."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    revalidate.enqueue_revalidate("issue", 1)
    revalidate.enqueue_revalidate("issue", 2)
    revalidate.enqueue_revalidate("volume", 3)

    queue = Queue("default", connection=conn)
    args_in_queue_order = [queue.fetch_job(jid).args for jid in queue.job_ids]
    assert args_in_queue_order == [("issue", 1), ("issue", 2), ("volume", 3)]


def test_at_front_promotes_existing_queued_job_to_front(monkeypatch):
    """The motivating case from the field. An earlier polling tick
    (or any other caller) enqueued the per-issue revalidate at the
    default FIFO tail, behind a thousands-of-jobs match backlog. The
    user now lands on the Confirm Volume page and the polling
    endpoint re-calls ``enqueue_revalidate(..., at_front=True)``.
    Without in-place promotion the dedupe would no-op (the job is
    QUEUED, treated as in-flight) and the cover would sit at
    position 13k+ until the backlog drains. The promotion path
    deletes the tail-queued record and re-enqueues at the head."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # Backlog: three unrelated match-like jobs sitting in the queue.
    revalidate.enqueue_revalidate("issue", 1)
    revalidate.enqueue_revalidate("issue", 2)
    revalidate.enqueue_revalidate("issue", 3)

    # An earlier non-interactive tick enqueued 99 at the tail. (In
    # the real system this could be the matcher's per-candidate
    # background revalidate or an earlier polling tick predating
    # the at_front change.)
    revalidate.enqueue_revalidate("issue", 99)

    queue = Queue("default", connection=conn)
    args_before = [queue.fetch_job(j).args for j in queue.job_ids]
    assert args_before == [
        ("issue", 1),
        ("issue", 2),
        ("issue", 3),
        ("issue", 99),
    ]

    # Interactive caller asks for at_front. The existing QUEUED job
    # is promoted to the head, not left at the tail.
    revalidate.enqueue_revalidate("issue", 99, at_front=True)

    args_after = [queue.fetch_job(j).args for j in queue.job_ids]
    assert args_after == [
        ("issue", 99),
        ("issue", 1),
        ("issue", 2),
        ("issue", 3),
    ]
    # No duplicate — the deterministic-id contract is intact.
    assert sum(1 for a in args_after if a == ("issue", 99)) == 1


def test_at_front_does_not_preempt_running_job(monkeypatch):
    """STARTED means a worker is mid-execution; we can't preempt
    that. A duplicate at_front enqueue while running is still a
    no-op (the running job will finish and update the row; a fresh
    enqueue would add a wasted second run)."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    revalidate.enqueue_revalidate("issue", 50)
    job = Job.fetch(revalidate.revalidate_job_id("issue", 50), connection=conn)
    job.set_status(JobStatus.STARTED)
    # Remove from the queue list — STARTED jobs sit in the
    # started-job registry, not the dispatchable queue.
    queue = Queue("default", connection=conn)
    queue.remove(job.id)

    revalidate.enqueue_revalidate("issue", 50, at_front=True)

    # The job is still STARTED; no fresh queued duplicate.
    refetched = Job.fetch(revalidate.revalidate_job_id("issue", 50), connection=conn)
    assert refetched.get_status() == JobStatus.STARTED
    assert revalidate.revalidate_job_id("issue", 50) not in queue.job_ids


def test_at_front_opt_in_promotes_interactive_revalidates(monkeypatch):
    """``at_front=True`` lets callers (the /covers polling endpoint)
    promote an interactive per-issue revalidate ahead of an existing
    backlog. Without this, a user actively waiting on a stub cover
    sits at the tail of however many match jobs the scanner had
    enqueued — a freshly-scanned 22k-file library can put the cover
    at position 13,500+, which is technically correct and entirely
    unusable."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # Build a backlog: three "issue" revalidates at default FIFO.
    revalidate.enqueue_revalidate("issue", 1)
    revalidate.enqueue_revalidate("issue", 2)
    revalidate.enqueue_revalidate("issue", 3)

    # The user clicks Confirm Volume; the polling endpoint enqueues
    # a per-issue revalidate for the stub cover they're staring at.
    revalidate.enqueue_revalidate("issue", 99, at_front=True)

    queue = Queue("default", connection=conn)
    args_in_queue_order = [queue.fetch_job(jid).args for jid in queue.job_ids]
    # 99 jumps to the front, ahead of the backlog.
    assert args_in_queue_order == [
        ("issue", 99),
        ("issue", 1),
        ("issue", 2),
        ("issue", 3),
    ]


def test_deterministic_job_ids_use_entity_and_cv_id(monkeypatch):
    """Every revalidate enqueues with id ``revalidate-{entity}-{cv_id}``
    so the Confirm Volume page's polling endpoint can look up the job's
    state by that id without bookkeeping. Format uses dashes (not
    colons) because RQ's ``validate_job_id`` rejects colons and its
    ``parse_job_id`` would also truncate on them."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    revalidate.enqueue_revalidate("volume_issues", 42)
    job = Job.fetch(revalidate.revalidate_job_id("volume_issues", 42), connection=conn)
    assert job.args == ("volume_issues", 42)
    # Pin the literal format so a contributor who refactors the
    # helper without thinking through downstream consumers sees this
    # test fail.
    assert revalidate.revalidate_job_id("volume_issues", 42) == "revalidate-volume_issues-42"


def test_duplicate_enqueue_while_in_flight_is_no_op(monkeypatch):
    """A second ``enqueue_revalidate`` for the same job while the first
    is still queued must not produce a second job. Page reloads on the
    Confirm Volume view would otherwise spam the queue with duplicates."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    revalidate.enqueue_revalidate("volume_issues", 99)
    revalidate.enqueue_revalidate("volume_issues", 99)
    revalidate.enqueue_revalidate("volume_issues", 99)

    queue = Queue("default", connection=conn)
    expected_id = revalidate.revalidate_job_id("volume_issues", 99)
    assert len(queue.job_ids) == 1
    assert queue.job_ids[0] == expected_id


def test_enqueue_revalidate_does_not_crash_on_bad_cv_id(monkeypatch):
    """A non-integer cv_id (e.g. an accidental float / Decimal /
    string-with-dots from a malformed CV payload) used to crash
    ``enqueue_revalidate`` via RQ's job-id validator — and through
    it, the parent ``match_file_job`` that triggered the
    background revalidate. A lost revalidate enqueue is harmless;
    a crashed match job is real lost work. This pins the defensive
    coercion + try/except shape: a bad cv_id logs a warning and
    returns, the parent keeps running.

    Notable: the regression that motivated this was a cv_id of
    ``Decimal('1138418.0')`` slipping through ``_safe_int`` and
    producing ``revalidate-volume-1138418.0`` — the period trips
    RQ's ``[A-Za-z0-9_-]`` validation."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # A non-numeric cv_id — ``int()`` coercion in
    # ``revalidate_job_id`` raises; ``enqueue_revalidate`` must
    # catch and not propagate.
    revalidate.enqueue_revalidate("issue", "not-a-number")  # type: ignore[arg-type]

    # A float that would survive int() but only as ``1138418`` —
    # round-trip safe via the defensive coercion. Queue should now
    # have one job under the integer-coerced id.
    revalidate.enqueue_revalidate("issue", 1138418.0)  # type: ignore[arg-type]
    queue = Queue("default", connection=conn)
    assert revalidate.revalidate_job_id("issue", 1138418) in queue.job_ids


def test_rate_limited_revalidate_reschedules_and_blocks_re_enqueue(
    monkeypatch,
):
    """When a revalidate hits ``ComicVineRateLimitError``, the job:

    1. Re-schedules itself via ``queue.enqueue_in`` (with a random
       RQ job_id — see ``_reschedule_revalidate`` for why we don't
       reuse the deterministic id).
    2. Stashes a Redis marker so subsequent ``enqueue_revalidate``
       calls under the same ``(entity_type, cv_id)`` no-op until the
       retry has had a chance to fire.

    Without (2), every 3-second poll tick would re-enqueue under the
    deterministic id, the worker would burn another rate-limit
    window pulling the fresh job, and the cooldown would never
    lift. The marker is what breaks the hammer-loop.
    """
    from app.comicvine.errors import ComicVineRateLimitError

    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # Stub asyncio.run so the inner coroutine never actually runs —
    # we don't need a real DB / CV client to exercise the outer
    # error handler. Raising ComicVineRateLimitError from inside
    # asyncio.run is exactly what the cache layer does when CV
    # returns a 420.
    def _fake_run(coro):
        # Close the coroutine to silence "coroutine was never
        # awaited" warnings.
        coro.close()
        raise ComicVineRateLimitError("rate limited", retry_after=42)

    monkeypatch.setattr(revalidate.asyncio, "run", _fake_run)

    result = revalidate.revalidate_cv_entity_job("issue", 1234)

    assert result["status"] == "rescheduled"
    # retry_after=42 from the error, plus the floor of 30s and up
    # to 25% jitter. The exact value varies; just bound it.
    assert 30 <= result["retry_after_seconds"] <= 200

    # The Redis marker is set so further enqueue_revalidate calls
    # for this entity no-op until the cooldown lifts.
    assert conn.exists(revalidate._rescheduled_marker_key("issue", 1234))

    # A second enqueue_revalidate is a no-op while the marker exists.
    # The deterministic-id job (if any) stays untouched, AND no
    # fresh queued job appears at the head — that's what breaks the
    # 3-second hammer-loop the polling endpoint was driving.
    queue = Queue("default", connection=conn)
    scheduled_before = list(queue.scheduled_job_registry.get_job_ids())
    revalidate.enqueue_revalidate("issue", 1234)
    scheduled_after = list(queue.scheduled_job_registry.get_job_ids())
    # The dispatchable queue stays empty; no fresh deterministic-id
    # enqueue happened. (The scheduled retry from the reschedule
    # call is the only thing in flight.)
    assert revalidate.revalidate_job_id("issue", 1234) not in queue.job_ids
    # And the scheduled set didn't grow — the marker blocked the
    # duplicate enqueue from happening at all.
    assert scheduled_before == scheduled_after


def test_rescheduled_retry_after_reports_seconds_remaining(monkeypatch):
    """``rescheduled_retry_after`` lets the polling endpoint surface
    the cooldown in the hydration toast — it returns the number of
    seconds before the retry fires, or None when no retry is
    pending."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # No marker → None.
    assert revalidate.rescheduled_retry_after("issue", 9999) is None

    # Marker present → seconds remaining (minus the 60s grace we
    # added on the way in to give the retry time to actually fire).
    # 300s TTL → user-meaningful 240s remaining.
    conn.setex(
        revalidate._rescheduled_marker_key("issue", 9999),
        300,
        b"1",
    )
    remaining = revalidate.rescheduled_retry_after("issue", 9999)
    assert remaining is not None
    # fakeredis is precise; expect ~240 with a small tolerance.
    assert 230 <= remaining <= 240


def test_terminal_prior_job_is_replaced(monkeypatch):
    """If a previous attempt failed and the failed-registry record is
    still hanging around under the same id, the next enqueue should
    clear it and start fresh — so a user clicking Refresh on a stuck
    volume actually gets a retry, not a silent no-op."""
    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # Seed a finished/failed job with the deterministic id we'll reuse.
    queue = Queue("default", connection=conn)
    job_id = revalidate.revalidate_job_id("volume_issues", 7)
    stale = queue.enqueue(
        revalidate.revalidate_cv_entity_job,
        "volume_issues",
        7,
        job_id=job_id,
    )
    # Drive it into a terminal state directly — fakeredis doesn't run
    # workers, so we move it to the failed registry by hand. The id
    # remains pingable via Job.fetch.
    failed = FailedJobRegistry(queue=queue)
    failed.add(stale, ttl=3600)
    stale.set_status(JobStatus.FAILED)

    # New enqueue should detect the terminal status, delete the stale
    # record, and re-enqueue under the same id.
    revalidate.enqueue_revalidate("volume_issues", 7)

    refetched = Job.fetch(job_id, connection=conn)
    # The refetched job is the fresh one (queued, not failed).
    assert refetched.get_status() == JobStatus.QUEUED
    assert job_id in queue.job_ids
