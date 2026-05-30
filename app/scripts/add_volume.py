"""CLI helper: add or refresh a ComicVine volume by CV ID.

Usage::

    python -m app.scripts.add_volume <cv_id>

Requires the ComicVine API key to be configured in ``app_settings`` (set it
via the admin UI first). Prints the resulting volume name + issue count on
success; exits non-zero on any CV error.
"""

from __future__ import annotations

import asyncio
import sys

from app.comicvine import ComicVineCache, ComicVineClient
from app.comicvine.errors import ComicVineError
from app.db import SessionLocal
from app.jobs.revalidate import enqueue_revalidate


async def _main(cv_id: int) -> int:
    client = ComicVineClient()
    try:
        cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
        async with SessionLocal() as db:
            try:
                # ``force_refresh=True`` so this CLI is a genuine "go fetch
                # the latest from CV" — without it, a fresh cache entry
                # would short-circuit the call and the script would
                # misleadingly print "ok" without having talked to CV.
                vol = await cache.get_volume(db, cv_id, force_refresh=True)
            except ComicVineError as e:
                print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
                return 1
        print(
            f"ok cv_id={vol.cv_id} name={vol.name!r} "
            f"year={vol.year} count_of_issues={vol.count_of_issues}"
        )
        return 0
    finally:
        await client.aclose()


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m app.scripts.add_volume <cv_id>", file=sys.stderr)
        sys.exit(2)
    try:
        cv_id = int(sys.argv[1])
    except ValueError:
        print("error: cv_id must be an integer", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_main(cv_id)))


if __name__ == "__main__":
    main()
