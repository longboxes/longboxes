"""Library browse routes.

All routes here require an authenticated user (``RequireUserDep``).
Library content is shared across users (per design §11); we don't
filter by user. The Phase 11E local-content edit routes are the one
exception — they mutate user-authored data, so they require admin.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.auth.dependencies import DbSessionDep, RequireAdminDep, RequireUserDep
from app.comicvine import ComicVineCache, ComicVineClient
from app.comicvine.errors import (
    ComicVineError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)

# Browse-page enqueues route to the ``interactive`` lane so they
# don't queue behind the match backlog on ``default``. We import the
# interactive wrappers under the bare public symbols so every existing
# call site (including the ``enqueue_revalidate=enqueue_revalidate``
# callback handed to ``ComicVineCache``, which fires from inside an
# async request handler) lands on the interactive worker without
# per-call changes.
from app.jobs.revalidate import (
    enqueue_revalidate_interactive as enqueue_revalidate,
)
from app.jobs.scrape import (
    enqueue_character_volumes_scrape_interactive as enqueue_character_volumes_scrape,
)
from app.jobs.scrape import (
    enqueue_volume_themes_scrape_interactive as enqueue_volume_themes_scrape,
)
from app.models import CvCharacter, CvIssue, CvPublisher, CvStoryArc, CvVolume
from app.services.cv_helpers import parse_id_csv, parse_iso_date, safe_int
from app.services.cv_search import (
    clean_search_query,
    publishers_for_volumes,
    result_facets,
    shape_volume_results,
)
from app.services.duplicates import get_issue_duplicate_group
from app.services.library import (
    LibraryFilters,
    get_arc_detail,
    get_character_detail,
    get_creator_detail,
    get_hydrated_arc_rows,
    get_hydrated_library_rows,
    get_hydrated_volume_credits,
    get_issue_detail,
    get_publisher_detail,
    get_team_detail,
    get_volume_detail,
    list_library_volumes,
    list_publishers_in_library,
)
from app.services.local import (
    SUPPLEMENT_TYPES,
    get_local_issue_detail,
    get_local_volume_detail,
    list_local_volumes,
    merge_local_volumes,
    update_local_issue,
    update_local_volume,
)
from app.services.reader import get_read_progress, progress_bar
from app.services.review import execute_fix_match
from app.services.settings import get_page_size
from app.templates_env import templates


class HydrateIssuesRequest(BaseModel):
    """Body of POST /volume/{cv_id}/hydrate-issues.

    ``issue_cv_ids`` is a list of CV issue IDs to consider for hydration.
    Server filters to stubs (``fetched_at IS NULL``) before enqueuing,
    so callers can safely send already-hydrated IDs without churn."""

    issue_cv_ids: list[int]


def _try_int(s: str | None) -> int | None:
    """Coerce a query-string value to int, treating empty/garbage as None.

    The library filter form posts ``?publisher=&year=`` whenever the
    user clears those dropdowns, and FastAPI's ``int | None`` parser
    rejects empty strings outright (HTTP 422). This helper just
    sidesteps the strict parse — empty or non-numeric values resolve
    to None instead of raising."""
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


async def _publisher_for(db, publisher_cv_id: int | None) -> CvPublisher | None:
    """Load the publisher row by id, fire-and-forget hydrate if it's a stub.

    Returns whatever's currently in the DB — even a stub. The reusable
    ``_publisher_chip.html`` macro gracefully falls back to a text-only
    link when ``raw_payload.image`` isn't present yet, and the next page
    load (after the revalidate worker drains) shows the icon. This keeps
    page render cheap — no synchronous CV call on every volume/issue view.
    """
    if publisher_cv_id is None:
        return None
    pub = await db.get(CvPublisher, publisher_cv_id)
    if pub is None:
        return None
    is_stub = isinstance(pub.raw_payload, dict) and pub.raw_payload.get("_stub") is True
    if is_stub:
        enqueue_revalidate("publisher", pub.cv_id)
    return pub


def _cv_error_response(
    request: Request,
    user,
    err: ComicVineError,
    *,
    entity_label: str,
):
    """Render the friendly ``_load_error.html`` page for a CV-side
    failure on a single-entity page (issue / volume / arc / publisher).

    Splits the user-facing message by exception subtype:
      * ``ComicVineRateLimitError`` → 503 + "rate-limited" hint
      * ``ComicVineNotFoundError`` → 404 + "doesn't exist on CV"
      * any other ``ComicVineError`` → 502 + generic "couldn't reach CV"
    The HTML response carries the site header so the user can keep
    browsing instead of seeing a raw JSON error blob.
    """
    if isinstance(err, ComicVineRateLimitError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        message = f"ComicVine is rate-limiting us. We couldn't load this {entity_label} right now."
        hint = "Wait a minute or two, then try again."
    elif isinstance(err, ComicVineNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        message = f"ComicVine doesn't know about this {entity_label}."
        hint = None
    else:
        status_code = status.HTTP_502_BAD_GATEWAY
        message = f"Couldn't reach ComicVine to load this {entity_label}."
        hint = "This is usually transient — try again in a moment."
    return templates.TemplateResponse(
        request,
        "_load_error.html",
        {
            "user": user,
            "title": f"Couldn't load this {entity_label}",
            "message": message,
            "hint": hint,
        },
        status_code=status_code,
    )


@asynccontextmanager
async def cv_cache_ctx() -> AsyncIterator[ComicVineCache]:
    """Yield a ``ComicVineCache`` wired up with the live HTTP client
    and the worker-enqueue callback, then close the client on exit.

    Every entity page route opens a CV cache the same way — fresh
    ``ComicVineClient``, ``ComicVineCache`` wrapping it,
    ``enqueue_revalidate`` as the background-job hook — and has to
    remember to ``await client.aclose()`` in a finally. This helper
    centralizes the boilerplate so routes can just::

        async with cv_cache_ctx() as cache:
            detail = await get_X_detail(db, cache, cv_id)

    Routes that need to catch ``ComicVineError`` for the user-facing
    error page still do so inside the ``async with`` — the exception
    propagates back through the context manager and gets caught by
    the route's own ``try``/``except``, exactly the same as the
    inline pattern. The context manager only owns the client's
    lifecycle.
    """
    client = ComicVineClient()
    try:
        yield ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
    finally:
        await client.aclose()


def _entity_not_found_response(
    request: Request,
    user,
    *,
    entity_label: str,
    cv_id: int,
    hint: str | None = None,
):
    """Render the friendly 404 page when an entity detail fetch returns
    ``None`` (i.e., not in our cache and CV didn't supply it either).

    Distinct from ``_cv_error_response`` — that handles CV-side
    failures (rate limit / 5xx / explicit "not found from CV").
    This one is "everything succeeded, but we still have no data
    to render," which usually means the entity exists on CV but
    we haven't ingested it yet."""
    return templates.TemplateResponse(
        request,
        "_load_error.html",
        {
            "user": user,
            "title": f"{entity_label.capitalize()} not found",
            "message": f"{entity_label.capitalize()} {cv_id} isn't in our cache.",
            "hint": hint,
        },
        status_code=status.HTTP_404_NOT_FOUND,
    )


router = APIRouter()


# ---- /library --------------------------------------------------------


_LIBRARY_FORMATS = frozenset({"ongoing", "limited", "one_shot", "collection"})


def _normalize_format(raw: str | None) -> str | None:
    """Validate the ?format= facet — one of the four classified volume
    formats, or None for an empty / unrecognised value (a junked-up URL
    just drops the filter)."""
    if raw and raw.strip() in _LIBRARY_FORMATS:
        return raw.strip()
    return None


@router.get("/library")
async def library_index(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    view: Annotated[Literal["grid", "table"], Query()] = "grid",
    publisher: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
    missing_only: Annotated[bool, Query()] = False,
    sort: Annotated[Literal["name", "year", "owned", "missing"], Query()] = "name",
    letter: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    fmt: Annotated[str | None, Query(alias="format")] = None,
):
    """All volumes the user has at least one matched file in.

    Stub-volume hydration: the matcher's Stage 1 path creates placeholder
    ``cv_volumes`` rows when it matches an issue before anything fetched
    the parent volume. Those rows have no cover, year, or count_of_issues
    and look broken on this page. We detect them in the service layer and
    enqueue a background ``revalidate_cv_entity`` job per stub so the
    next view shows full metadata. Cheap — one CV call per stub volume,
    going through the same rate limiter as everything else.

    Paginated: returns the first ``page_size`` matching volumes plus a
    total count. The template wires an Alpine + IntersectionObserver
    "infinite scroll" against ``/library/fragment`` for subsequent pages.
    """
    page_size = await get_page_size(db)
    publisher_cv_id = _try_int(publisher)
    year_int = _try_int(year)
    letter_norm = _normalize_letter(letter)
    q_norm = (q or "").strip() or None
    format_norm = _normalize_format(fmt)
    filters = LibraryFilters(
        publisher_cv_id=publisher_cv_id,
        year=year_int,
        has_missing_only=missing_only,
        sort=sort,
        name_starts_with=letter_norm,
        name_query=q_norm,
        format=format_norm,
    )
    rows, total = await list_library_volumes(
        db, filters, limit=page_size, offset=0, user_id=user.id
    )
    publishers = await list_publishers_in_library(db)
    # Resolve the selected publisher's display name + icon from the
    # already-loaded list so the active-filter chip can render with
    # the logo without a second query. ``None`` when no publisher is
    # selected or the selection isn't in the library (stale URL).
    selected_publisher_name: str | None = None
    selected_publisher_icon: str | None = None
    if publisher_cv_id is not None:
        for pid, pname, picon in publishers:
            if pid == publisher_cv_id:
                selected_publisher_name = pname
                selected_publisher_icon = picon
                break

    # Background-hydrate any stub volumes in the rendered page. The
    # toast in the template shows a live count of pending IDs via
    # ``setupAutoRefresh.pendingCount`` — no need to pass a counter
    # in the context anymore.
    for row in rows:
        if row.is_stub:
            enqueue_revalidate("volume", row.cv_id)

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "user": user,
            "rows": rows,
            "total": total,
            "page_size": page_size,
            "publishers": publishers,
            "filters": filters,
            "view": view,
            "selected_publisher_id": publisher_cv_id,
            "selected_year": year_int,
            "missing_only": missing_only,
            "sort": sort,
            "letter": letter_norm,
            "q": q_norm or "",
            "selected_format": format_norm,
            "selected_publisher_name": selected_publisher_name,
            "selected_publisher_icon": selected_publisher_icon,
        },
    )


