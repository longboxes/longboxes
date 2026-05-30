"""Backfill the description-link rewriter across existing cv_* rows.

The save-time rewriter in ``app/services/cv_description.py`` only
runs on new upserts. Rows already cached before this feature shipped
still carry ComicVine's original relative URLs in their description
and deck fields. This script walks every cv_* table, re-applies the
rewriter to those fields in place, and saves rows whose payload
actually changed.

Safe properties:

* **Idempotent** — re-running is a no-op on already-rewritten rows.
* **Resumable** — paginates by ``cv_id`` and commits per batch, so
  a kill mid-run leaves a consistent prefix of rewritten rows
  behind.
* **Allocation-free** — only rows whose payload changed are marked
  dirty and persisted, so an idempotent re-run touches no rows.

Usage::

    docker compose exec web python -m app.scripts.backfill_cv_descriptions

Prints per-table counts ``checked / updated`` and a final total.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import SessionLocal
from app.models import (
    CvCharacter,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvStoryArc,
    CvTeam,
    CvVolume,
)
from app.services.cv_description import rewrite_cv_description

# Every cv_* model that carries a raw_payload with description / deck
# fields. Ordered by expected row count ascending so the small tables
# go first and progress feedback feels responsive on a fresh boot.
_MODELS = (
    CvPublisher,
    CvTeam,
    CvStoryArc,
    CvCharacter,
    CvPerson,
    CvVolume,
    CvIssue,
)

# Page size for the cv_id-ordered walk. 500 keeps a single batch's
# JSONB payloads comfortably under typical memory budgets for even
# very large libraries.
_PAGE_SIZE = 500


async def _backfill_model(session, model) -> tuple[int, int]:
    """Walk every row of ``model``, rewriting description/deck in
    place. Returns ``(checked, updated)``."""
    checked = 0
    updated = 0
    last_cv_id: int | None = None

    while True:
        stmt = select(model).order_by(model.cv_id).limit(_PAGE_SIZE)
        if last_cv_id is not None:
            stmt = stmt.where(model.cv_id > last_cv_id)
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            break

        for row in rows:
            checked += 1
            payload = row.raw_payload
            if not isinstance(payload, dict):
                continue
            changed = False
            for field in ("description", "deck"):
                if field not in payload:
                    continue
                original = payload[field]
                rewritten = rewrite_cv_description(original)
                if rewritten != original:
                    payload[field] = rewritten
                    changed = True
            if changed:
                # SQLAlchemy doesn't detect in-place JSONB mutation by
                # default — flag_modified tells the unit of work this
                # column needs writing.
                flag_modified(row, "raw_payload")
                updated += 1

        await session.commit()
        last_cv_id = rows[-1].cv_id

    return checked, updated


async def _main() -> int:
    total_checked = 0
    total_updated = 0
    async with SessionLocal() as session:
        for model in _MODELS:
            checked, updated = await _backfill_model(session, model)
            total_checked += checked
            total_updated += updated
            print(f"{model.__name__:14s}  checked={checked:>7d}  updated={updated:>7d}")
    print(f"{'TOTAL':14s}  checked={total_checked:>7d}  updated={total_updated:>7d}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
