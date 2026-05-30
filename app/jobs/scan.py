"""Scan job + recurring-schedule registration.

The job itself is a thin sync→async bridge: RQ workers are sync, the scanner
is async. ``asyncio.run`` per job is the simplest reliable bridge for an
RQ-style "one job at a time per process" worker.

The recurring schedule is owned by ``rq-scheduler`` (a separate daemon — see
``docker-compose.yml``'s ``scheduler`` service). On worker startup we
re-register the recurring job idempotently: cancel any existing schedule for
this function name, then schedule a fresh one with the interval from
``app_settings.scan_interval_seconds``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from redis import Redis
from rq_scheduler import Scheduler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings as app_settings
from app.scanner import scan_all_libraries

logger = logging.getLogger("longboxes.jobs.scan")

_FUNC_PATH = "app.jobs.scan.scan_all_libraries_job"
# Worker-topology split: the scan is a monolithic long-running job
# that, if it shared the ``default`` queue with the match workers,
# would block them for hours during a fresh import — and worse,
# browse-triggered jobs would queue behind it. Routing it to its own
# ``scan`` queue means the ``worker-scan`` service (WORKER_QUEUES=scan)
# drains it on its own process, untouched by match or interactive
# work. Both ``register_scan_schedule`` and ``enqueue_scan_now``
# route through this constant so the recurring scan and the admin
# "Rescan" button move together.
_QUEUE_NAME = "scan"


def scan_all_libraries_job() -> dict:
    """RQ entrypoint. Runs a full scan and returns the public report dict.

    The job builds its own async engine per invocation (with ``NullPool``)
    instead of reusing ``app.db.SessionLocal``. Reason: ``asyncio.run`` creates
    a fresh event loop on every call, but asyncpg's pooled connections bind
    to the loop they were opened in — sharing a pool across loops trips
    ``RuntimeError: Future attached to a different loop`` on the second job.
    A fresh engine + ``NullPool`` means each session opens and disposes its
    own connection inside the current loop, eliminating cross-loop reuse.
    """
    logger.info("Starting full library scan")

    async def _run() -> dict:
        engine = create_async_engine(app_settings.database_url, poolclass=NullPool)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            result = await scan_all_libraries(session_factory=session_factory)
            return result.report()
        finally:
            await engine.dispose()

    report = asyncio.run(_run())
    logger.info("Scan finished: %s", report)
    return report


def register_scan_schedule(redis_conn: Redis, interval_seconds: int) -> None:
    """Idempotently register the recurring scan job.

    Safe to call multiple times — every call cancels any previous recurring
    scan registration before adding the new one. This is what lets us pick up
    interval changes after an admin edits ``scan_interval_seconds``.
    """
    sched = Scheduler(queue_name=_QUEUE_NAME, connection=redis_conn)
    cancelled = 0
    for job in sched.get_jobs():
        if (job.func_name or "") == _FUNC_PATH:
            sched.cancel(job)
            cancelled += 1
    if cancelled:
        logger.info("Cancelled %d existing recurring scan job(s)", cancelled)

    sched.schedule(
        scheduled_time=datetime.now(tz=UTC),
        func=_FUNC_PATH,
        interval=interval_seconds,
        repeat=None,  # forever
        # If a scan run takes longer than the interval, don't pile up
        # backlog — drop the next firing instead.
        timeout=interval_seconds * 4,
    )
    logger.info("Recurring scan scheduled every %d seconds", interval_seconds)


def enqueue_scan_now(redis_conn: Redis) -> str:
    """Enqueue an ad-hoc scan immediately (used by the admin 'Rescan' button).

    Returns the RQ job ID so the caller can surface it / poll for status.
    """
    from rq import Queue

    queue = Queue(_QUEUE_NAME, connection=redis_conn)
    job = queue.enqueue(scan_all_libraries_job)
    return job.id