@router.get("/library/fragment")
async def library_fragment(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    view: Annotated[Literal["grid", "table"], Query()] = "grid",
    publisher: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
    missing_only: Annotated[bool, Query()] = False,
    sort: Annotated[Literal["name", "year", "owned", "missing"], Query()] = "name",
    letter: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    fmt: Annotated[str | None, Query(alias="format")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Render the next page of library cards (or table rows) for the
    infinite-scroll wiring on ``/library``.

    Returns just the inner items markup — no surrounding chrome — so
    the volume page's Alpine handler can append the response straight
    into the grid / tbody.
    """
    publisher_cv_id = _try_int(publisher)
    year_int = _try_int(year)
    letter_norm = _normalize_letter(letter)
    q_norm = (q or "").strip() or None
    filters = LibraryFilters(
        publisher_cv_id=publisher_cv_id,
        year=year_int,
        has_missing_only=missing_only,
        sort=sort,
        name_starts_with=letter_norm,
        name_query=q_norm,
        format=_normalize_format(fmt),
    )
    page_size = await get_page_size(db)
    rows, _total = await list_library_volumes(
        db, filters, limit=page_size, offset=offset, user_id=user.id
    )
    for row in rows:
        if row.is_stub:
            enqueue_revalidate("volume", row.cv_id)

    template_name = "_library_grid_items.html" if view == "grid" else "_library_table_rows.html"
    return templates.TemplateResponse(
        request,
        template_name,
        {"rows": rows},
    )


@router.get("/library/hydration")
async def library_hydration(
    user: RequireUserDep,
    db: DbSessionDep,
    ids: Annotated[str, Query()] = "",
):
    """Poll endpoint feeding the library page's ``setupAutoRefresh``.

    Client sends the cv_volume IDs it's still waiting on; response
    contains rendered HTML for any whose ``cv_volumes`` row is no
    longer a stub. Card swap covers both the grid and the table
    views — the page only ever shows one at a time but both copies
    can be in the DOM under ``x-show``, so we always emit both
    swaps for completed IDs.
    """
    volume_ids = parse_id_csv(ids)
    if not volume_ids:
        return JSONResponse({"swaps": [], "completed_ids": []})

    rows = await get_hydrated_library_rows(db, volume_ids)
    tpl = templates.env.get_template("_library_card.html")
    library_grid_card = tpl.module.library_grid_card
    library_table_row = tpl.module.library_table_row

    swaps = []
    for row in rows:
        swaps.append(
            {
                "target_id": f"grid-volume-{row.cv_id}",
                "html": str(library_grid_card(row)),
            }
        )
        swaps.append(
            {
                "target_id": f"table-volume-{row.cv_id}",
                "html": str(library_table_row(row)),
            }
        )
    return JSONResponse(
        {
            "swaps": swaps,
            "completed_ids": [row.cv_id for row in rows],
        }
    )


@router.get("/volume-credits/hydration")
async def volume_credits_hydration(
    user: RequireUserDep,
    db: DbSessionDep,
    ids: Annotated[str, Query()] = "",
    credit: Annotated[str | None, Query()] = None,
):
    """Poll endpoint feeding ``volume_credits_list`` on the character /
    creator / team pages.

    Same shape as ``/library/hydration``: client sends the cv_volume
    IDs it's still waiting on, server returns swap HTML for any whose
    ``cv_volumes`` row is no longer a stub. The ``credit`` query param
    (e.g. ``character:21599``, ``team:850``) is baked into each card's
    link target so the swapped row preserves the same "filter the
    /volume page to issues this entity is credited on" behavior the
    original render used.
    """
    volume_ids = parse_id_csv(ids)
    if not volume_ids:
        return JSONResponse({"swaps": [], "completed_ids": []})

    credits = await get_hydrated_volume_credits(db, volume_ids)
    tpl = templates.env.get_template("_volume_credit_card.html")
    volume_credit_card = tpl.module.volume_credit_card

    swaps = [
        {
            "target_id": f"volume-credit-{c.cv_id}",
            "html": str(volume_credit_card(c, credit)),
        }
        for c in credits
    ]
    return JSONResponse(
        {
            "swaps": swaps,
            "completed_ids": [c.cv_id for c in credits],
        }
    )


# ---- /volume/{id} ----------------------------------------------------


def _parse_credit(value: str | None) -> tuple[str, int] | None:
    """Parse the volume page's ``?credit=<kind>:<cv_id>`` filter param.

    ``kind`` must be ``team``, ``creator`` or ``character`` and
    ``cv_id`` a positive integer — e.g. ``team:850``. Returns
    ``(kind, cv_id)`` for a valid value, else None so a junked-up URL
    just drops the filter."""
    if not value or ":" not in value:
        return None
    kind, _, raw_id = value.partition(":")
    if kind not in ("team", "creator", "character") or not raw_id.isdigit():
        return None
    return kind, int(raw_id)


@router.get("/volume/{cv_id}")
async def volume_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    from_issue: Annotated[int | None, Query(alias="from", ge=1)] = None,
    credit: Annotated[str | None, Query()] = None,
):
    """Single volume: metadata + issues table with owned/missing badges.

    Arc population strategy: rather than hydrating every issue (one CV call
    per issue, expensive on big volumes), we let ``get_volume_detail``
    fetch each *story arc* the volume's already-hydrated issues mention.
    Each arc's payload lists its member issues across all volumes, so a
    single fetch can populate arc stripes for many issues — including
    stubs that haven't been individually hydrated yet. Most small volumes
    only have one or two arcs, making this dramatically cheaper than the
    per-issue approach.

    We still trigger background hydration for any stub issues in the
    initial visible window so that per-issue details (cover, name,
    cover_date) fill in too. The arc data shows up immediately via the
    arc-fetch path; the rest catches up on the next refresh.
    """
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_volume_detail(
                db,
                cv_id,
                cv_cache=cache,
                from_issue_cv_id=from_issue,
                user_id=user.id,
                credit_filter=_parse_credit(credit),
            )
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="volume")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="volume",
            cv_id=cv_id,
            hint="Add it from /admin first.",
        )

    # The volume's CV "themes" (genre / era / status) aren't in the
    # JSON API — scrape them off the volume's web page once, in the
    # background, the first time the page is viewed.
    if detail.volume.themes_scraped_at is None:
        enqueue_volume_themes_scrape(
            cv_id,
            site_url=(detail.volume.raw_payload or {}).get("site_detail_url"),
        )

    # ---- Initial-window hydration -------------------------------------
    # Enqueue per-issue hydration for the slice both views land on.
    # ``initial_window_start`` / ``initial_window_size`` are computed
    # inside ``get_volume_detail`` so the route, the template, and the
    # arc rail all share the same window.
    window = detail.issues[
        detail.initial_window_start : detail.initial_window_start + detail.initial_window_size
    ]

    # Per-issue hydration for the visible window only. The bulk
    # ``/issues/?filter=volume:<id>`` walk (fired once by
    # ``_upsert_volume`` on initial registration) already filled
    # cover / name / cover_date for every issue, but it omits
    # ``story_arc_credits`` / ``person_credits`` /
    # ``character_credits`` — the volume page needs the arc data for
    # stripes + boundary arrows. So we fire one ``/issue/<id>/`` per
    # issue in the visible window to upgrade the row to a full
    # payload. The 15-issue page size keeps that burst modest;
    # subsequent ``loadMore`` pages enqueue 15 more each.
    #
    # Two conditions trigger an enqueue:
    #   * ``fetched_at IS NULL`` — true stub, never fetched (the
    #     bulk job hasn't run yet, or is in flight).
    #   * ``raw_payload._bulk_hydrated`` — bulk fetch ran but didn't
    #     include arc credits; the per-issue call upgrades it.
    pending_issue_ids: list[int] = []
    for issue_row in window:
        cv_issue = await db.get(CvIssue, issue_row.cv_id)
        if cv_issue is None:
            continue
        is_bulk_only = (
            isinstance(cv_issue.raw_payload, dict)
            and cv_issue.raw_payload.get("_bulk_hydrated") is True
        )
        if cv_issue.fetched_at is None or is_bulk_only:
            enqueue_revalidate("issue", issue_row.cv_id)
            pending_issue_ids.append(issue_row.cv_id)

    publisher = await _publisher_for(db, detail.volume.publisher_cv_id)

    return templates.TemplateResponse(
        request,
        "volume.html",
        {
            "user": user,
            "detail": detail,
            "publisher": publisher,
            "pending_issue_ids": pending_issue_ids,
            "page_size": await get_page_size(db),
        },
    )


@router.get("/volume/{cv_id}/rail")
async def volume_rail_fragment(
    request: Request,
    cv_id: int,
    user: RequireUserDep,
    db: DbSessionDep,
    start: Annotated[int, Query(ge=0)] = 0,
    count: Annotated[int, Query(ge=1, le=2000)] = 30,
    view: Annotated[Literal["list", "gallery", "arcs"], Query()] = "list",
    credit: Annotated[str | None, Query()] = None,
):
    """Re-render an arc-flow rail SVG for a specific pagination window.

    The volume page's Alpine pagination handlers call this on every
    loadEarlier / loadMore / showAll so the rail stays in lockstep
    with the visible table/shelves. Returns just the SVG fragment
    for the rail container's innerHTML — no surrounding chrome.

    ``view`` picks which rail to return:
      * ``list``    — per-issue nodes, full row alignment.
      * ``gallery`` — per-shelf nodes, branch-appearance rule applied.
      * ``arcs``    — same model as ``list`` (per-issue, every arc)
        but rendered with ``show_nodes=false`` so it reads as the
        spec's "lines only" treatment. The Arcs main pane already
        encodes per-issue arc membership via the shelves; node
        anchors would be redundant.

    Rebuilds the volume's full detail (including arc fetches) on each
    call. Acceptable for now; cacheable later via a per-request memo
    or by exposing arc data on ``VolumeDetail`` directly.
    """
    async with cv_cache_ctx() as cache:
        detail = await get_volume_detail(
            db,
            cv_id,
            cv_cache=cache,
            rail_window_start=start,
            rail_window_size=count,
            credit_filter=_parse_credit(credit),
        )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {cv_id} not in cache.",
        )
    if view == "gallery":
        rail = detail.gallery_rail
        show_nodes = True
    elif view == "arcs":
        # Compact per-issue rail; lines-only.
        rail = detail.arcs_rail
        show_nodes = False
    else:
        rail = detail.list_rail
        show_nodes = True
    return templates.TemplateResponse(
        request,
        "_arc_rail_fragment.html",
        {"rail": rail, "show_nodes": show_nodes},
    )


@router.get("/volume/{cv_id}/issues/hydration")
async def volume_issues_hydration(
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    ids: Annotated[str, Query()] = "",
):
    """Poll endpoint feeding the volume page's ``setupAutoRefresh``.

    Client sends the issue cv_ids it's still waiting on; response
    contains rendered HTML for any whose ``cv_issues`` row is now
    individually hydrated (``fetched_at IS NOT NULL``). Re-runs
    ``get_volume_detail`` to compute the full arc + boundary-arrow
    decoration — same code path as the initial page render so the
    swapped-in row matches byte-for-byte.
    """
    issue_ids = parse_id_csv(ids)
    if not issue_ids:
        return JSONResponse({"swaps": [], "completed_ids": []})

    async with cv_cache_ctx() as cache:
        detail = await get_volume_detail(db, cv_id, cv_cache=cache, user_id=user.id)
    if detail is None:
        return JSONResponse({"swaps": [], "completed_ids": []})

    wanted = set(issue_ids)
    issue_row_macro = templates.env.get_template("_volume_macros.html").module.volume_issue_row
    issue_cover_card_macro = templates.env.get_template("_issue_card.html").module.issue_cover_card

    # Each hydrated issue produces TWO swaps — one for the list view's
    # ``<tr>`` and one for the gallery view's cover-card ``<a>``. The
    # Arcs view's per-arc shelves intentionally skip ID emission (an
    # issue can legitimately appear in multiple arc shelves, which
    # would create duplicate IDs), so they stay stale until the next
    # full reload.
    swaps = []
    completed_ids = []
    for i in detail.issues:
        if i.cv_id in wanted and i.is_hydrated:
            swaps.append(
                {
                    "target_id": f"issue-row-{i.cv_id}",
                    "html": str(issue_row_macro(detail, i)),
                }
            )
            swaps.append(
                {
                    "target_id": f"gallery-issue-{i.cv_id}",
                    "html": str(issue_cover_card_macro(i, dom_id=f"gallery-issue-{i.cv_id}")),
                }
            )
            completed_ids.append(i.cv_id)

    return JSONResponse(
        {
            "swaps": swaps,
            "completed_ids": completed_ids,
        }
    )


@router.post("/volume/{cv_id}/hydrate-issues")
async def hydrate_volume_issues(
    cv_id: int,
    user: RequireUserDep,
    db: DbSessionDep,
    body: HydrateIssuesRequest,
) -> dict:
    """Enqueue per-issue hydration for a set of issues.

    Called by the volume page's Alpine pagination whenever the user
    reveals rows that weren't covered by the initial-window hydration.
    Idempotent and cheap: we filter to issues that actually need a
    per-issue fetch (true stubs OR bulk-only rows missing arc credits)
    before enqueueing, so spam-clicking "Load more" doesn't pile up
    redundant jobs against already-full issues.

    ``cv_id`` is the volume — used to scope the query so a caller
    can't sneak unrelated issue IDs through this endpoint.
    """
    if not body.issue_cv_ids:
        return {"enqueued": 0}
    # An issue "needs hydration" when either:
    #   * ``fetched_at IS NULL`` — never fetched at all.
    #   * ``raw_payload->>'_bulk_hydrated' = 'true'`` — has the cheap
    #     bulk fields but no arc / character credits.
    # Volumes after initial registration will have most rows in the
    # ``_bulk_hydrated`` state; the per-issue call upgrades them.
    needs_fetch_stmt = select(CvIssue.cv_id).where(
        CvIssue.cv_id.in_(body.issue_cv_ids),
        CvIssue.volume_cv_id == cv_id,
        or_(
            CvIssue.fetched_at.is_(None),
            CvIssue.raw_payload["_bulk_hydrated"].astext == "true",
        ),
    )
    target_ids = list((await db.execute(needs_fetch_stmt)).scalars())
    for issue_cv_id in target_ids:
        enqueue_revalidate("issue", issue_cv_id)
    return {"enqueued": len(target_ids)}


# ---- /issue/{id} -----------------------------------------------------


@router.get("/issue/{cv_id}")
async def issue_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
):
    """Single issue: full credits, characters, arcs, neighbors, file info.

    Hydrates the issue via ``cv_cache.get_issue`` if it's a stub or missing.
    This is the only point in the system that makes a per-issue CV call
    (the §8 "second hop"). The cache layer handles SWR — stale issues are
    served immediately while a background revalidation refreshes them.
    """
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_issue_detail(db, cache, cv_id)
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="issue")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="issue",
            cv_id=cv_id,
            hint=(
                "It may not exist on ComicVine, or its volume "
                "hasn't been added to your library yet."
            ),
        )
    publisher = await _publisher_for(db, detail.volume.publisher_cv_id if detail.volume else None)
    # Reading progress for the hero cover's progress bar — the first
    # file on disk, the same one the cover links into the reader for.
    reading_progress = None
    if detail.matched_files:
        reading_progress = progress_bar(
            await get_read_progress(db, user.id, detail.matched_files[0].file_id)
        )
    return templates.TemplateResponse(
        request,
        "issue.html",
        {
            "user": user,
            "detail": detail,
            "publisher": publisher,
            "reading_progress": reading_progress,
        },
    )


@router.get("/issue/{cv_id}/compare")
async def issue_compare(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    cv_id: int,
):
    """Side-by-side comparison of every file claiming one CV issue.

    The issue page's "Compare" button (admin-only, shown when more
    than one file matches an issue) lands here. The view is a
    single-issue version of ``/admin/duplicates`` — same ranking,
    same action set (exclude / make supplement / fix match) — so the
    admin doesn't have to scroll the whole library's duplicate
    inventory to triage one issue.

    Admin-only because every action this page exposes is. A non-admin
    landing here would see a wall of disabled buttons; better to 403
    upstream.

    ≤1 resolved file or unhydrated CV cover → redirect to the issue
    page. The Compare button only renders when there are ≥2 files,
    so a user landing here typed the URL directly or followed a
    stale link; the redirect is the friendly fallback.
    """
    group = await get_issue_duplicate_group(db, cv_id)
    if group is None:
        return RedirectResponse(
            url=f"/issue/{cv_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return templates.TemplateResponse(
        request,
        "issue_compare.html",
        {
            "user": user,
            "group": group,
            # Drives the per-row "Make supplement" dropdown so the
            # vocabulary stays single-sourced in app.services.local.
            "supplement_types": SUPPLEMENT_TYPES,
        },
    )


# ---- /local/volume/{id} + /local/issue/{id} --------------------------
#
# Phase 11C — browse pages for user-authored local content. The id is a
# uuid; the CV ``/volume/{cv_id}`` / ``/issue/{cv_id}`` routes are
# int-typed and the ``/local/`` prefix is static, so the two families
# never collide. A bad id is a plain 404 — unlike a CV miss (which
# offers an "add it from /admin" hint), a wrong local uuid isn't a
# pending state, just wrong.


@router.get("/local/volume/{volume_id}")
async def local_volume_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    volume_id: uuid.UUID,
):
    """A user-authored local volume — core metadata + its issue list."""
    detail = await get_local_volume_detail(db, volume_id, user_id=user.id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local volume.",
        )
    return templates.TemplateResponse(
        request,
        "local_volume.html",
        {
            "user": user,
            "detail": detail,
            # Window size for the issue grid's range-tab pagination —
            # the same admin-tunable page size the CV volume page uses.
            "page_size": await get_page_size(db),
        },
    )


@router.get("/local/issue/{issue_id}")
async def local_issue_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    issue_id: uuid.UUID,
):
    """A user-authored local issue — core metadata, files on disk, and
    prev/next navigation within its local volume."""
    detail = await get_local_issue_detail(db, issue_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local issue.",
        )
    # Reading progress for the hero cover's progress bar — the first
    # file on disk, the same one the cover links into the reader for.
    reading_progress = None
    if detail.files:
        reading_progress = progress_bar(
            await get_read_progress(db, user.id, detail.files[0].file_id)
        )
    return templates.TemplateResponse(
        request,
        "local_issue.html",
        {
            "user": user,
            "detail": detail,
            "reading_progress": reading_progress,
        },
    )


# ---- Editing local content (Phase 11E) -------------------------------
#
# Editing a hand-entered local volume / issue — the fix for typo'd
# metadata. Admin-only (``RequireAdminDep``), like the create-local
# routes, since these mutate user-authored library data. The GET reuses
# the detail builders to pre-fill the form; the POST parses the
# free-text year / cover_date, writes through ``update_local_*``, and
# redirects back to the page (303 — POST-then-GET).


@router.get("/local/volume/{volume_id}/edit")
async def local_volume_edit_form(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    volume_id: uuid.UUID,
):
    """Edit form for a local volume's name / year / publisher."""
    detail = await get_local_volume_detail(db, volume_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local volume.",
        )
    return templates.TemplateResponse(
        request,
        "local_volume_edit.html",
        {"user": user, "detail": detail},
    )


@router.post("/local/volume/{volume_id}/edit")
async def local_volume_edit_submit(
    user: RequireAdminDep,
    db: DbSessionDep,
    volume_id: uuid.UUID,
    volume_name: Annotated[str, Form()] = "",
    volume_year: Annotated[str, Form()] = "",
    publisher_name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
):
    """Commit a local-volume edit, then redirect to the volume page."""
    if not volume_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A volume name is required.",
        )
    # ``volume_year`` is free text — parse leniently, blank/garbage → None.
    parsed_year = safe_int(volume_year)
    result = await update_local_volume(
        db,
        volume_id,
        name=volume_name,
        year=parsed_year,
        publisher_name=publisher_name,
        description=description,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local volume.",
        )
    return RedirectResponse(
        url=f"/local/volume/{volume_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/local/issue/{issue_id}/edit")
