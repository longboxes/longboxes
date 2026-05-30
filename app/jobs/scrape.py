"""RQ jobs for the best-effort ComicVine *web-page* scrapers.

Two background jobs, both wrapping a scraper in ``app.comicvine.scrape``:

* ``scrape_character_volumes_job`` — a character's volume-appearance
  list off its paginated ``issues-cover`` page.
* ``scrape_volume_themes_job`` — a volume's "themes" row.

Each follows the same per-job-engine + per-job-``asyncio.run`` pattern
as ``revalidate_cv_entity_job``: asyncpg connection pools can't be
shared across event loops, and RQ workers spin up a fresh loop per job.
"""

from __future__ import annotations

import asyncio
import logging

from redis import Redis
from rq import Queue
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.comicvine.scrape import (
    scrape_character_volumes,
    scrape_volume_themes,
)
from app.config import settings as app_settings

logger = logging.getLogger("longboxes.jobs.scrape")

_QUEUE_NAME = "default"


def scrape_character_volumes_job(
    character_cv_id: int,
    site_url: str | None = None,
) -> dict:
    """RQ entrypoint. ``character_cv_id`` is a ComicVine *character* id;
    scrapes the character's ``issues-cover`` web page into
    ``cv_character_volumes``."""

    async def _run() -> dict:
        engine = create_async_engine(
            app_settings.database_url, poolclass=NullPool
        )
        try:
            session_factory = async_sessionmaker(
                engine, expire_on_commit=False
            )
            async with session_factory() as db:
                result = await scrape_character_volumes(
                    db, character_cv_id, site_url=site_url
                )
            logger.info(
                "character-volumes scrape cv_id=%s -> %s",
                character_cv_id,
                result,
            )
            return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def enqueue_character_volumes_scrape(
    character_cv_id: int,
    *,
    site_url: str | None = None,
    queue: str = _QUEUE_NAME,
) -> None:
    """Enqueue a character volume-list scrape — the character page fires
    this when the character has no scraped volume list yet.

    The RQ job id is derived from the character id so repeated visits
    while the scrape is still queued / running coalesce onto one job
    rather than piling up duplicate ~45-page walks.

    ``queue`` routes the scrape to one of the worker lanes — browse
    callers pass ``queue="interactive"`` so the scrape lands on the
    interactive worker."""
    conn = Redis.from_url(app_settings.redis_url)
    queue_obj = Queue(queue, connection=conn)
    queue_obj.enqueue(
        scrape_character_volumes_job,
        character_cv_id,
        site_url,
        job_id=f"char-volumes-scrape-{character_cv_id}",
    )


def scrape_volume_themes_job(
    volume_cv_id: int,
    site_url: str | None = None,
) -> dict:
    """RQ entrypoint. ``volume_cv_id`` is a ComicVine *volume* id;
    scrapes the volume's web page for its "themes" row into
    ``cv_volumes.themes``."""

    async def _run() -> dict:
        engine = create_async_engine(
            app_settings.database_url, poolclass=NullPool
        )
        try:
            session_factory = async_sessionmaker(
                engine, expire_on_commit=False
            )
            async with session_factory() as db:
                result = await scrape_volume_themes(
                    db, volume_cv_id, site_url=site_url
                )
            logger.info(
                "volume-themes scrape cv_id=%s -> %s", volume_cv_id, result
            )
            return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def enqueue_volume_themes_scrape(
    volume_cv_id: int,
    *,
    site_url: str | None = None,
    queue: str = _QUEUE_NAME,
) -> None:
    """Enqueue a volume themes scrape — the volume page fires this when
    the volume has no scraped themes yet. The RQ job id is derived from
    the volume id so repeated visits coalesce onto one job.

    ``queue`` routes the scrape to one of the worker lanes — browse
    callers pass ``queue="interactive"`` so the scrape lands on the
    interactive worker."""
    conn = Redis.from_url(app_settings.redis_url)
    queue_obj = Queue(queue, connection=conn)
    queue_obj.enqueue(
        scrape_volume_themes_job,
        volume_cv_id,
        site_url,
        job_id=f"volume-themes-scrape-{volume_cv_id}",
    )


# Convenience wrappers that pin the queue to ``"interactive"``. Browse
# routes import these so they can drop scrapes on the interactive worker
# without threading ``queue="interactive"`` through every call site —
# mirrors ``enqueue_revalidate_interactive`` in ``app.jobs.revalidate``.
# Kept in this module (rather than at each router) so the routing
# choice is reviewable in one place, alongside the underlying job.


def enqueue_character_volumes_scrape_interactive(
    character_cv_id: int,
    *,
    site_url: str | None = None,
) -> None:
    """``enqueue_character_volumes_scrape`` pinned to the ``interactive``
    queue. Browse-page entry point."""
    enqueue_character_volumes_scrape(
        character_cv_id, site_url=site_url, queue="interactive"
    )


def enqueue_volume_themes_scrape_interactive(
    volume_cv_id: int,
    *,
    site_url: str | None = None,
) -> None:
    """``enqueue_volume_themes_scrape`` pinned to the ``interactive``
    queue. Browse-page entry point."""
    enqueue_volume_themes_scrape(
        volume_cv_id, site_url=site_url, queue="interactive"
    )
