"""Shared helpers for the ComicVine volume-search surfaces.

Both ``/review/volume-search`` (matcher confirm flow) and
``/volume/{old_cv_id}/fix-match`` (browse-side rebind flow) hit CV's
``/search/?resources=volume`` endpoint and render the same card grid
on top of the same facet filter. They used to keep a private copy of
these four helpers each — extracting them here so the two routers
stay in lock-step. A change to the cleaner regex, or to the result
shape, can't drift between them again.

Keep this module narrow: only the search-card pipeline lives here.
``classify_volume_format`` and ``cv_image_url`` stay in cv_helpers
since they're used much more widely.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CvPublisher, CvVolume
from app.services.cv_helpers import classify_volume_format, cv_image_url

# Drop everything that isn't a word char or whitespace. CV's
# ``/search/`` endpoint does fuzzy multi-word matching, but punctuation
# (hyphens left over from subtitle-truncated filenames, colons,
# apostrophes) just adds noise — a clean space-separated token list
# searches best.
_SEARCH_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def clean_search_query(raw: str | None) -> str:
    """Strip punctuation from a raw query and collapse whitespace."""
    if not raw:
        return ""
    cleaned = _SEARCH_STRIP_RE.sub(" ", raw)
    return " ".join(cleaned.split())


def shape_volume_results(envelope: dict) -> list[dict]:
    """Pull the result-card display fields out of a CV ``/search/``
    envelope.

    The envelope's ``results`` is a list of volume payloads (we asked
    for ``resources=volume``). We keep only what the cards render —
    name, year, publisher, issue count, cover thumb, and a format
    classification (ongoing / limited / one-shot / collection) — plus
    the CV id for the confirm link. Entries without an integer ``id``
    are skipped: there's nothing to link to."""
    raw = envelope.get("results")
    if not isinstance(raw, list):
        return []
    shaped: list[dict] = []
    for vol in raw:
        if not isinstance(vol, dict):
            continue
        vid = vol.get("id")
        if not isinstance(vid, int):
            continue
        publisher = vol.get("publisher")
        publisher_name = (
            publisher.get("name") if isinstance(publisher, dict) else None
        )
        first_issue = vol.get("first_issue")
        first_issue_name = (
            first_issue.get("name") if isinstance(first_issue, dict) else None
        )
        shaped.append(
            {
                "cv_id": vid,
                "name": vol.get("name") or "(untitled volume)",
                "year": vol.get("start_year"),
                "publisher": publisher_name,
                "issue_count": vol.get("count_of_issues"),
                "cover_url": cv_image_url(vol, "thumb"),
                # Volume description (wiki HTML) for the result card's
                # hover popover.
                "description": vol.get("description"),
                # Waterfall classify on count + name/deck/description/
                # first-issue-name so the cards can badge the format
                # and the facet filter can narrow by it.
                "format": classify_volume_format(
                    name=vol.get("name"),
                    count_of_issues=vol.get("count_of_issues"),
                    deck=vol.get("deck"),
                    description=vol.get("description"),
                    first_issue_name=first_issue_name,
                ),
            }
        )
    return shaped


def result_facets(results: list[dict]) -> list[dict]:
    """Slim ``(publisher, format)`` projection of the volume search
    results, for the search page's client-side facet filter.

    The page's Alpine ``x-data`` only needs these two fields to drive
    the visible-count readout; serialising the full result dicts —
    each carrying a multi-KB volume description — into an HTML
    attribute would bloat the page badly."""
    return [
        {"publisher": r["publisher"], "format": r["format"]} for r in results
    ]


async def publishers_for_volumes(
    db: AsyncSession, volume_ids: set[int]
) -> dict[int, str]:
    """Map volume cv_id → publisher name, from our local CV cache.

    Search results are CV volumes/issues we may not have ingested,
    so this only resolves publishers for volumes already cached
    (matcher candidates, library volumes). Uncached volumes simply
    get no publisher — far cheaper than a per-result CV fetch. Two
    batch queries: volumes → publisher ids, then publisher names.
    """
    if not volume_ids:
        return {}
    vol_rows = (
        await db.execute(
            select(CvVolume.cv_id, CvVolume.publisher_cv_id).where(
                CvVolume.cv_id.in_(volume_ids)
            )
        )
    ).all()
    publisher_ids = {pid for _, pid in vol_rows if pid is not None}
    if not publisher_ids:
        return {}
    publisher_names = dict(
        (
            await db.execute(
                select(CvPublisher.cv_id, CvPublisher.name).where(
                    CvPublisher.cv_id.in_(publisher_ids)
                )
            )
        ).all()
    )
    return {
        vid: publisher_names[pid]
        for vid, pid in vol_rows
        if pid is not None and pid in publisher_names
    }