async def local_issue_edit_form(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    issue_id: uuid.UUID,
):
    """Edit form for a local issue's number / title / cover date."""
    detail = await get_local_issue_detail(db, issue_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local issue.",
        )
    return templates.TemplateResponse(
        request,
        "local_issue_edit.html",
        {"user": user, "detail": detail},
    )


@router.post("/local/issue/{issue_id}/edit")
async def local_issue_edit_submit(
    user: RequireAdminDep,
    db: DbSessionDep,
    issue_id: uuid.UUID,
    issue_number: Annotated[str, Form()] = "",
    issue_name: Annotated[str, Form()] = "",
    cover_date: Annotated[str, Form()] = "",
):
    """Commit a local-issue edit, then redirect to the issue page."""
    # ``cover_date`` is an optional ISO date from a ``<input type=date>``;
    # blank or unparseable → None.
    parsed_date = parse_iso_date(cover_date)
    result = await update_local_issue(
        db,
        issue_id,
        issue_number=issue_number,
        name=issue_name,
        cover_date=parsed_date,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local issue.",
        )
    return RedirectResponse(
        url=f"/local/issue/{issue_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- Merging local volumes (Phase 11E) -------------------------------
#
# The backstop for accidental duplicate local volumes ("My Indie
# Series" catalogued twice). The reviewer opens the merge form from one
# volume and looks up another; ``merge_local_volumes`` reassigns the
# picked volume's issues here and deletes it. Admin-only, like the
# other local-content mutations.


@router.get("/local/volume/{volume_id}/merge")
async def local_volume_merge_form(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    volume_id: uuid.UUID,
):
    """The merge form — pick another local volume to fold into this one."""
    detail = await get_local_volume_detail(db, volume_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such local volume.",
        )
    # Every OTHER local volume is a merge-source candidate; the volume
    # being merged into is filtered out so it can't be picked.
    others = [v for v in await list_local_volumes(db) if v["id"] != str(volume_id)]
    return templates.TemplateResponse(
        request,
        "local_volume_merge.html",
        {"user": user, "volume": detail, "other_volumes": others},
    )


@router.post("/local/volume/{volume_id}/merge")
async def local_volume_merge_submit(
    user: RequireAdminDep,
    db: DbSessionDep,
    volume_id: uuid.UUID,
    source_volume_id: Annotated[str, Form()] = "",
):
    """Commit a merge: fold the picked volume into this one, then
    redirect to this volume's (now larger) page."""
    source_id: uuid.UUID | None = None
    if source_volume_id.strip():
        try:
            source_id = uuid.UUID(source_volume_id.strip())
        except ValueError:
            source_id = None
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pick a local volume to merge in.",
        )
    result = await merge_local_volumes(db, target_id=volume_id, source_id=source_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Couldn't merge — the chosen volume no longer exists, or it's this same volume."
            ),
        )
    return RedirectResponse(
        url=f"/local/volume/{volume_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- /arc/{id} -------------------------------------------------------


@router.get("/arc/{cv_id}")
async def arc_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    view: Annotated[Literal["list", "gallery"], Query()] = "list",
):
    """Story arc page: the arc's member issues across every volume the
    arc touches, with owned-vs-missing status from the user's library.

    ``view`` toggles between a flat issues table (in arc reading order)
    and a per-volume gallery (shelves grouped by parent volume). No
    rail — the arc IS the unit of interest on this page, so the
    volume-level rail visualization would have nothing extra to say.
    """
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_arc_detail(db, cache, cv_id)
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="story arc")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="story arc",
            cv_id=cv_id,
        )

    # Publisher comes straight off the arc's CV payload (``publisher``
    # is a top-level field on a story arc, not derived from volumes).
    # ``_publisher_for`` fires a background revalidate if the row is
    # a stub.
    publisher = await _publisher_for(db, detail.publisher_cv_id)

    # Per-issue hydration for the arc's *initial window* only — same
    # PAGE_SIZE slice the template's Alpine pagination lands on.
    # Bigger arcs (Civil War-scale crossovers can run a couple
    # hundred issues) would otherwise blast the CV rate limiter
    # with one revalidate per member. ``loadMore`` / ``loadEarlier``
    # in the template POST to ``/arc/{id}/hydrate-issues`` to enqueue
    # additional slices as the user pages through.
    #
    # Two cases trigger an enqueue inside the window:
    #   * Row exists but is a true stub (``fetched_at IS NULL``) or
    #     a bulk-only row missing arc credits.
    #   * Row doesn't exist in ``cv_issues`` at all — the arc payload
    #     references it, but no volume fetch has registered it yet.
    #     ``ArcIssueRow`` defaults ``is_hydrated=False`` in this
    #     case, so the same path picks it up.
    page_size = await get_page_size(db)
    window = detail.issues[:page_size]
    pending_issue_ids: list[int] = []
    for issue_row in window:
        if issue_row.is_hydrated:
            cv_issue = await db.get(CvIssue, issue_row.cv_id)
            is_bulk_only = (
                cv_issue is not None
                and isinstance(cv_issue.raw_payload, dict)
                and cv_issue.raw_payload.get("_bulk_hydrated") is True
            )
            if not is_bulk_only:
                continue
        enqueue_revalidate("issue", issue_row.cv_id)
        pending_issue_ids.append(issue_row.cv_id)

    return templates.TemplateResponse(
        request,
        "arc.html",
        {
            "user": user,
            "detail": detail,
            "view": view,
            "publisher": publisher,
            "pending_issue_ids": pending_issue_ids,
            "page_size": page_size,
        },
    )


