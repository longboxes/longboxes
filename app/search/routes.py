"""Global library + ComicVine search routes.

- ``GET /search``           — multi-section library results (and the
  optional ``?kind=`` drill-down) over the local cache only.
- ``GET /search/live``      — JSON for the header dropdown; library
  cache only, debounced fetch per keystroke.
- ``GET /search/hydration`` — poll endpoint feeding /search's
  ``setupAutoRefresh``; swaps stub rows once the worker hydrates them.
- ``GET /search/comicvine`` — opt-in catalogue search against CV's
  ``/search/`` endpoint, rendered through the same template. Every
  hit links to a local detail URL (``/volume/{cv_id}`` etc.); a
  first click lazy-hydrates the record via the existing cache layer.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import DbSessionDep, RequireUserDep
from app.comicvine import ComicVineCache, ComicVineClient
from app.comicvine.errors import (
    ComicVineError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)

# Browse-style hydration routes through the interactive queue so it
# doesn't queue behind the match backlog. We import the interactive
# wrapper under the bare name so call sites read straight: matches
# the pattern in app/library_browse/routes.py.
from app.jobs.revalidate import (
    enqueue_revalidate_interactive as enqueue_revalidate,
)
from app.services.search import (
    MIN_QUERY_LENGTH,
    SECTION_KEYS,
    SearchResults,
    cv_search_catalogue,
    get_hit_for_hydration,
    search_library,
)
from app.templates_env import templates

logger = logging.getLogger("longboxes.search")

# Kinds that participate in the search-page hydration poll. Mirrors
# the _REVALIDATE_ENTITY_TYPE_BY_KIND keys — every kind the page
# enqueues hydration for is also one the poll can swap in. Anything
# outside this set in the ``?ids=`` payload is dropped silently
# (defensive against URL tampering / future kind additions).
_HYDRATABLE_KINDS = frozenset({"volume", "character", "creator", "team", "arc"})


def _parse_hydration_keys(raw: str) -> list[tuple[str, int]]:
    """Parse a ``?ids=character:21599,team:880`` query string into a
    list of (kind, cv_id) tuples.

    Tokens that don't match the ``<kind>:<int>`` shape are dropped
    quietly — the JS client serializes its in-DOM tokens directly, so
    a stray comma or stale id should fail-safe rather than 400 the
    whole poll request.
    """
    out: list[tuple[str, int]] = []
    if not raw:
        return out
    for token in raw.split(","):
        token = token.strip()
        if ":" not in token:
            continue
        kind, _, raw_id = token.partition(":")
        if kind not in _HYDRATABLE_KINDS:
            continue
        try:
            cv_id = int(raw_id)
        except ValueError:
            continue
        if cv_id <= 0:
            continue
        out.append((kind, cv_id))
    return out

router = APIRouter()


# Per-kind caps.
#
# - ``PAGE_LIMIT_PER_KIND``: the multi-kind /search view shows up to
#   this many rows per section, with a "View all <kind> →" link when
#   the section has more.
# - ``KIND_FILTER_LIMIT``: the /search?kind=<kind> view (what "View
#   all" links to) shows up to this many rows of the one selected
#   kind. No pagination at this tier — if 100 isn't enough, the user
#   can refine the query string.
# - ``LIVE_LIMIT_PER_KIND``: the header dropdown's per-kind cap,
#   kept tight so the JSON payload stays small on every keystroke.
PAGE_LIMIT_PER_KIND = 10
KIND_FILTER_LIMIT = 100
LIVE_LIMIT_PER_KIND = 3


# SearchHit.kind → revalidate.py entity_type. The two diverge for
# creators (kind="creator" in the UI, entity_type="person" in CV's
# vocabulary) and arcs (kind="arc" vs "story_arc"). Kinds without an
# entry don't get hydrated from this layer — local volumes are
# user-authored and need no CV roundtrip; issues are hydrated via
# their parent volume's bulk path.
_REVALIDATE_ENTITY_TYPE_BY_KIND = {
    "volume": "volume",
    "character": "character",
    "creator": "person",
    "team": "team",
    "arc": "story_arc",
}


def _enqueue_stub_hydration(results: SearchResults) -> None:
    """Fire one ``enqueue_revalidate`` per stub in the result set.

    The job's deterministic id dedupes against anything already queued,
    running, or scheduled — so calling this on every /search render is
    cheap (just an EXISTS check per stub) and won't pile up retries
    on a long-running query the user reloads a few times.

    The job routes to the ``interactive`` queue so it bypasses the
    match-worker backlog: a search user staring at a stub character
    shouldn't wait hours for the queue to drain.

    Logs the count at INFO so a ``docker logs web | grep search``
    shows whether the search page actually found anything to
    hydrate — handy when "the images aren't filling in" needs to be
    distinguished from "no stubs were even detected".
    """
    targets: list[tuple[str, int]] = []
    for hits in (
        results.volumes,
        results.characters,
        results.creators,
        results.teams,
        results.arcs,
    ):
        for hit in hits:
            if not hit.is_stub:
                continue
            entity_type = _REVALIDATE_ENTITY_TYPE_BY_KIND.get(hit.kind)
            if entity_type is None:
                continue
            try:
                cv_id = int(hit.key)
            except (TypeError, ValueError):
                continue
            targets.append((entity_type, cv_id))

    if not targets:
        logger.info("search: no stubs to hydrate for q=%r", results.query)
        return

    logger.info(
        "search: enqueueing %d stub hydration(s) for q=%r: %s",
        len(targets), results.query, targets,
    )
    for entity_type, cv_id in targets:
        try:
            enqueue_revalidate(entity_type, cv_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "search: enqueue_revalidate(%r, %r) failed: %s",
                entity_type, cv_id, exc,
            )


@router.get("/search")
async def search_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    q: Annotated[str, Query()] = "",
    kind: Annotated[str | None, Query()] = None,
):
    """Full-page search results.

    Default (``?q=...``): one section per non-empty kind, capped at
    ``PAGE_LIMIT_PER_KIND``. Sections with more results carry a
    "View all <kind> →" link.

    Kind-filter (``?q=...&kind=volumes``): renders only the named
    section, expanded to ``KIND_FILTER_LIMIT``. Unknown kinds fall
    back to the multi-section view rather than 404'ing.

    Empty ``q`` renders the page in its empty-state shell (no DB hit)
    so users who follow a stray ``/search`` link don't see a 400.
    """
    only_kind = kind if kind in SECTION_KEYS else None
    limit = KIND_FILTER_LIMIT if only_kind else PAGE_LIMIT_PER_KIND
    results = await search_library(
        db, q, limit_per_kind=limit, only_kind=only_kind
    )
    # Kick off background hydration for any stub rows the search
    # surfaced (CvVolume ``_stub`` placeholders + credit-walk
    # character/creator/team/arc rows with no cv_* table entry yet).
    # Page renders immediately; the rows fill in as the interactive
    # worker drains. Skipped on /search/live so the live dropdown
    # stays a pure read.
    _enqueue_stub_hydration(results)
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": user,
            "request": request,
            "q": q,
            "kind": only_kind,
            "results": results,
            "min_query_length": MIN_QUERY_LENGTH,
            "section_keys": SECTION_KEYS,
            # ``source`` controls page chrome (heading, "Search
            # ComicVine" CTA, owned/other volume split). The CV
            # route below renders the same template with
            # source="comicvine".
            "source": "library",
        },
    )


# Per-kind cap for the live CV catalogue search. Matches the library
# /search cap so the user gets a consistent "10 per section" experience
# across both surfaces.
CV_PAGE_LIMIT_PER_KIND = PAGE_LIMIT_PER_KIND

# Per-kind cap for the CV-side "View all <kind>" drill-down. Same
# value as the library KIND_FILTER_LIMIT so /search/comicvine?kind=...
# feels symmetric with /search?kind=...
CV_KIND_FILTER_LIMIT = KIND_FILTER_LIMIT

# CV doesn't have a notion of local volumes — that's user-authored
# data on our side — so the CV kind-filter accepts every section key
# except ``local_volumes``. cv_search_catalogue's only_kind handling
# already silently drops unknown values, but explicitly listing the
# valid set here lets the route route a junked-up ``?kind=foo`` to
# the multi-section view rather than an empty result.
_CV_KINDS = frozenset(SECTION_KEYS) - {"local_volumes"}


@router.get("/search/comicvine")
async def search_comicvine_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    q: Annotated[str, Query()] = "",
    kind: Annotated[str | None, Query()] = None,
):
    """Live CV catalogue search rendered through the library template.

    Default (``?q=...``): one CV ``/search/`` call across all six
    resource kinds, each section capped at ``CV_PAGE_LIMIT_PER_KIND``.

    Kind-filter (``?q=...&kind=volumes``): narrows the CV call to one
    resource_type and bumps the cap to ``CV_KIND_FILTER_LIMIT`` — the
    "View all <kind> on ComicVine" drill-down target the multi-section
    view's section header links to.

    Both modes cache through ``cv_search_cache`` (separate keys because
    the ``resources`` + ``limit`` differ), and every result links
    back to a local detail URL where the standard lazy-hydrate-on-view
    flow handles the per-record CV fetch on the click-through.
    """
    only_kind = kind if kind in _CV_KINDS else None
    limit = CV_KIND_FILTER_LIMIT if only_kind else CV_PAGE_LIMIT_PER_KIND
    results = SearchResults(query=q)
    error: ComicVineError | None = None
    if q and len(q.strip()) >= MIN_QUERY_LENGTH:
        # Direct client + cache wiring — same as cv_cache_ctx in
        # library_browse/routes.py, but the route can't import that
        # without a circular dep, so we open one here. ``cv_cache.search``
        # handles the request_key hash + cv_search_cache TTL.
        client = ComicVineClient()
        try:
            cv_cache = ComicVineCache(
                client, enqueue_revalidate=enqueue_revalidate
            )
            try:
                results = await cv_search_catalogue(
                    db, cv_cache, q,
                    limit_per_kind=limit,
                    only_kind=only_kind,
                )
            except ComicVineError as err:
                error = err
        finally:
            await client.aclose()

    if error is not None:
        return _cv_search_error_response(request, user, error)

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": user,
            "request": request,
            "q": q,
            "kind": only_kind,
            "results": results,
            "min_query_length": MIN_QUERY_LENGTH,
            "section_keys": SECTION_KEYS,
            "source": "comicvine",
        },
    )


def _cv_search_error_response(
    request: Request, user, err: ComicVineError
):
    """Friendly error page for /search/comicvine failures. Same shape
    as ``_cv_error_response`` in library_browse, with copy tuned for
    the search use case (the user can fall back to library results)."""
    from fastapi import status

    if isinstance(err, ComicVineKeyMissingError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        message = "Set your ComicVine API key in Admin to search the catalogue."
        hint = None
    elif isinstance(err, ComicVineRateLimitError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        message = "ComicVine is rate-limiting us. Try again in a moment."
        hint = "Library results are still available — click 'Library results' above."
    elif isinstance(err, ComicVineNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        message = "ComicVine returned nothing for that query."
        hint = None
    else:
        status_code = status.HTTP_502_BAD_GATEWAY
        message = "Couldn't reach ComicVine to search the catalogue."
        hint = "This is usually transient — try again in a moment."
    return templates.TemplateResponse(
        request,
        "_load_error.html",
        {
            "user": user,
            "title": "ComicVine search unavailable",
            "message": message,
            "hint": hint,
        },
        status_code=status_code,
    )


@router.get("/search/hydration")
async def search_hydration(
    user: RequireUserDep,
    db: DbSessionDep,
    ids: Annotated[str, Query()] = "",
):
    """Poll endpoint feeding the /search page's ``setupAutoRefresh``.

    Receives the ``kind:cv_id`` tokens the page is still waiting on
    and returns swap HTML for any whose cv_* row has been hydrated
    since the page loaded. Tokens for rows still in stub state are
    not included in either ``swaps`` or ``completed_ids``; the JS
    keeps polling those.
    """
    keys = _parse_hydration_keys(ids)
    if not keys:
        return JSONResponse({"swaps": [], "completed_ids": []})

    tpl = templates.env.get_template("_search_hit_row.html")
    search_hit_row = tpl.module.search_hit_row

    swaps: list[dict] = []
    completed: list[str] = []
    for kind, cv_id in keys:
        hit = await get_hit_for_hydration(db, kind, cv_id)
        if hit is None:
            continue
        target_id = f"search-hit-{kind}-{cv_id}"
        swaps.append({"target_id": target_id, "html": str(search_hit_row(hit))})
        completed.append(f"{kind}:{cv_id}")
    return JSONResponse({"swaps": swaps, "completed_ids": completed})


@router.get("/search/live")
async def search_live(
    user: RequireUserDep,
    db: DbSessionDep,
    q: Annotated[str, Query()] = "",
):
    """JSON shape consumed by ``setupSearchDropdown`` on every keystroke.

    Returns one list per kind. Each row is a flat dict the dropdown
    renders without any extra interpretation — name, subtitle, cover
    URL (may be null), link target, and owned flag for the optional
    "In your library" badge.
    """
    # ``include_credits_stubs=False``: the live dropdown doesn't enqueue
    # hydration (only the full /search page does), and the JSONB credits
    # walk is the slowest part of every keystroke. Hydrated rows still
    # surface; un-hydrated character/creator/team/arc credit stubs are
    # what the dropdown skips.
    results = await search_library(
        db,
        q,
        limit_per_kind=LIVE_LIMIT_PER_KIND,
        include_credits_stubs=False,
    )

    def shape(hits):
        return [
            {
                "key": h.key,
                "name": h.name,
                "subtitle": h.subtitle,
                "cover_url": h.cover_url,
                "detail_url": h.detail_url,
                "owned": h.owned,
                "kind": h.kind,
            }
            for h in hits
        ]

    return {
        "q": results.query,
        "total": results.total,
        "min_query_length": MIN_QUERY_LENGTH,
        "groups": {
            "volumes": shape(results.volumes),
            "local_volumes": shape(results.local_volumes),
            "issues": shape(results.issues),
            "characters": shape(results.characters),
            "creators": shape(results.creators),
            "teams": shape(results.teams),
            "arcs": shape(results.arcs),
        },
    }
