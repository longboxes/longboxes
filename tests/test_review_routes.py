"""Route-level tests for ``app/review/routes.py``.

These tests pin the per-issue fallback behaviour on the
``/review/volume-confirm/{cv_id}/covers`` polling endpoint: when the
volume's bulk-hydration job has finished but the client is still
asking about pending covers, the endpoint enqueues a per-ISSUE
revalidate (full ``/issue/N/`` GET) for each unfilled id. The bulk
job is deliberately NOT re-enqueued — re-running it would just
re-hit the same payload that didn't include the missing images,
and the every-3s polling cadence would hammer the worker into a
stuck-state loop.

We call the route handler as a plain async function rather than
going through the AsyncClient because the handler's dependencies
(DbSession, RequireAdmin) are awkward to wire up here, and the
behaviour under test is purely in the handler body. Redis is faked
at the module level — both ``app.jobs.revalidate`` and
``app.jobs.queue_status`` look it up via ``Redis.from_url``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis
import pytest
from rq import Queue
from rq.job import Job, JobStatus
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import queue_status as queue_status_mod
from app.jobs import revalidate as revalidate_mod
from app.jobs.revalidate import revalidate_job_id
from app.models import CvVolume

pytestmark = pytest.mark.asyncio


def _patch_redis(monkeypatch, conn):
    """Force both ``enqueue_revalidate`` and ``get_job_position`` to use
    the same fakeredis connection. Same pattern as
    ``tests/test_revalidate.py``'s helper; without it the route would
    look up the real Redis URL from settings."""
    fake_redis_class = type(
        "R", (), {"from_url": staticmethod(lambda url: conn)}
    )
    monkeypatch.setattr(revalidate_mod, "Redis", fake_redis_class)
    monkeypatch.setattr(queue_status_mod, "Redis", fake_redis_class)


async def _build_terminal_volume_issues_job(conn, *, cv_id: int) -> None:
    """Set up a FINISHED ``volume_issues`` job under the same
    deterministic id the route looks up, so the cheap pass returns
    ``state="done"``."""
    queue = Queue("default", connection=conn)
    job = queue.enqueue(
        # The job function the route would invoke; never executed in
        # this test (fakeredis doesn't run workers).
        revalidate_mod.revalidate_cv_entity_job,
        "volume_issues",
        cv_id,
        job_id=revalidate_job_id("volume_issues", cv_id),
    )
    # Drive the job into a terminal state directly. fakeredis doesn't
    # run workers; ``set_status`` flips the persisted status flag the
    # status lookup reads.
    job.set_status(JobStatus.FINISHED)
    # Drop the queue list entry too — a finished job no longer sits in
    # the dispatchable queue. Without this, ``Queue.job_ids`` still
    # lists it, which wouldn't change ``state`` (the status read wins),
    # but the surrounding state is more realistic this way.
    queue.remove(job.id)


async def test_covers_polling_enqueues_per_issue_fallback_for_stuck_ids(
    db_session: AsyncSession, monkeypatch
):
    """Bulk job in terminal state + client still waiting on covers →
    enqueue a per-ISSUE revalidate for each unfilled id. The bulk
    job is NOT re-enqueued (re-running it would just re-hit the same
    payload that didn't include the images, and the every-3s polling
    cadence would hammer the worker forever)."""
    from app.review.routes import volume_confirm_covers_hydration

    cv_id = 100
    # Volume must exist in the cache so the surrounding setup is
    # realistic; the polling handler doesn't actually read this row
    # but the foreign key from cv_issues would block the issue insert
    # if it weren't here.
    db_session.add(
        CvVolume(
            cv_id=cv_id,
            name="Saga",
            year=2012,
            raw_payload={"id": cv_id, "name": "Saga"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)

    # Seed: a terminal volume_issues job sits in Redis.
    await _build_terminal_volume_issues_job(conn, cv_id=cv_id)

    # Sanity-check: status starts as "done" / "finished".
    job = Job.fetch(revalidate_job_id("volume_issues", cv_id), connection=conn)
    assert job.get_status() == JobStatus.FINISHED

    # Client asks about issue ids that have no row in the DB — the
    # query returns nothing, no swaps. With ``state == "done"`` AND
    # the client still asking, the per-issue fallback fires.
    response = await volume_confirm_covers_hydration(
        user=None,  # type: ignore[arg-type]  RequireAdminDep is a type hint, not enforced when called directly
        db=db_session,
        volume_cv_id=cv_id,
        ids="999,1001",  # two issues the client is waiting on
    )

    # Per-issue revalidate jobs were enqueued for each unfilled id —
    # AND they're at the head of the queue (``at_front=True``) so an
    # interactive cover fetch doesn't sit behind a multi-thousand
    # match backlog. The polling endpoint lives in
    # ``app/review/routes.py``, which imports
    # ``enqueue_revalidate_interactive as enqueue_revalidate`` (see
    # the worker-topology split), so the jobs land on the
    # ``interactive`` queue rather than ``default``. That's exactly
    # the lane this work belongs on — a user staring at a stub cover
    # shouldn't sit behind the match backlog.
    queue = Queue("interactive", connection=conn)
    assert revalidate_job_id("issue", 999) in queue.job_ids
    assert revalidate_job_id("issue", 1001) in queue.job_ids
    # The two per-issue jobs occupy positions 0 and 1 in the queue
    # (order between them isn't pinned — both were ``at_front``).
    assert set(queue.job_ids[:2]) == {
        revalidate_job_id("issue", 999),
        revalidate_job_id("issue", 1001),
    }
    # And the bulk job is NOT back in the queue — that's the
    # hammer-loop the fallback is designed to avoid.
    assert revalidate_job_id("volume_issues", cv_id) not in queue.job_ids
    # Match queue should be empty — interactive work doesn't bleed
    # over into the match lane.
    assert Queue("default", connection=conn).job_ids == []

    assert response["swaps"] == []
    assert response["completed_ids"] == []
    # The response surfaces the per-issue work in flight via
    # ``queue_status``, not the bulk job's stale "done" state — the
    # toast needs to show what's actually happening so the user
    # isn't staring at "N covers loading…" with no progress signal.
    # A freshly-enqueued per-issue job sits at QUEUED, so the
    # response carries that.
    assert response["queue_status"]["state"] == "queued"
    assert response["queue_status"]["position"] is not None
    assert response["queue_status"]["depth"] is not None


async def test_covers_polling_per_issue_fallback_dedupes_in_flight(
    db_session: AsyncSession, monkeypatch
):
    """Successive polls for the same stuck cover must not enqueue
    duplicate per-issue revalidates. ``enqueue_revalidate`` already
    no-ops on in-flight jobs; this test pins that the polling
    endpoint relies on that contract correctly — so the worker only
    sees one fresh per-issue job per stuck cover, not one per poll
    tick (which would be 20+/minute)."""
    from app.review.routes import volume_confirm_covers_hydration

    cv_id = 200
    db_session.add(
        CvVolume(
            cv_id=cv_id,
            name="X-Men",
            year=1991,
            raw_payload={"id": cv_id, "name": "X-Men"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)
    await _build_terminal_volume_issues_job(conn, cv_id=cv_id)

    # First tick — per-issue job lands on the interactive queue
    # (browse-page surface uses the interactive enqueue wrapper).
    await volume_confirm_covers_hydration(
        user=None, db=db_session, volume_cv_id=cv_id, ids="555",  # type: ignore[arg-type]
    )
    queue = Queue("interactive", connection=conn)
    after_first = list(queue.job_ids)
    assert revalidate_job_id("issue", 555) in after_first

    # Second tick (simulating the next 3-second poll) — the existing
    # per-issue job is still queued (worker hasn't run yet in this
    # fake setup), so ``enqueue_revalidate`` no-ops. Queue stays the
    # same length.
    await volume_confirm_covers_hydration(
        user=None, db=db_session, volume_cv_id=cv_id, ids="555",  # type: ignore[arg-type]
    )
    after_second = list(queue.job_ids)
    assert after_first == after_second


async def test_covers_polling_surfaces_rate_limit_cooldown_via_marker(
    db_session: AsyncSession, monkeypatch
):
    """When a per-issue revalidate has been rescheduled for rate-limit
    cooldown, its deterministic-id job is FINISHED (RQ overwrote the
    SCHEDULED hash on worker exit) — but the Redis marker key from
    the reschedule path still captures the cooldown. The polling
    endpoint falls back to that marker so the toast can render
    "rate-limit cooldown — Xm remaining" instead of falling through
    to the generic count message."""
    from app.jobs import revalidate

    cv_id = 300
    db_session.add(
        CvVolume(
            cv_id=cv_id,
            name="Saga",
            year=2012,
            raw_payload={"id": cv_id, "name": "Saga"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)
    await _build_terminal_volume_issues_job(conn, cv_id=cv_id)

    # Simulate a previous tick's per-issue revalidate having
    # rescheduled itself for cooldown: marker key set with a TTL,
    # no deterministic-id job (post-reschedule cleanup or just
    # never created).
    conn.setex(
        revalidate._rescheduled_marker_key("issue", 777),
        300,  # 300s TTL → user-facing 240s remaining (300 minus 60s grace)
        b"1",
    )

    from app.review.routes import volume_confirm_covers_hydration

    response = await volume_confirm_covers_hydration(
        user=None, db=db_session, volume_cv_id=cv_id, ids="777",  # type: ignore[arg-type]
    )

    # The polling endpoint surfaces the cooldown state from the marker
    # so the hydration toast can show "rate-limit cooldown — Xm".
    assert response["queue_status"]["state"] == "scheduled"
    assert response["queue_status"]["retry_after"] is not None
    # 300s marker - 60s grace = 240s user-facing remaining (with a
    # small tolerance for fakeredis precision).
    assert 230 <= response["queue_status"]["retry_after"] <= 240


async def test_covers_polling_does_not_re_enqueue_when_no_pending_ids(
    db_session: AsyncSession, monkeypatch
):
    """A poll with no ``ids`` parameter (client has nothing to wait on)
    must NOT re-enqueue. Otherwise every idle volume-confirm tab
    would keep spinning the worker on a volume that's already fully
    hydrated."""
    from app.review.routes import volume_confirm_covers_hydration

    cv_id = 101
    db_session.add(
        CvVolume(
            cv_id=cv_id,
            name="X-Men",
            year=1991,
            raw_payload={"id": cv_id, "name": "X-Men"},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    conn = fakeredis.FakeStrictRedis()
    _patch_redis(monkeypatch, conn)
    await _build_terminal_volume_issues_job(conn, cv_id=cv_id)

    response = await volume_confirm_covers_hydration(
        user=None,  # type: ignore[arg-type]
        db=db_session,
        volume_cv_id=cv_id,
        ids="",  # no pending IDs from the client
    )

    # Per-issue fallback is gated on ``ids`` being non-empty (the
    # client must actually be waiting on something) — neither this
    # branch nor the early-return path enqueue anything.
    assert response["queue_status"]["state"] == "done"
    queue = Queue("default", connection=conn)
    # No per-issue jobs got created; queue stays empty (terminal
    # volume_issues job was already removed by the setup helper).
    assert queue.job_ids == []