@router.post("/arc/{cv_id}/hydrate-issues")
async def hydrate_arc_issues(
    cv_id: int,
    user: RequireUserDep,
    db: DbSessionDep,
    body: HydrateIssuesRequest,
) -> dict:
    """Enqueue per-issue hydration for arc members the user just
    revealed via pagination.

    Mirrors ``/volume/{cv_id}/hydrate-issues`` but doesn't constrain
    issues to a single volume — arcs span many. We do verify the
    issue is referenced by this arc's payload (``raw_payload.issues``)
    so a caller can't sneak unrelated IDs through.

    Filter mirrors the volume endpoint: enqueue for true stubs
    (``fetched_at IS NULL``) and bulk-only rows missing arc credits.
    ``cv_id`` here is the arc, not an issue.
    """
    if not body.issue_cv_ids:
        return {"enqueued": 0}

    # Bound the request to issues this arc actually references. Look
    # up the arc's payload (cached row only — no CV fetch here; this
    # endpoint sits in a hot loop and we already fetched on page
    # load) and intersect against the request body.
    arc = await db.get(CvStoryArc, cv_id)
    if arc is None or not isinstance(arc.raw_payload, dict):
        return {"enqueued": 0}
    member_ids = {
        int(m["id"])
        for m in (arc.raw_payload.get("issues") or [])
        if isinstance(m, dict) and m.get("id") is not None
    }
    candidate_ids = [i for i in body.issue_cv_ids if i in member_ids]
    if not candidate_ids:
        return {"enqueued": 0}

    # "Needs hydration" condition matches the volume endpoint —
    # stubs (``fetched_at IS NULL``) plus bulk-only rows
    # (``_bulk_hydrated`` flag set). Issues with no ``cv_issues``
    # row at all are enqueued unconditionally below.
    needs_fetch_stmt = select(CvIssue.cv_id).where(
        CvIssue.cv_id.in_(candidate_ids),
        or_(
            CvIssue.fetched_at.is_(None),
            CvIssue.raw_payload["_bulk_hydrated"].astext == "true",
        ),
    )
    db_ids = set((await db.execute(needs_fetch_stmt)).scalars())
    existing_stmt = select(CvIssue.cv_id).where(CvIssue.cv_id.in_(candidate_ids))
    existing_ids = set((await db.execute(existing_stmt)).scalars())
    # Members in the arc payload that have no ``cv_issues`` row at
    # all — the volume hasn't been registered yet. ``enqueue_revalidate``
    # for ``issue`` will create the row + a stub volume as a side
    # effect of the per-issue fetch.
    missing_ids = set(candidate_ids) - existing_ids
    target_ids = db_ids | missing_ids
    for issue_cv_id in target_ids:
        enqueue_revalidate("issue", issue_cv_id)
    return {"enqueued": len(target_ids)}


