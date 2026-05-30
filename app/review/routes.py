"""Review-queue routes mounted under ``/review``.

Admin-only — match data is shared across the library, so confirming
or rejecting a match affects what every viewer sees on the library /
volume / arc pages.

The queue page (this file) and the per-file review page (a separate
file once we build it) both consume ``app.services.review``. Route
handlers stay thin: parse query params, call the service, render a
template.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.archives import open_archive
from app.archives.base import (
    ArchiveError,
    UnsupportedArchiveError,
    resolve_cover_page_name,
)
from app.archives.cover_image import CoverInspection, crop_to_front, inspect_cover
from app.auth.dependencies import DbSessionDep, RequireAdminDep, RequireUserDep
from app.comicvine import ComicVineCache, ComicVineClient
from app.comicvine.errors import (
    ComicVineError,
    ComicVineKeyInvalidError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)
from app.jobs.queue_status import get_job_position

# Review-page enqueues route to the ``interactive`` lane so they
# don't queue behind the match backlog on ``default``. Import-rename
# trick keeps the existing call sites and the cache callback wiring
# untouched; see ``app/library_browse/routes.py`` for the same
# pattern. The match-side path (``app/jobs/match_file.py``) still
# imports the bare ``enqueue_revalidate`` so its enqueues stay on
# the match queue.
from app.jobs.revalidate import (
    enqueue_revalidate_interactive as enqueue_revalidate,
)
from app.jobs.revalidate import (
    rescheduled_retry_after,
)
from app.models import CvIssue, CvVolume, File, FileErrorKind, FileLocation
from app.services.cv_helpers import cv_image_url, parse_id_csv, safe_int
from app.services.cv_search import (
    clean_search_query,
    publishers_for_volumes,
    result_facets,
    shape_volume_results,
)
from app.services.file_errors import (
    clear_error_for_path_and_kind,
    record_error,
)
from app.services.local import (
    SUPPLEMENT_TYPE_LABELS,
    SUPPLEMENT_TYPES,
    attach_local_group,
    attach_supplement,
    create_local_entry,
    create_local_group,
    list_local_volumes,
    preview_local_group,
)
from app.services.review import (
    confirm_file_match,
    exclude_files_by_series,
    execute_bulk_confirm,
    execute_volume_confirm,
    get_file_review,
    get_group_reference,
    list_pending_groups,
    list_volume_issues,
    preview_volume_confirm,
    reject_file_match,
)
from app.services.settings import get_archive_backend, get_page_size
from app.templates_env import static_url, templates

logger = logging.getLogger("longboxes.review")

router = APIRouter(prefix="/review", tags=["review"])


# ---- Helpers -----------------------------------------------------------


def _parse_confidence(raw: str | None) -> float | None:
    """Coerce a query-string confidence value (``0.5``, ``50%``,
    junk) to a float in ``[0, 1]`` or None.

    Defensive: bogus values become None rather than 400-ing, so a
    stale bookmark with a typo'd filter still shows the queue with
    the filter dropped instead of failing outright."""
    if raw is None:
        return None
    raw = raw.strip().rstrip("%")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    # Allow "50" as shorthand for 0.50 — admins typing into a URL bar
    # often skip the decimal.
    if value > 1:
        value = value / 100.0
    if value < 0 or value > 1:
        return None
    return value


# ---- Routes ------------------------------------------------------------


@router.get("")
async def review_queue(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    min_confidence: Annotated[str | None, Query()] = None,
    max_confidence: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=0)] = 0,
    confirmed: Annotated[int | None, Query(ge=0)] = None,
    skipped: Annotated[int | None, Query(ge=0)] = None,
    group_count: Annotated[int | None, Query(ge=0)] = None,
    file_confirmed: Annotated[int | None, Query(ge=0)] = None,
    file_rejected: Annotated[int | None, Query(ge=0)] = None,
    file_local: Annotated[int | None, Query(ge=0)] = None,
    file_supplement: Annotated[int | None, Query(ge=0)] = None,
    file_excluded: Annotated[int | None, Query(ge=0)] = None,
):
    """Grouped queue of files awaiting human review.

    Files are bucketed by their parsed series — typically a group is
    "all the Avengers files the matcher couldn't pin to a specific
    volume." Cards show the file count + year range + aggregated
    volume suggestions across the group, with expand-to-list for
    drilling into individual rows. Each group's "Review N files"
    CTA opens the volume-confirm page; ticking group checkboxes and
    using the bulk bar confirms several groups at once, each against
    its top suggested volume.

    Confidence filters still apply — files outside the band are
    dropped before grouping, so a group whose only members fall
    out of the filter disappears entirely. URL-typing convenience:
    ``min_confidence`` / ``max_confidence`` accept either decimal
    (``0.7``) or percent (``70`` or ``70%``).
    """
    min_c = _parse_confidence(min_confidence)
    max_c = _parse_confidence(max_confidence)

    all_groups, total_files_seen, hit_row_cap = await list_pending_groups(
        db,
        min_confidence=min_c,
        max_confidence=max_c,
    )

    # Slice the sorted full group list into a single page. The service
    # returns groups sorted by file_count desc, so page 0 is always the
    # biggest groups. Honour the admin-configured page size (same
    # setting other paginated views use). Clamp ``page`` so a hand-typed
    # out-of-range value snaps to the last available page rather than
    # rendering blank.
    page_size = await get_page_size(db)
    total_groups = len(all_groups)
    total_pages = max(1, (total_groups + page_size - 1) // page_size)
    page = min(page, total_pages - 1) if total_groups > 0 else 0
    groups = all_groups[page * page_size : (page + 1) * page_size]

    # Note: we deliberately do NOT pre-warm cv_issues here on page load.
    # The previous design enqueued one ``issue`` revalidate per top
    # candidate (~500 jobs per page load) to populate cover URLs. That
    # work is redundant with the ``volume_issues`` bulk-hydration job
    # already enqueued when each candidate volume was first registered:
    # the bulk endpoint returns ``image`` along with everything the
    # queue card needs (number, name, cover_date), at ~1 CV call per
    # 100 issues instead of one per issue. ``enqueue_revalidate`` now
    # uses ``at_front=True`` for ``volume_issues`` so those cheap
    # hydrations don't strand behind a long match backlog.

    return templates.TemplateResponse(
        request,
        "review_queue.html",
        {
            "user": user,
            "groups": groups,
            "total_files_seen": total_files_seen,
            "hit_row_cap": hit_row_cap,
            # Pagination context — passed straight to the template's
            # summary line and prev/next controls.
            "page": page,
            "page_size": page_size,
            "total_groups": total_groups,
            "total_pages": total_pages,
            # Echo the raw query-param strings back into the form so
            # the filter inputs keep what the user typed (including
            # percent shorthand).
            "min_confidence_raw": min_confidence or "",
            "max_confidence_raw": max_confidence or "",
            # Confirm result banner. Both the volume-confirm POST and
            # the queue's bulk-confirm POST redirect back here with
            # these counts so we can show how many rows actually
            # moved (which can differ from a preview if something
            # raced). ``group_count`` is set only by the bulk action.
            "confirmed": confirmed,
            "skipped": skipped,
            "group_count": group_count,
            # Single-file review result banner — the per-file
            # confirm / reject / local-entry routes redirect back with
            # one of these flags set.
            "file_confirmed": file_confirmed,
            "file_rejected": file_rejected,
            "file_local": file_local,
            "file_supplement": file_supplement,
            "file_excluded": file_excluded,
        },
    )


# ---- File cover endpoint ----------------------------------------------
#
# Streams the cover page of an archive so the review queue (and,
# eventually, the per-file review page + the Phase 6 reader) can show
# the file's actual cover side-by-side with candidate metadata.
# Lives under ``/review/`` for now because access is admin-only; when
# the reader ships it'll likely move under ``/file/`` and gate on the
# regular user role.


# By-extension MIME map. Comic archives are overwhelmingly JPEG; the
# others are real but rare. Anything we don't recognise we default to
# ``image/jpeg`` rather than ``application/octet-stream`` so the
# browser at least tries to render it — image bytes that fail are no
# worse than an opaque download dialog.
_COVER_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
}


@lru_cache(maxsize=256)
def _extract_cover_cached(path: str, backend: str) -> tuple[bytes, str, CoverInspection | None]:
    """Open the archive at ``path`` and return ``(image_bytes,
    content_type, inspection)`` for its cover page.

    Cached because the review queue and the per-file review page
    both hit this endpoint repeatedly for the same files. Cache key
    is ``(path, backend)``; if the file at the path changes the
    cache won't see it without a worker restart. Acceptable for
    Phase 7 — admins reviewing matches don't typically swap files
    mid-session, and reordering the page invalidates the offset
    anyway.

    Honours the archive's ComicInfo ``<Page Type="FrontCover">`` hint
    via ``ComicboxReader.cover_filename()`` when the comicbox backend
    is in use; falls back to ``list_pages()[0]`` otherwise.

    A double-wide / wraparound cover (one image spanning back + front)
    is cropped to its front cover — the right half — before returning,
    so the 2:3 portrait cover cards don't render a sliver of the spine.
    The returned ``CoverInspection`` carries the *original* (pre-crop)
    geometry so the caller can persist it.
    """
    reader = open_archive(Path(path), backend=backend)
    cover_name = resolve_cover_page_name(reader)
    if cover_name is None:
        raise ArchiveError(f"no pages in archive: {path}")

    data = reader.extract_page(cover_name)
    ext = Path(cover_name).suffix.lower()
    content_type = _COVER_CONTENT_TYPES.get(ext, "image/jpeg")

    inspection = inspect_cover(data)
    # ``crop_to_front`` returns the front-cover JPEG for a wraparound
    # and None for a normal single cover — serve those bytes untouched.
    cropped = crop_to_front(data)
    if cropped is not None:
        data, content_type = cropped, "image/jpeg"
    return data, content_type, inspection


def _placeholder_cover_response() -> RedirectResponse:
    """Redirect to the ``cover-unavailable.svg`` placeholder.

    Used in both failure branches of ``file_cover`` — "no current
    location for this file" and "extraction blew up." Browsers cache
    the redirect for ``max-age=60`` so a queue scroll doesn't re-hit
    this endpoint for every failing file in the visible window; that
    floor is tight enough that a later fix surfaces on the next page
    load. We deliberately stay at 302 (not 301) since the URL might
    serve a real cover again once the underlying file is repaired.

    ``static_url`` adds the cache-busting hash so the placeholder
    itself is also browser-cacheable per the standard static-asset
    pipeline.
    """
    return RedirectResponse(
        url=static_url("cover-unavailable.svg"),
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get("/file/{file_id}/cover")
async def file_cover(
    user: RequireUserDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
):
    """Return the cover page of the file as a raw image response.

    The review queue's left-hand thumbnails point at this, and from
    Phase 11C so do local volume/issue pages — a local issue has no CV
    image, its cover *is* the matched file's first page. So this is
    ``RequireUserDep``, not admin-only: any authenticated library
    visitor can load a local cover. Resolves the file's current on-disk
    location (prefer non-missing rows), fetches the archive backend
    setting, and streams the cover.

    On failure (file missing, corrupt archive, unsupported format), we
    redirect to a static placeholder SVG instead of returning an HTTP
    error. That way the browser renders a "cover unavailable" panel in
    place of the default broken-image icon, which tells the operator
    something useful at a glance (the file may need attention) while
    keeping the page chrome intact.
    """
    stmt = (
        select(FileLocation)
        .where(FileLocation.file_id == file_id)
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.last_seen_at.desc())
        .limit(1)
    )
    location = (await db.execute(stmt)).scalar_one_or_none()
    if location is None:
        logger.warning("file_cover: no current location for file_id=%s", file_id)
        return _placeholder_cover_response()

    backend = await get_archive_backend(db)
    try:
        # Sync archive work goes through ``to_thread`` so the event
        # loop stays free while ``unar`` / pymupdf does its thing.
        data, content_type, inspection = await asyncio.to_thread(
            _extract_cover_cached, location.path, backend
        )
    except (ArchiveError, UnsupportedArchiveError, OSError) as e:
        logger.warning("file_cover failed for %s: %s", location.path, e)
        # Persist the failure so /admin/file-errors can surface it.
        # The placeholder fallback alone leaves the operator with no
        # signal that this file is broken; record_error gives them a
        # clickable row with the path + retry button.
        await record_error(
            db,
            path=location.path,
            kind=FileErrorKind.COVER_EXTRACTION,
            exc=e,
            file_id=file_id,
        )
        return _placeholder_cover_response()

    # Success path — clear any stale COVER_EXTRACTION row for this
    # path before the lazy-backfill below. The scanner does the same
    # in bulk on a re-scan; this catches the case where the operator
    # repairs the archive and opens the review page before the next
    # scan runs.
    await clear_error_for_path_and_kind(db, location.path, FileErrorKind.COVER_EXTRACTION)

    # Lazy-backfill cover geometry for files scanned before cover
    # inspection existed — the scanner fills this eagerly for new
    # files; this catches the long tail whose covers get viewed here.
    # One UPDATE on first view; null-guarded so it never clobbers
    # scanner data.
    if inspection is not None:
        file_row = await db.get(File, file_id)
        if file_row is not None and file_row.cover_width is None:
            file_row.cover_width = inspection.width
            file_row.cover_height = inspection.height
            file_row.cover_is_wraparound = inspection.is_wraparound
            await db.commit()

    # ``Cache-Control: private`` keeps shared caches out of the loop
    # (per-user library data) but lets the browser memoise within the
    # session. ``max-age=3600`` is generous enough that a queue scroll
    # doesn't refetch on every paint; covers don't change often enough
    # to need a tighter window.
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ---- Volume confirm: pick a volume, auto-match by issue_number -------
#
# The unique value of the review surface: in our experience most
# PENDING rows are volume-disambiguation failures (multiple "Avengers"
# volumes; the matcher's name + year similarity couldn't pick the
# right one). Once a human says "these are all the 2018 Avengers
# run," the issue numbers map themselves. This pair of endpoints
# implements that workflow for ONE group — preview the file-to-issue
# mapping, then commit it in one transaction. The queue's bulk action
# (further down) drives this same machinery across several groups.


@router.get("/volume-confirm")
async def volume_confirm_preview(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[str, Query()],
    volume: Annotated[int, Query(ge=1)],
):
    """Preview a volume-confirm: every PENDING file in the series-group
    paired with the issue in the chosen volume whose number matches.

    Renders a side-by-side table. Files for which the volume has no
    matching issue surface as skip rows (will stay PENDING on commit);
    files that map cleanly become confirm rows. The summary banner at
    the top shows the totals so the reviewer can sanity-check before
    they pull the trigger.

    The chosen volume is hydrated through the CV cache first. A
    volume picked from the manual ``/review/volume-search`` page
    won't be in our local cache — that's the whole point of
    searching CV for one the matcher never considered — and the
    fetch also writes stub ``cv_issues`` rows (each carrying its
    ``issue_number``), which the issue-number mapping below needs.
    Already-cached volumes (a candidate clicked from the queue)
    short-circuit on the fresh cache with no CV round trip."""
    client = ComicVineClient()
    try:
        cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
        await cache.get_volume(db, volume)
    except ComicVineNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {volume} doesn't exist on ComicVine.",
        ) from e
    except ComicVineError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Couldn't load volume {volume} from ComicVine: {e}",
        ) from e
    finally:
        await client.aclose()

    # Defensive bulk-hydration nudge. The matcher enqueues a
    # ``volume_issues`` job when a volume is first registered, but
    # this page load can arrive before that job runs — or for a
    # volume whose original job was enqueued before ``at_front=True``
    # was in effect (and is therefore stuck behind a large match
    # backlog). When any issue row for this volume is still a pure
    # stub (``fetched_at IS NULL``), re-enqueue. ``enqueue_revalidate``
    # promotes ``volume_issues`` to the head of the queue, so the
    # next reload (typically <30s later) shows real per-issue covers
    # instead of the empty-placeholder boxes. Guarded so a
    # fully-hydrated volume isn't nudged on every page load.
    has_unhydrated = (
        await db.execute(
            select(CvIssue.cv_id)
            .where(CvIssue.volume_cv_id == volume)
            .where(CvIssue.fetched_at.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    if has_unhydrated:
        enqueue_revalidate("volume_issues", volume)

    preview = await preview_volume_confirm(
        db,
        series_key=series,
        volume_cv_id=volume,
    )
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {volume} isn't in the cache; can't preview.",
        )

    return templates.TemplateResponse(
        request,
        "review_volume_confirm.html",
        {
            "user": user,
            "preview": preview,
        },
    )


@router.post("/volume-confirm")
async def volume_confirm_submit(
    user: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[str, Form()],
    volume: Annotated[int, Form(ge=1)],
    file_ids: Annotated[list[uuid.UUID] | None, Form()] = None,
):
    """Commit a volume-confirm. POST-only so the action isn't
    triggered by an accidental URL prefetch or bot crawl.

    ``file_ids`` is the set of still-checked rows from the
    volume-confirm page's per-file checkboxes — only those get
    confirmed, so a reviewer can exclude a mis-mapped file. The
    browser omits unchecked boxes, so an all-unchecked submission
    arrives with no ``file_ids`` at all (None here) and confirms
    nothing.

    Redirects back to ``/review`` with banner query params so the
    queue shows a success message. The actual write counts come
    from ``execute_volume_confirm`` — they can differ from the
    preview when something raced (another tab confirmed a row,
    matcher re-ran and bumped status to AUTO, etc.)."""
    result = await execute_volume_confirm(
        db,
        series_key=series,
        volume_cv_id=volume,
        matched_by_user_id=user.id,
        included_file_ids=set(file_ids or []),
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {volume} isn't in the cache; can't confirm.",
        )
    return RedirectResponse(
        url=(f"/review?confirmed={result.confirmed_count}&skipped={result.skipped_count}"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/volume-confirm/{volume_cv_id}/covers")
async def volume_confirm_covers_hydration(
    user: RequireAdminDep,
    db: DbSessionDep,
    volume_cv_id: int,
    ids: Annotated[str, Query()] = "",
):
    """Poll endpoint feeding the Confirm Volume page's ``setupAutoRefresh``.

    Client sends the matched-issue cv_ids it's still waiting on; for
    each whose ``cv_issues.raw_payload`` now carries image data, we
    render the same ``matched_cover`` macro the initial page uses
    and return a swap fragment. The client's auto-refresh loop
    replaces the placeholder div with this fragment, so the cover
    fades into place as the volume's bulk-hydration job populates
    the rows.

    Mirrors ``/volume/{cv_id}/issues/hydration`` in shape — same
    ``{swaps: [...], completed_ids: [...]}`` response contract. No
    CV calls happen here; this only surfaces data already written by
    ``hydrate_volume_issues`` (or per-issue fetches).
    """
    # ``queue_status`` is computed even when ``ids`` is empty — the
    # client can poll with no pending IDs once to read state (e.g.
    # to learn the job is in the "scheduled" cooldown branch).
    queue_status = get_job_position("volume_issues", volume_cv_id).to_dict()

    issue_ids = parse_id_csv(ids)
    if not issue_ids:
        return {
            "swaps": [],
            "completed_ids": [],
            "queue_status": queue_status,
        }

    rows = (
        (
            await db.execute(
                select(CvIssue)
                .where(CvIssue.cv_id.in_(issue_ids))
                .where(CvIssue.volume_cv_id == volume_cv_id)
            )
        )
        .scalars()
        .all()
    )

    cover_macro = templates.env.get_template("_matched_cover.html").module.matched_cover

    swaps: list[dict] = []
    completed_ids: list[int] = []
    for row in rows:
        cover_url = cv_image_url(row.raw_payload, "thumb")
        if not cover_url:
            # Row exists but the bulk-hydration job hasn't filled in
            # the image yet — leave it pending, the client keeps
            # asking on its next tick.
            continue
        swaps.append(
            {
                "target_id": f"matched-cover-{row.cv_id}",
                "html": str(cover_macro(row.cv_id, cover_url)),
            }
        )
        completed_ids.append(row.cv_id)

    # Per-issue fallback for stuck covers. When the bulk ``volume_issues``
    # job has finished (or was never enqueued) but the client is still
    # asking about pending IDs that this query couldn't fill, the bulk
    # hydration didn't include image data for those specific issues —
    # CV's ``/issues/?filter=volume:N`` response is observed to omit the
    # ``image`` field for some nested rows. Bulk hydration can't fix what
    # it doesn't return, so enqueue a per-issue revalidate (full
    # ``/issue/N/`` GET) for each unfilled id.
    #
    # ``enqueue_revalidate`` dedupes on in-flight jobs (no flood) and
    # checks the rescheduled marker (cooldown), so successive polls
    # don't pile up duplicates. The bulk job is deliberately NOT
    # re-enqueued here — re-running it just re-hits the same CV
    # payload that didn't include the images the first time, and the
    # every-3s polling cadence would hammer the worker into a stuck-
    # state loop.
    uncompleted_ids = set(issue_ids) - set(completed_ids)
    if uncompleted_ids and queue_status["state"] in ("done", "missing"):
        for issue_cv_id in uncompleted_ids:
            # ``at_front=True``: the user is actively staring at this
            # stub cover, so a per-issue revalidate triggered from
            # here jumps the queue ahead of background match jobs.
            # Without this, on a fresh-scan library the per-issue
            # job lands at the tail of a 10k+ match backlog and the
            # toast reads "In queue, position 13504 of 13512" —
            # accurate but unusable.
            enqueue_revalidate("issue", issue_cv_id, at_front=True)
        # The bulk job's state ("done"/"missing") is stale information
        # from the toast's perspective now — the actual work in flight
        # is the per-issue revalidates we just enqueued (or that are
        # already in the cooldown queue from a previous tick). Sample
        # one of the uncompleted ids and surface its job state so the
        # toast shows "queued" / "running" / "rate-limit cooldown"
        # instead of falling through to the generic count message.
        sample_state = _per_issue_queue_state(next(iter(uncompleted_ids)))
        if sample_state["state"] != "missing":
            queue_status = sample_state

    return {
        "swaps": swaps,
        "completed_ids": completed_ids,
        "queue_status": queue_status,
    }


def _per_issue_queue_state(issue_cv_id: int) -> dict:
    """Resolve the queue state for a per-issue revalidate, including
    the rescheduled-retry cooldown when applicable.

    ``get_job_position`` reports ``done``/``missing`` for terminal
    or absent jobs — but our rate-limit reschedule path runs the
    retry under a NEW random RQ job_id (RQ overwrites the
    deterministic id's hash on worker exit, see
    ``app.jobs.revalidate._reschedule_revalidate``), so the
    deterministic-id lookup goes terminal even while a SCHEDULED
    retry is pending. The marker key from the reschedule path
    captures the cooldown; check it as a fallback so the toast can
    render the rate-limit state instead of going silent."""
    pos = get_job_position("issue", issue_cv_id)
    if pos.state != "missing":
        return pos.to_dict()
    # Deterministic-id job is gone — but a rescheduled retry might
    # still be pending under a different id. The marker key TTL is
    # the user-meaningful cooldown.
    retry = rescheduled_retry_after("issue", issue_cv_id)
    if retry is not None:
        return {
            "state": "scheduled",
            "position": None,
            "depth": None,
            "retry_after": retry,
        }
    return pos.to_dict()


# ---- Bulk confirm: several groups at once (the queue's bulk action) --
#
# The queue-level counterpart to volume-confirm. The reviewer ticks
# the series-groups whose top suggested volume looks right and commits
# them all in one POST — each group confirmed against its own #1
# aggregated volume suggestion, no per-file drill-in. The fast path
# for "the matcher got a run right but landed it in PENDING."


@router.post("/bulk-confirm")
async def bulk_confirm(
    user: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[list[str] | None, Form()] = None,
):
    """Commit the queue's bulk action. POST-only.

    ``series`` is the set of checked group keys from the queue —
    the parsed series string, or ``""`` for the unparsed bucket.
    Each group is confirmed against its top aggregated volume
    suggestion; groups with no candidate volume are skipped. No CV
    round trip: every target is a volume the matcher already
    considered (and therefore already hydrated) when it built the
    candidate list.

    Redirects back to ``/review`` with banner query params — the
    file counts plus ``groups``, how many groups actually committed.
    An empty submission (nothing checked) confirms nothing."""
    result = await execute_bulk_confirm(
        db,
        series_keys=series or [],
        matched_by_user_id=user.id,
    )
    return RedirectResponse(
        url=(
            f"/review?confirmed={result.confirmed_count}"
            f"&skipped={result.skipped_count}"
            f"&group_count={result.group_count}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- Manual volume search ---------------------------------------------
#
# Escape hatch for the volume-confirm page: when none of the matcher's
# candidate volumes is the right one, the reviewer searches ComicVine
# directly. Picking a result returns to /review/volume-confirm with that
# volume as the confirm target, folding straight back into the flow.


@router.get("/volume-search")
async def volume_search(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
):
    """Search ComicVine volumes by name, for manually picking a
    volume-confirm target.

    ``series`` is the parsed series-group key carried over from the
    volume-confirm page — it seeds the search box on first load and
    rides along on every result link so picking a volume can return
    to ``/review/volume-confirm?series=<series>&volume=<picked>``.

    ``q`` is the (editable) search-box value. When it's absent the
    page searches ``series`` directly, so arriving from the
    volume-confirm page's "Search for a volume" button shows results
    immediately.
    The query is stripped of punctuation before it hits CV's
    ``/search/`` endpoint."""
    raw_query = q if q is not None else (series or "")
    cleaned = clean_search_query(raw_query)

    # Reference card — the PENDING file group the reviewer came from,
    # so they can eyeball cover art / year while scanning CV results.
    reference = await get_group_reference(db, series_key=series)

    results: list[dict] = []
    error: str | None = None
    if cleaned:
        client = ComicVineClient()
        try:
            cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
            envelope = await cache.search(db, cleaned, resources="volume", limit=50)
            results = shape_volume_results(envelope)
        except ComicVineKeyMissingError:
            error = (
                "No ComicVine API key is configured. "
                "Set one in the admin settings before searching."
            )
        except ComicVineKeyInvalidError:
            error = "ComicVine rejected the API key. Re-paste it in the admin settings."
        except ComicVineError as e:
            error = f"ComicVine search failed: {e}"
        finally:
            await client.aclose()

    # Fill in any publisher the CV search payload didn't carry from
    # our local cache (matcher candidates, library volumes).
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
            "series": series or "",
            "query": raw_query,
            "cleaned_query": cleaned,
            "results": results,
            "result_facets": result_facets(results),
            "error": error,
            "reference": reference,
        },
    )


# ---- Single-file review -----------------------------------------------
#
# The per-file counterpart to the volume-confirm flow: review one
# file, then confirm it to a candidate, reject every candidate, or
# search CV by hand. Reached from the queue's per-row "Review" button.
# Confirm / reject redirect back to /review with a result banner.
#
# These routes carry a ``{file_id}`` path param, so they're registered
# AFTER every literal ``/review/...`` route above — FastAPI matches in
# registration order, so ``/review/volume-confirm`` etc. still win.


# ---- Add a whole group as a local volume (Phase 11D) ------------------
#
# The local-metadata counterpart of volume-confirm: when a review-queue
# series group's comic isn't in ComicVine at all, create one local
# volume and a local issue per file in a single action. A preview/edit
# page seeds the volume from the parsed series; the reviewer adjusts the
# volume fields and per-file issue numbers before committing.
#
# Registered BEFORE ``/{file_id}`` so the literal ``/local-group`` path
# wins over the file-id parameter route.


@router.get("/local-group")
async def local_group_form(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[str | None, Query()] = None,
):
    """Preview/edit page for cataloguing a whole series group as a local
    volume — either create a new one or attach the group to an existing
    local volume via the find-or-create picker. ``series`` is the group
    key (blank ⇒ the unparsed bucket). Redirects back to the queue if
    the group has drained."""
    series_key = series or None
    preview = await preview_local_group(db, series_key)
    if preview is None:
        return RedirectResponse(url="/review", status_code=status.HTTP_303_SEE_OTHER)
    # Existing local volumes for the find-or-create picker. The template
    # auto-opens in "attaching" mode when one matches the seeded volume
    # name exactly (case-insensitive), so the common "next batch of an
    # ongoing local series" flow needs no typing.
    local_volumes = await list_local_volumes(db)
    return templates.TemplateResponse(
        request,
        "review_local_group.html",
        {
            "user": user,
            "preview": preview,
            "local_volumes": local_volumes,
            "conflicts": [],
            "form_values": {},
        },
    )


@router.post("/local-group")
async def local_group_submit(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[str, Form()] = "",
    volume_name: Annotated[str, Form()] = "",
    volume_year: Annotated[str, Form()] = "",
    publisher_name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    local_volume_id: Annotated[str, Form()] = "",
    file_id: Annotated[list[str] | None, Form()] = None,
    issue_number: Annotated[list[str] | None, Form()] = None,
    issue_name: Annotated[list[str] | None, Form()] = None,
):
    """Create the local volume (or attach the group to an existing one)
    + one local issue per file, flipping each file's ``file_matches``
    row to ``LOCAL``.

    Branches on ``local_volume_id``: when set, the group is attached to
    that existing local volume via ``attach_local_group`` — the
    volume-meta fields are ignored. When blank, the existing create-new
    path runs.

    ``file_id`` / ``issue_number`` / ``issue_name`` are parallel arrays
    — one entry per file row, in document order — zipped into per-file
    issue-number and issue-title maps. ``volume_year`` is free text,
    parsed leniently."""
    series_key = series or None

    parsed_year = safe_int(volume_year)

    # Zip the parallel form arrays into {file_id: issue_number} and
    # {file_id: issue_name}. Every file row submits exactly one hidden
    # file_id plus one issue_number and one issue_name input (text
    # inputs always submit, even when empty), so the three lists are the
    # same length under any real browser submission — ``strict=True``
    # codifies that invariant and fails loud on a malformed/tampered
    # POST instead of silently mis-keying the maps.
    ids = file_id or []
    nums = issue_number or []
    names = issue_name or []
    file_issue_numbers: dict[str, str] = {}
    file_issue_names: dict[str, str] = {}
    for fid, num, nm in zip(ids, nums, names, strict=True):
        key = fid.strip()
        file_issue_numbers[key] = num
        file_issue_names[key] = nm

    # Attach branch: the reviewer picked an existing local volume from
    # the find-or-create picker.
    existing_id: uuid.UUID | None = None
    if local_volume_id.strip():
        try:
            existing_id = uuid.UUID(local_volume_id.strip())
        except ValueError:
            existing_id = None

    if existing_id is not None:
        outcome = await attach_local_group(
            db,
            series_key=series_key,
            target_volume_id=existing_id,
            file_issue_numbers=file_issue_numbers,
            file_issue_names=file_issue_names,
            created_by=user.id,
        )
        if outcome is None:
            # Target volume was deleted or the group drained — fall back
            # to the queue, same as create_local_group's drained path.
            return RedirectResponse(url="/review", status_code=status.HTTP_303_SEE_OTHER)
        result, conflicts = outcome
        if conflicts:
            # Soft failure: re-render the form with conflicts surfaced
            # inline so the reviewer can edit the offending numbers.
            preview = await preview_local_group(db, series_key)
            if preview is None:
                return RedirectResponse(url="/review", status_code=status.HTTP_303_SEE_OTHER)
            local_volumes = await list_local_volumes(db)
            # Build a {file_id: error_reason} map for the template — one
            # entry per conflicting row, keyed exactly the way the
            # template iterates ``preview.files``.
            conflict_map = {str(c.file_id): c.reason for c in conflicts}
            return templates.TemplateResponse(
                request,
                "review_local_group.html",
                {
                    "user": user,
                    "preview": preview,
                    "local_volumes": local_volumes,
                    "conflicts": conflict_map,
                    # Echo the submitted values so the reviewer doesn't
                    # lose their edits on re-render.
                    "form_values": {
                        "local_volume_id": str(existing_id),
                        "file_issue_numbers": file_issue_numbers,
                        "file_issue_names": file_issue_names,
                    },
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            url=f"/review?file_local={result.issue_count}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Create branch: no existing volume picked.
    if not volume_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A volume name is required to create a local volume.",
        )

    result = await create_local_group(
        db,
        series_key=series_key,
        volume_name=volume_name,
        volume_year=parsed_year,
        publisher_name=publisher_name,
        volume_description=description,
        file_issue_numbers=file_issue_numbers,
        file_issue_names=file_issue_names,
        created_by=user.id,
    )
    if result is None:
        # Group drained between preview and submit — nothing committed.
        return RedirectResponse(url="/review", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(
        url=f"/review?file_local={result.issue_count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- Exclude-from-matching -------------------------------------------
#
# Registered BEFORE ``/{file_id}`` so the literal ``/group/exclude``
# path takes precedence over the catch-all. Mirrors the placement
# convention used by ``/local-group``.


@router.post("/group/exclude")
async def group_exclude(
    _: RequireAdminDep,
    db: DbSessionDep,
    series: Annotated[str, Form()] = "",
):
    """Flip ``excluded_from_matching = True`` on every reviewable
    file in the parsed-series group identified by ``series`` (the
    same key the create-local-volume flow uses).

    One-way action — no un-exclude path here, matching the user's
    "exclude is one-way" preference. Files already resolved (AUTO /
    CONFIRMED / LOCAL / SUPPLEMENT) are left alone; only the
    reviewable bucket gets the flag. Redirects back to the review
    queue with ``excluded=N`` so the banner says how many landed.
    """
    series_key = series or None
    n = await exclude_files_by_series(db, series_key=series_key)
    return RedirectResponse(
        url=f"/review?file_excluded={n}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{file_id}")
async def file_review(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
):
    """Single-file review page: the file's cover + parsed signals,
    the matcher's ranked candidates, and the confirm / reject /
    manual-search actions."""
    review = await get_file_review(db, file_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )

    # Background-hydrate the candidates' real issue covers — same
    # fire-and-forget as the queue. CV's volume payload has no image
    # data for nested issues, so fresh candidates fall back to the
    # parent volume's cover until a per-issue fetch fills them in.
    for c in review.candidates:
        enqueue_revalidate("issue", c.issue_cv_id)

    return templates.TemplateResponse(
        request,
        "review_file.html",
        {"user": user, "review": review},
    )


@router.post("/{file_id}/confirm")
async def file_confirm(
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    issue_cv_id: Annotated[int, Form(ge=1)],
):
    """Confirm a single file to a chosen CV issue.

    The ``cv_issues`` row must exist locally so the ``file_matches``
    FK is satisfied. We check the DB first and only hit ComicVine
    when it's actually missing — that's the only case worth waiting
    on the rate-pacer for. Everything else (matcher candidates, bulk-
    hydrated stubs, previously-fetched issues) already has a row;
    a stub's freshness is irrelevant for the confirm action itself,
    and background SWR will refresh it later.

    Without this short-circuit, every confirm tried to refresh the
    row through the cache layer. During a rate-limit cooldown the
    pacer sleeps up to ``DEFAULT_MAX_INLINE_WAIT_SECONDS`` (45s) per
    request and then raises — every confirm stalls 45s and returns
    a 502. With it, confirms on cached issues are instant; only the
    rare manual-search-picked-novel-issue case touches CV at all."""
    existing_issue = await db.get(CvIssue, issue_cv_id)
    if existing_issue is None:
        # No local row — must hit CV (typically the manual-search
        # flow picked an issue the matcher never considered, so
        # nothing in our cache references it yet).
        client = ComicVineClient()
        try:
            cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
            await cache.get_issue(db, issue_cv_id)
        except ComicVineNotFoundError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Issue {issue_cv_id} doesn't exist on ComicVine.",
            ) from e
        except ComicVineRateLimitError as e:
            # Specific message + 503 (vs the generic 502 below) so
            # the user knows the failure is transient and how long
            # to wait, not a permanent CV outage.
            wait_s = int(e.retry_after or 60)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"ComicVine is rate-limiting us right now — this issue "
                    f"isn't in our cache, so we have to fetch it. Try again "
                    f"in about {wait_s}s."
                ),
            ) from e
        except ComicVineError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Couldn't load issue {issue_cv_id} from ComicVine: {e}",
            ) from e
        finally:
            await client.aclose()

    ok = await confirm_file_match(
        db,
        file_id=file_id,
        issue_cv_id=issue_cv_id,
        matched_by_user_id=user.id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    return RedirectResponse(
        url="/review?file_confirmed=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{file_id}/reject")
async def file_reject(
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
):
    """Reject every candidate for a file — it leaves the PENDING
    queue without being confirmed to anything. POST-only so a stray
    prefetch can't trip it."""
    ok = await reject_file_match(
        db,
        file_id=file_id,
        matched_by_user_id=user.id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    return RedirectResponse(
        url="/review?file_rejected=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- Add as a local entry (single-file, Phase 11B) --------------------
#
# For a file whose comic isn't in ComicVine at all. A dedicated form
# page: the reviewer hand-enters core identification metadata, the
# service writes ``local_volumes`` / ``local_issues`` rows and flips
# the file's ``file_matches`` row to ``LOCAL``.


@router.get("/{file_id}/local")
async def file_local_entry(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
):
    """The 'add as a local entry' form. Seeded from the file's parsed-
    filename signals; the volume field is a find-or-create picker over
    the existing local volumes (preloaded for a client-side filter)."""
    review = await get_file_review(db, file_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    local_volumes = await list_local_volumes(db)
    return templates.TemplateResponse(
        request,
        "review_local_entry.html",
        {"user": user, "review": review, "local_volumes": local_volumes},
    )


@router.post("/{file_id}/local")
async def file_local_entry_submit(
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    volume_name: Annotated[str, Form()] = "",
    volume_year: Annotated[str, Form()] = "",
    publisher_name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    local_volume_id: Annotated[str, Form()] = "",
    issue_number: Annotated[str, Form()] = "",
    issue_name: Annotated[str, Form()] = "",
):
    """Create the local volume/issue and flip the file to ``LOCAL``.

    ``local_volume_id`` is set only when the reviewer picked an existing
    local volume in the find-or-create picker; otherwise a new volume is
    created from the name / year / publisher fields. ``volume_year`` is
    a free-text field, parsed leniently."""
    existing_volume_id: uuid.UUID | None = None
    if local_volume_id.strip():
        try:
            existing_volume_id = uuid.UUID(local_volume_id.strip())
        except ValueError:
            existing_volume_id = None

    parsed_year = safe_int(volume_year)

    if existing_volume_id is None and not volume_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A volume name is required to create a local entry.",
        )

    result = await create_local_entry(
        db,
        file_id=file_id,
        existing_volume_id=existing_volume_id,
        volume_name=volume_name,
        volume_year=parsed_year,
        publisher_name=publisher_name,
        volume_description=description,
        issue_number=issue_number,
        issue_name=issue_name,
        created_by=user.id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    return RedirectResponse(
        url="/review?file_local=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- Manual volume → issue search (single-file) -----------------------
#
# The per-file manual search funnels volume-first, mirroring the
# matcher and the volume-confirm flow: search CV for the volume, pick
# one, then pick the issue from that volume's issue list. Step 1
# reuses the shared volume-search template in "file mode"; step 2 has
# its own issue-picker template.


async def _file_volume_search(db, review, q: str | None) -> tuple[str, str, list[dict], str | None]:
    """Run the per-file ComicVine volume search shared by the manual
    issue-match and the supplement-attach flows.

    ``q`` is the editable search box; when None it defaults to the
    file's parsed series. Returns ``(raw_query, cleaned, results,
    error)`` — results are publisher-enriched and ready to render."""
    series = (
        review.parsed_long_series
        if (review.parsed_long_series and review.parsed_long_series != review.parsed_series)
        else review.parsed_series
    )
    raw_query = q if q is not None else (series or "")
    cleaned = clean_search_query(raw_query)

    results: list[dict] = []
    error: str | None = None
    if cleaned:
        client = ComicVineClient()
        try:
            cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
            envelope = await cache.search(db, cleaned, resources="volume", limit=50)
            results = shape_volume_results(envelope)
        except ComicVineKeyMissingError:
            error = (
                "No ComicVine API key is configured. "
                "Set one in the admin settings before searching."
            )
        except ComicVineKeyInvalidError:
            error = "ComicVine rejected the API key. Re-paste it in the admin settings."
        except ComicVineError as e:
            error = f"ComicVine search failed: {e}"
        finally:
            await client.aclose()

    if results:
        pub_map = await publishers_for_volumes(db, {r["cv_id"] for r in results})
        for r in results:
            if not r["publisher"]:
                r["publisher"] = pub_map.get(r["cv_id"])
    return raw_query, cleaned, results, error


@router.get("/{file_id}/volume-search")
async def file_volume_search(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    q: Annotated[str | None, Query()] = None,
):
    """Step 1 of the per-file manual search: search ComicVine for the
    volume this file belongs to.

    ``q`` is the editable search box; when absent it defaults to the
    file's parsed series, so arriving from the file page's "Search
    ComicVine" button shows results immediately. Each result links to
    step 2 — the issue picker for that volume."""
    review = await get_file_review(db, file_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    raw_query, cleaned, results, error = await _file_volume_search(db, review, q)

    # The shared volume-search template renders in "file mode" when
    # ``review`` is present — switching the reference card, the
    # result links (to the issue picker), and the breadcrumb.
    return templates.TemplateResponse(
        request,
        "review_volume_search.html",
        {
            "user": user,
            "review": review,
            "query": raw_query,
            "cleaned_query": cleaned,
            "results": results,
            "result_facets": result_facets(results),
            "error": error,
        },
    )


# ---- Attach as a supplement (single-file, Phase 11F) ------------------
#
# For a non-issue file (a cover gallery, extras archive) that belongs to
# a real ComicVine series. The reviewer searches CV for the volume and
# attaches the file straight to it as a ``SUPPLEMENT`` — no issue match.


@router.get("/{file_id}/supplement")
async def file_supplement(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    q: Annotated[str | None, Query()] = None,
):
    """The 'attach as supplement' page: search ComicVine for the volume
    this non-issue file belongs to. Each result carries an inline
    attach form (volume + supplement type)."""
    review = await get_file_review(db, file_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    raw_query, cleaned, results, error = await _file_volume_search(db, review, q)
    return templates.TemplateResponse(
        request,
        "review_supplement.html",
        {
            "user": user,
            "review": review,
            "query": raw_query,
            "cleaned_query": cleaned,
            "results": results,
            "result_facets": result_facets(results),
            "error": error,
            "supplement_types": SUPPLEMENT_TYPES,
        },
    )


@router.post("/{file_id}/supplement")
async def file_supplement_submit(
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    volume_cv_id: Annotated[int, Form(ge=1)],
    supplement_type: Annotated[str, Form()],
):
    """Attach the file to the chosen CV volume as a supplement.

    The ``cv_volumes`` row must exist locally so the ``file_matches``
    FK is satisfied. We check the DB first and only hit ComicVine
    when it's actually missing — same short-circuit ``file_confirm``
    uses, for the same reason: a stub's freshness doesn't matter for
    the attach action, and falling through to ``cache.get_volume``
    during a rate-limit cooldown costs up to a 45s pacer stall + a
    502 response."""
    if supplement_type not in SUPPLEMENT_TYPE_LABELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown supplement type: {supplement_type}.",
        )
    existing_volume = await db.get(CvVolume, volume_cv_id)
    if existing_volume is None:
        client = ComicVineClient()
        try:
            cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
            await cache.get_volume(db, volume_cv_id)
        except ComicVineNotFoundError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Volume {volume_cv_id} doesn't exist on ComicVine.",
            ) from e
        except ComicVineRateLimitError as e:
            wait_s = int(e.retry_after or 60)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"ComicVine is rate-limiting us right now — this volume "
                    f"isn't in our cache, so we have to fetch it. Try again "
                    f"in about {wait_s}s."
                ),
            ) from e
        except ComicVineError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Couldn't load volume {volume_cv_id} from ComicVine: {e}",
            ) from e
        finally:
            await client.aclose()

    ok = await attach_supplement(
        db,
        file_id=file_id,
        volume_cv_id=volume_cv_id,
        supplement_type=supplement_type,
        attached_by=user.id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )
    return RedirectResponse(
        url="/review?file_supplement=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{file_id}/volume-issues")
async def file_volume_issues(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    volume: Annotated[int, Query(ge=1)],
    q: Annotated[str | None, Query()] = None,
):
    """Step 2 of the per-file manual search: pick the issue from the
    chosen volume.

    The volume is hydrated through the CV cache (which writes its
    issue rows), then every issue is listed for the reviewer to pick.
    The issue matching the file's parsed number is flagged as the
    suggested pick. ``q`` is the step-1 search string, echoed so the
    "back to volume search" link returns to those results."""
    review = await get_file_review(db, file_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No match record for this file.",
        )

    client = ComicVineClient()
    try:
        cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
        volume_row = await cache.get_volume(db, volume)
    except ComicVineNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume {volume} doesn't exist on ComicVine.",
        ) from e
    except ComicVineError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Couldn't load volume {volume} from ComicVine: {e}",
        ) from e
    finally:
        await client.aclose()

    issues = await list_volume_issues(
        db,
        volume,
        suggested_number=review.parsed_issue_number,
    )

    # Center the picker's initial pagination window on the
    # parsed-number match, so one issue buried deep in a long run is
    # on screen without scrolling. ``suggested_index`` is the match's
    # position in the natural-sorted list; ``initial_start`` is the
    # windowed-page offset that puts it mid-window. With no match
    # (no parsed number, or the number isn't in this volume) the
    # window just starts at the top.
    page_size = await get_page_size(db)
    total = len(issues)
    suggested_index = next((idx for idx, opt in enumerate(issues) if opt.is_suggested), None)
    if suggested_index is not None:
        initial_start = max(
            0,
            min(suggested_index - page_size // 2, max(0, total - page_size)),
        )
    else:
        initial_start = 0

    return templates.TemplateResponse(
        request,
        "review_file_issues.html",
        {
            "user": user,
            "review": review,
            "volume_cv_id": volume,
            "volume_name": volume_row.name,
            "volume_year": volume_row.year,
            "volume_cover_url": cv_image_url(volume_row.raw_payload, "thumb"),
            "issues": issues,
            "back_query": q or "",
            "page_size": page_size,
            "suggested_index": suggested_index,
            "initial_start": initial_start,
        },
    )
