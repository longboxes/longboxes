"""RQ worker entrypoint. Run with `python -m app.worker`.

On startup the worker:

1. Seeds default ``app_settings`` rows (idempotent; turns the
   ``LIBRARY_PATHS`` env var into a DB row on first boot).
2. Reads the current scan interval from ``app_settings``.
3. Registers / refreshes the recurring scan job in rq-scheduler —
   gated to workers that actually handle the ``scan`` queue, so a
   match-only worker doesn't re-register on every startup.

Then it enters the normal RQ work loop. The ``scheduler`` service in
``docker-compose.yml`` is a separate process that does the actual
"poll Redis, enqueue due jobs" work.

The queue list comes from ``settings.worker_queues`` (env var
``WORKER_QUEUES``, comma-separated, ordered by priority). The default
is ``default`` — the match lane. docker-compose runs three workers:
``worker`` (queues=default, the match backlog), ``worker-interactive``
(queues=interactive, browse-triggered hydration) and ``worker-scan``
(queues=scan, the recurring library walk). RQ workers don't preempt,
so each lane lives on its own process — a long scan can't park
hydration jobs behind it the way a single shared worker would.
"""

import asyncio
import logging
import sys

from redis import Redis
from rq import Queue, Worker

from app.config import settings
from app.db import SessionLocal, engine
from app.jobs.scan import register_scan_schedule
from app.services.settings import get_scan_interval_seconds, seed_defaults

logging.basicConfig(
    level=settings.log_level,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("longboxes.worker")


def _parse_queue_names(raw: str) -> list[str]:
    """Comma-split ``raw`` into a list of queue names.

    Whitespace around each entry is stripped. Empty entries are
    dropped. An empty / all-whitespace input yields ``["default"]``
    so a missing env var produces a sensible match-worker default
    rather than a worker that listens to nothing.

    Order is preserved — RQ uses queue order as priority (the worker
    drains the first listed queue before checking the next), so
    ``"interactive,scan"`` means "drain interactive first, only pick
    up scan when interactive is empty."
    """
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return parts or ["default"]


async def _bootstrap() -> int:
    """Seed app_settings defaults and return the current scan interval.

    Disposes the global engine on exit. The asyncpg connection pool would
    otherwise hold connections bound to the loop ``asyncio.run`` created for
    this bootstrap call; subsequent jobs run in their own loops and would
    trip "Future attached to a different loop" if they touched the same
    pool. Each RQ job builds its own engine (see ``scan_all_libraries_job``).
    """
    try:
        async with SessionLocal() as db:
            await seed_defaults(db)
        async with SessionLocal() as db:
            return await get_scan_interval_seconds(db)
    finally:
        await engine.dispose()


def main() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    interval_seconds = asyncio.run(_bootstrap())
    queue_names = _parse_queue_names(settings.worker_queues)
    # Only the worker(s) handling the ``scan`` lane should
    # (re-)register the recurring scan job — otherwise every worker
    # on every restart would idempotently churn the schedule. It's
    # safe to do from multiple workers (the schedule registration
    # cancels any prior copy) but cleaner to gate.
    if "scan" in queue_names:
        register_scan_schedule(redis_conn, interval_seconds)
    queues = [Queue(name, connection=redis_conn) for name in queue_names]
    worker = Worker(queues, connection=redis_conn)
    logger.info("Starting RQ worker on queues: %s", [q.name for q in queues])
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