@router.get("/arc/{cv_id}/issues/hydration")
async def arc_issues_hydration(
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    ids: Annotated[str, Query()] = "",
):
    """Poll endpoint feeding the arc page's ``setupAutoRefresh``.

    Client sends the issue cv_ids it's still waiting on; the response
    contains rendered HTML for any whose ``cv_issues`` row is now at
    least bulk-hydrated (covers + names + cover_dates). Re-runs
    ``get_arc_detail`` so the swapped-in rows carry the same volume-
    context enrichment as the initial page render.

    Each hydrated issue produces TWO swaps — one for the list view's
    ``<tr id="arc-issue-row-N">`` and one for the gallery view's
    ``<a id="arc-gallery-N">`` — because both views are in the DOM
    simultaneously under ``x-show`` toggles, and we want each to
    pick up the new cover the moment it lands.
    """
    issue_ids = parse_id_csv(ids)
    if not issue_ids:
        return JSONResponse({"swaps": [], "completed_ids": []})

    async with cv_cache_ctx() as cache:
        detail = await get_arc_detail(db, cache, cv_id)
    if detail is None:
        return JSONResponse({"swaps": [], "completed_ids": []})

    wanted = set(issue_ids)
    arc_issue_row_macro = templates.env.get_template("_arc_macros.html").module.arc_issue_row
    issue_cover_card_macro = templates.env.get_template("_issue_card.html").module.issue_cover_card

    swaps = []
    completed_ids = []
    for i in detail.issues:
        if i.cv_id in wanted and i.is_hydrated:
            swaps.append(
                {
                    "target_id": f"arc-issue-row-{i.cv_id}",
                    "html": str(arc_issue_row_macro(i)),
                }
            )
            swaps.append(
                {
                    "target_id": f"arc-gallery-{i.cv_id}",
                    "html": str(issue_cover_card_macro(i, dom_id=f"arc-gallery-{i.cv_id}")),
                }
            )
            completed_ids.append(i.cv_id)

    return JSONResponse(
        {
            "swaps": swaps,
            "completed_ids": completed_ids,
        }
    )


# ---- /publisher/{id} -------------------------------------------------


def _enqueue_arc_hydration(detail) -> None:
    """Background-enqueue ``revalidate-cv-entity`` jobs for any arc
    in the current window that isn't hydrated locally yet.

    Fire-and-forget — covers populate on the next page refresh as
    the worker drains. Same shape `/library` uses to upgrade stub
    volumes during browse."""
    for arc in detail.arcs:
        if not arc.is_hydrated:
            enqueue_revalidate("story_arc", arc.cv_id)


def _normalize_letter(letter: str | None) -> str | None:
    """Validate and normalize the ``?letter=`` query param used by the
    alphabet-bar filters.

    Accepts a single ASCII letter (case-insensitive — returns the
    uppercase form) or ``"#"`` (the non-alphabetic bucket — covers
    titles like ``"100 Bullets"`` or ``"#GamerLife"``). Returns
    ``None`` for empty / malformed values so a junked-up URL just
    silently drops the filter rather than 422'ing."""
    if not letter:
        return None
    cand = letter.strip().upper()
    if cand == "#" or (len(cand) == 1 and "A" <= cand <= "Z"):
        return cand
    return None


@router.get("/character/{cv_id}")
async def character_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    letter: Annotated[str | None, Query()] = None,
    fpage: Annotated[int, Query(ge=1)] = 1,
    epage: Annotated[int, Query(ge=1)] = 1,
    tpage: Annotated[int, Query(ge=1)] = 1,
):
    """A ComicVine character — the volumes it appears in, as a
    paginated card grid with an alphabet filter.

    The volume list is scraped from CV's ``issues-cover`` page (the
    JSON API's per-character volume data is unreliable). The first
    visit to a never-scraped character enqueues that scrape in the
    background; the page shows a "building" state until it lands.

    ``fpage`` / ``epage`` / ``tpage`` are the pages of the "Friends" /
    "Enemies" / "Teams" tabs (each its own pager)."""
    page_size = await get_page_size(db)
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_character_detail(
                db,
                cache,
                cv_id,
                page=page,
                page_size=page_size,
                letter=_normalize_letter(letter),
                friends_page=fpage,
                enemies_page=epage,
                teams_page=tpage,
            )
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="character")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="character",
            cv_id=cv_id,
        )

    publisher = await _publisher_for(db, detail.publisher_cv_id)
    # The character's volume list hasn't been scraped yet — enqueue the
    # background issues-cover scrape (the RQ job id coalesces repeat
    # visits onto one walk).
    if detail.volumes_scraping:
        enqueue_character_volumes_scrape(
            cv_id,
            site_url=(detail.character.raw_payload or {}).get("site_detail_url"),
        )
    # Hydrate the volume cards shown on this page so their cover / year
    # / format fill in on a later render.
    for credit in detail.appearance_volumes:
        if not credit.is_hydrated:
            enqueue_revalidate("volume", credit.cv_id)
    # The "First appearance" row shows a cover thumbnail — walk that
    # issue so its cover fills in on the next render.
    first_appearance = detail.info.first_appearance
    if first_appearance is not None and not first_appearance.is_hydrated:
        enqueue_revalidate("issue", first_appearance.cv_id)
    # Hydrate the friends / enemies shown on this page of those tabs
    # so their avatars fill in on a later render.
    for person in [*detail.friends, *detail.enemies]:
        if not person.is_hydrated:
            enqueue_revalidate("character", person.cv_id)
    # Teams hydrate as the "team" entity (their own cache table).
    for team in detail.teams:
        if not team.is_hydrated:
            enqueue_revalidate("team", team.cv_id)

    return templates.TemplateResponse(
        request,
        "character.html",
        {
            "user": user,
            "detail": detail,
            "publisher": publisher,
            # Widen the entity-page sidebar to fit the info card.
            "sidebar_width": "18rem",
        },
    )


@router.post("/character/{cv_id}/rescrape")
async def character_rescrape(
    user: RequireAdminDep,
    db: DbSessionDep,
    cv_id: int,
):
    """Reset the volume-scrape state for a character and re-enqueue.

    The Appearances tab reads ``cv_character_volumes``, which is
    populated by scraping CV's character "issues-cover" web page. The
    scrape can come back empty even when CV's API credits the
    character in many issues — a page-layout change, a parser miss,
    or a character whose appearances aren't in the gallery view. The
    page exposes a "re-run scrape" button in that mismatch state;
    this route is what it posts to.

    Admin-only because the scrape is a real CV-side cost (one HTTP
    fetch per gallery page) and shouldn't be triggerable by every
    page viewer. Clears ``volumes_scraped_at`` so the page falls
    back into its "building" state on the next render, then enqueues
    the scrape. The job's deterministic id dedupes a refresh-spam
    into a single walk.
    """
    character = await db.get(CvCharacter, cv_id)
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cached character with cv_id {cv_id}.",
        )
    character.volumes_scraped_at = None
    await db.commit()
    enqueue_character_volumes_scrape(
        cv_id,
        site_url=(character.raw_payload or {}).get("site_detail_url"),
    )
    return RedirectResponse(
        url=f"/character/{cv_id}?rescrape=queued",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/creator/{cv_id}")
async def creator_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    letter: Annotated[str | None, Query()] = None,
    cpage: Annotated[int, Query(ge=1)] = 1,
    cletter: Annotated[str | None, Query()] = None,
    apage: Annotated[int, Query(ge=1)] = 1,
    aletter: Annotated[str | None, Query()] = None,
):
    """A ComicVine creator/person — three tabs: the volumes they are
    credited on, the characters they created, and the story arcs they
    are credited on. Each tab is sorted by name with its own paging
    and alphabet-bar filter.

    ``cpage`` / ``cletter`` and ``apage`` / ``aletter`` are the
    "Created characters" / "Story arcs" tab page + letter filter."""
    page_size = await get_page_size(db)
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_creator_detail(
                db,
                cache,
                cv_id,
                page=page,
                page_size=page_size,
                letter=_normalize_letter(letter),
                characters_page=cpage,
                characters_letter=_normalize_letter(cletter),
                arcs_page=apage,
                arcs_letter=_normalize_letter(aletter),
            )
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="creator")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="creator",
            cv_id=cv_id,
        )

    # Hydrate the volumes shown on this page so their cover thumbnails
    # (and year / format) fill in on a later render.
    for credit in detail.volume_credits:
        if not credit.is_hydrated:
            enqueue_revalidate("volume", credit.cv_id)
    # The "Created characters" / "Story arcs" tab windows hydrate as
    # their own CV entity types so the avatars fill in later.
    for character in detail.created_characters:
        if not character.is_hydrated:
            enqueue_revalidate("character", character.cv_id)
    for arc in detail.story_arcs:
        if not arc.is_hydrated:
            enqueue_revalidate("story_arc", arc.cv_id)

    return templates.TemplateResponse(
        request,
        "creator.html",
        {"user": user, "detail": detail},
    )


@router.get("/team/{cv_id}")
async def team_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    fpage: Annotated[int, Query(ge=1)] = 1,
    epage: Annotated[int, Query(ge=1)] = 1,
    vpage: Annotated[int, Query(ge=1)] = 1,
    vletter: Annotated[str | None, Query()] = None,
    apage: Annotated[int, Query(ge=1)] = 1,
    aletter: Annotated[str | None, Query()] = None,
):
    """A ComicVine team — its members, paginated as avatar cards.

    ``fpage`` / ``epage`` are the pages of the "Friends" / "Enemies"
    tabs (each its own pager); ``vpage`` / ``vletter`` page and
    alphabet-filter the "Volumes" tab; ``apage`` / ``aletter`` do the
    same for the "Story arcs" tab."""
    page_size = await get_page_size(db)
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_team_detail(
                db,
                cache,
                cv_id,
                page=page,
                page_size=page_size,
                friends_page=fpage,
                enemies_page=epage,
                volumes_page=vpage,
                volumes_letter=_normalize_letter(vletter),
                arcs_page=apage,
                arcs_letter=_normalize_letter(aletter),
            )
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="team")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="team",
            cv_id=cv_id,
        )

    publisher = await _publisher_for(db, detail.publisher_cv_id)
    # Hydrate the members / friends / enemies shown on this page so
    # their avatars fill in on a later render — all are characters.
    for person in [*detail.members, *detail.friends, *detail.enemies]:
        if not person.is_hydrated:
            enqueue_revalidate("character", person.cv_id)
    # Hydrate the credited volumes shown on this page so their cover
    # thumbnails (and year / format) fill in on a later render.
    for credit in detail.volumes:
        if not credit.is_hydrated:
            enqueue_revalidate("volume", credit.cv_id)
    # Story arcs hydrate as their own CV entity type so the avatars
    # fill in later.
    for arc in detail.story_arcs:
        if not arc.is_hydrated:
            enqueue_revalidate("story_arc", arc.cv_id)
    # The "First appearance" row shows a cover thumbnail — walk that
    # issue so its cover fills in on the next render.
    first_appearance = detail.info.first_appearance
    if first_appearance is not None and not first_appearance.is_hydrated:
        enqueue_revalidate("issue", first_appearance.cv_id)

    return templates.TemplateResponse(
        request,
        "team.html",
        {
            "user": user,
            "detail": detail,
            "publisher": publisher,
            # Widen the entity-page sidebar to fit the info card.
            "sidebar_width": "18rem",
        },
    )


@router.get("/publisher/{cv_id}")
async def publisher_page(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    q: Annotated[str | None, Query()] = None,
):
    """Publisher page — identity card + library CTA + story-arc index.

    Deliberately doesn't list / paginate this publisher's volumes:
    for Marvel or DC that's hundreds of thousands of CV entries,
    most of which aren't in the user's library. The library CTA
    bounces back to ``/library`` pre-filtered by this publisher,
    which IS scoped to their files.

    Story arcs DO render here (CV bundles a manageable list per
    publisher), paginated 30 at a time via infinite scroll. Each
    page-load enqueues background hydration for any unhydrated arcs
    in the visible window so covers fill in on refresh.

    ``q`` is an optional name filter — case-insensitive substring
    match against each arc's parsed name + parent book. Works
    against arc STUBS (every arc in the publisher's CV payload),
    so the filter applies to the full set even when many entries
    haven't been individually hydrated yet.
    """
    q_norm = (q or "").strip() or None
    page_size = await get_page_size(db)
    try:
        async with cv_cache_ctx() as cache:
            detail = await get_publisher_detail(
                db,
                cache,
                cv_id,
                arcs_limit=page_size,
                arcs_offset=0,
                arcs_query=q_norm,
            )
    except ComicVineError as err:
        return _cv_error_response(request, user, err, entity_label="publisher")
    if detail is None:
        return _entity_not_found_response(
            request,
            user,
            entity_label="publisher",
            cv_id=cv_id,
        )
    _enqueue_arc_hydration(detail)
    return templates.TemplateResponse(
        request,
        "publisher.html",
        {
            "user": user,
            "detail": detail,
            "page_size": page_size,
            "q": q_norm or "",
        },
    )


@router.get("/publisher/{cv_id}/arcs/fragment")
async def publisher_arcs_fragment(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    offset: Annotated[int, Query(ge=0)] = 0,
    view: Annotated[Literal["list", "gallery"], Query()] = "list",
    q: Annotated[str | None, Query()] = None,
):
    """Next-page fragment for the publisher page's infinite scroll.

    Returns inner-only HTML (table rows for ``list``, grid items
    for ``gallery``) — the publisher page's Alpine handler appends
    the response into the existing tbody / grid. Empty response
    when there's nothing more to fetch.

    ``q`` is forwarded through to ``get_publisher_detail`` so that
    pagination on a filtered list keeps paging through the same
    filtered set — the JS appends ``q`` to the fragment URL whenever
    the user navigates to ``/publisher/{id}?q=...``.
    """
    q_norm = (q or "").strip() or None
    page_size = await get_page_size(db)
    async with cv_cache_ctx() as cache:
        detail = await get_publisher_detail(
            db,
            cache,
            cv_id,
            arcs_limit=page_size,
            arcs_offset=offset,
            arcs_query=q_norm,
        )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Publisher {cv_id} not found.",
        )
    _enqueue_arc_hydration(detail)
    template_name = (
        "_publisher_arcs_list_rows.html" if view == "list" else "_publisher_arcs_gallery_items.html"
    )
    return templates.TemplateResponse(
        request,
        template_name,
        {"arcs": detail.arcs},
    )


@router.get("/publisher/{cv_id}/arcs/hydration")
async def publisher_arcs_hydration(
    user: RequireUserDep,
    db: DbSessionDep,
    cv_id: int,
    ids: Annotated[str, Query()] = "",
):
    """Poll endpoint: returns rendered HTML for arc IDs that have
    become hydrated since the client last asked.

    The client sends the IDs it's still waiting on; the response
    contains ``hydrated[]`` entries with both list and gallery
    markup so the client can swap the matching DOM nodes in either
    view (both copies of an arc render in the page under x-show).
    IDs that aren't hydrated yet are silently absent — client
    leaves them in its pending set and polls again.

    ``cv_id`` is the publisher, used only to scope the endpoint;
    the actual lookup is by arc ID against ``cv_story_arcs``.
    """
    arc_ids = parse_id_csv(ids)
    if not arc_ids:
        return JSONResponse({"hydrated": []})

    rows = await get_hydrated_arc_rows(db, arc_ids)

    # Render each row through the shared macros so the polled
    # markup matches the initial-render markup byte-for-byte (same
    # IDs, same classes, same data attributes). ``tpl.module``
    # gives us the macros as importable callables after a single
    # template load. Then flatten into the generic
    # ``{swaps, completed_ids}`` shape that ``setupAutoRefresh``
    # consumes — two swaps per row (list + gallery views).
    tpl = templates.env.get_template("_publisher_arc_card.html")
    list_row = tpl.module.list_row
    gallery_card = tpl.module.gallery_card

    swaps = []
    for row in rows:
        swaps.append(
            {
                "target_id": f"list-arc-{row.cv_id}",
                "html": str(list_row(row)),
            }
        )
        swaps.append(
            {
                "target_id": f"gallery-arc-{row.cv_id}",
                "html": str(gallery_card(row)),
            }
        )
    return JSONResponse(
        {
            "swaps": swaps,
            "completed_ids": [row.cv_id for row in rows],
        }
    )


# ---- Fix match (re-pick a wrong volume) ------------------------------
#
# When the matcher confirmed a group of files against the wrong CV
# volume (typically same series name, wrong publisher), the reviewer
# can recover from the volume page: click "Fix match" → CV-volume
# search → pick the correct volume → every owned file gets re-mapped
# to the new volume's issues by issue number.


@router.get("/volume/{old_cv_id}/fix-match")
async def volume_fix_match_search(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    old_cv_id: int,
    q: Annotated[str | None, Query()] = None,
):
    """Search ComicVine for the *correct* volume to re-map this
    library volume's files into. ``q`` is the editable query, seeded
    from the current (wrong) volume's name on first load so the
    common "same name, wrong publisher" case shows useful results
    immediately."""
    old_volume = await db.get(CvVolume, old_cv_id)
    if old_volume is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {old_cv_id} isn't in our cache.",
        )

    raw_query = q if q is not None else (old_volume.name or "")
    cleaned = clean_search_query(raw_query)

    results: list[dict] = []
    error: str | None = None
    if cleaned:
        client = ComicVineClient()
        try:
            cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
            envelope = await cache.search(
                db,
                cleaned,
                resources="volume",
                limit=50,
            )
            results = shape_volume_results(envelope)
        except ComicVineError as e:
            error = f"ComicVine search failed: {e}"
        finally:
            await client.aclose()

    if results:
        pub_map = await publishers_for_volumes(db, {r["cv_id"] for r in results})
        for r in results:
            if not r["publisher"]:
                r["publisher"] = pub_map.get(r["cv_id"])

    return templates.TemplateResponse(
        request,
        "review_volume_search.html",
        {
            "user": user,
            "query": raw_query,
            "cleaned_query": cleaned,
            "results": results,
            "result_facets": result_facets(results),
            "error": error,
            # ``fix_match`` mode flag, picked up by the template
            # alongside ``file_mode`` to switch the breadcrumb,
            # reference card, search-form action, and per-result
            # link / button.
            "fix_match": True,
            "old_volume": old_volume,
        },
    )


@router.post("/volume/{old_cv_id}/fix-match")
async def volume_fix_match_execute(
    user: RequireAdminDep,
    db: DbSessionDep,
    old_cv_id: int,
    new_cv_id: Annotated[int, Form(ge=1)],
):
    """Commit the re-map: every file currently matched to an issue
    in ``old_cv_id`` gets re-pointed to the corresponding-by-number
    issue in ``new_cv_id``. The new volume is hydrated through the
    CV cache first so its issues are present (the matching pass
    needs them)."""
    client = ComicVineClient()
    try:
        cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
        await cache.get_volume(db, new_cv_id)
    except ComicVineNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {new_cv_id} doesn't exist on ComicVine.",
        ) from e
    except ComicVineRateLimitError as e:
        wait_s = int(e.retry_after or 60)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"ComicVine is rate-limiting us right now — can't "
                f"fetch the new volume. Try again in about {wait_s}s."
            ),
        ) from e
    except ComicVineError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Couldn't load volume {new_cv_id} from ComicVine: {e}",
        ) from e
    finally:
        await client.aclose()

    result = await execute_fix_match(
        db,
        old_volume_cv_id=old_cv_id,
        new_volume_cv_id=new_cv_id,
        matched_by_user_id=user.id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {new_cv_id} isn't in our cache; can't fix-match.",
        )
    return RedirectResponse(
        url=(
            f"/volume/{new_cv_id}"
            f"?fix_matched={result.rematched_count}"
            f"&fix_skipped={result.skipped_count}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )
