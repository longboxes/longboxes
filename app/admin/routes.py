"""Admin routes mounted under ``/admin``.

Every route here requires the admin role (enforced by ``RequireAdminDep``).
Viewers get a 403 from the dependency before the handler runs.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse
from redis import Redis
from sqlalchemy import select, update

from app.auth.dependencies import DbSessionDep, RequireAdminDep
from app.comicvine import ComicVineCache, ComicVineClient
from app.comicvine.errors import (
    ComicVineError,
    ComicVineKeyInvalidError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
)
from app.config import settings
from app.jobs.queue_status import (
    clear_all_failed_jobs,
    clear_failed_jobs_by_class,
    delete_failed_job,
    get_queue_stats,
    list_failed_jobs,
    requeue_all_failed_jobs,
    requeue_failed_job,
    requeue_failed_jobs_by_class,
)
from app.jobs.revalidate import enqueue_revalidate
from app.jobs.scan import enqueue_scan_now
from app.models import CvIssue, CvPublisher, CvStoryArc, CvVolume, FileMatch
from app.services.duplicates import (
    count_hash_duplicate_groups,
    count_issue_duplicate_groups,
    list_hash_duplicates,
    list_issue_duplicates,
    mark_file_excluded,
)
from app.services.file_errors import (
    count_file_errors,
    dismiss_file_error,
    list_file_errors,
    try_open_archive,
)
from app.services.health import compute_health
from app.services.local import SUPPLEMENT_TYPES, attach_supplement
from app.services.settings import (
    ARCHIVE_BACKENDS,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MIN_PAGE_SIZE,
    get_archive_backend,
    get_cv_api_key,
    get_library_paths,
    get_page_size,
    get_scan_interval_seconds,
    redact_cv_api_key,
    set_archive_backend,
    set_cv_api_key,
    set_page_size,
)
from app.templates_env import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("")
async def admin_home(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
):
    """Placeholder admin page — library paths, scan, CV key, add-volume."""
    library_paths = await get_library_paths(db)
    scan_interval = await get_scan_interval_seconds(db)
    cv_key = await get_cv_api_key(db)
    page_size = await get_page_size(db)
    archive_backend = await get_archive_backend(db)
    recent_volumes = (
        (
            await db.execute(
                select(CvVolume).order_by(CvVolume.fetched_at.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "admin_home.html",
        {
            "user": user,
            "library_paths": library_paths,
            "scan_interval_seconds": scan_interval,
            "cv_key_display": redact_cv_api_key(cv_key),
            "cv_key_configured": cv_key is not None,
            "recent_volumes": recent_volumes,
            "page_size": page_size,
            "page_size_min": MIN_PAGE_SIZE,
            "page_size_max": MAX_PAGE_SIZE,
            "page_size_default": DEFAULT_PAGE_SIZE,
            "archive_backend": archive_backend,
            "archive_backends": ARCHIVE_BACKENDS,
        },
    )


@router.get("/health")
async def admin_health(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
):
    """Library health report — match-readiness stats, duplicates, projection
    vs observed match rate, plus a live background-job queue snapshot.
    Per §9 of the design doc."""
    report = await compute_health(db)
    queue = get_queue_stats()
    # Per-file failure inventory — counts archive-open / cover-
    # extraction / comicinfo-parse failures recorded by the scanner
    # and cover endpoint. Distinct from RQ's FailedJobRegistry (which
    # is per-job, not per-file), so it gets its own stat next to it.
    file_errors_count = await count_file_errors(db)
    # Duplicate-group counts — kept separate from the existing
    # ``duplicate_files`` stat (which counts *files*, useful for byte
    # accounting) because the inspector page works in groups (one
    # issue + N files, one file + N paths).
    hash_dup_groups = await count_hash_duplicate_groups(db)
    issue_dup_groups = await count_issue_duplicate_groups(db)
    return templates.TemplateResponse(
        request,
        "admin_health.html",
        {
            "user": user,
            "report": report,
            "queue": queue,
            "file_errors_count": file_errors_count,
            "hash_dup_groups": hash_dup_groups,
            "issue_dup_groups": issue_dup_groups,
        },
    )


@router.post("/rescan")
async def rescan(_: RequireAdminDep):
    """Enqueue a one-off scan and bounce back to the admin home."""
    conn = Redis.from_url(settings.redis_url)
    enqueue_scan_now(conn)
    return RedirectResponse(url="/admin?rescan=queued", status_code=303)


@router.get("/failed-jobs")
async def admin_failed_jobs(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
):
    """Listing of every job currently in the FailedJobRegistry, with
    each match-file job resolved to the file's current on-disk path.
    Lets the operator distinguish transient errors (asyncpg loop
    races, redis blips — requeue all and move on) from sticky ones
    (a specific archive that crashes the matcher every time — go
    look at the file on disk)."""
    records = await list_failed_jobs(db)
    # Group by exception class so the dominant failure mode jumps
    # out — typically 90% of the rows share one exception class on
    # a real stress-test failure run, and the user wants to act on
    # that bucket without scrolling the per-row list.
    by_exc: dict[str, list] = {}
    for r in records:
        by_exc.setdefault(r.exc_class, []).append(r)
    grouped = sorted(by_exc.items(), key=lambda kv: -len(kv[1]))
    return templates.TemplateResponse(
        request,
        "admin_failed_jobs.html",
        {
            "user": user,
            "records": records,
            "grouped": grouped,
            "total": len(records),
        },
    )


@router.post("/failed-jobs/{job_id}/requeue")
async def admin_failed_job_requeue(_: RequireAdminDep, job_id: str):
    """Move one failed job back to the dispatchable queue."""
    requeue_failed_job(job_id)
    return RedirectResponse(
        url="/admin/failed-jobs?requeued=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/failed-jobs/requeue-all")
async def admin_failed_jobs_requeue_all(_: RequireAdminDep):
    """Bulk-requeue every job currently in the failed registry. The
    common case after a transient infrastructure blip — kicks the
    backlog back into the worker without per-row clicking."""
    count = requeue_all_failed_jobs()
    return RedirectResponse(
        url=f"/admin/failed-jobs?requeued_all={count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/failed-jobs/{job_id}/delete")
async def admin_failed_job_delete(_: RequireAdminDep, job_id: str):
    """Drop one failed job permanently. The right action when its
    function no longer exists in the worker (a since-deleted job
    class) or when the operator has already handled the underlying
    file out-of-band and just wants the row gone."""
    delete_failed_job(job_id)
    return RedirectResponse(
        url="/admin/failed-jobs?deleted=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/failed-jobs/clear-all")
async def admin_failed_jobs_clear_all(_: RequireAdminDep):
    """Clear every failed job at once. Useful when the registry is
    full of stale entries from old, deleted job functions — re-
    queueing them would just crash again with ``ModuleNotFoundError``.
    """
    count = clear_all_failed_jobs()
    return RedirectResponse(
        url=f"/admin/failed-jobs?cleared_all={count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# Per-exception-class bulk actions. The class string can contain
# parens / angle brackets / quotes when the parser hands back a
# SQLAlchemy wrapper line (``(sqlalchemy.dialects... .IntegrityError)
# <class '...'>``), so the class is sent in the request body rather
# than the URL path to dodge encoding pitfalls. Lets the admin sweep
# a sticky exception class (16 stale FK violations, every
# UnboundLocalError, etc.) in one click instead of N.
@router.post("/failed-jobs/by-class/requeue")
async def admin_failed_jobs_requeue_by_class(
    _: RequireAdminDep, exc_class: Annotated[str, Form()]
):
    count = requeue_failed_jobs_by_class(exc_class)
    return RedirectResponse(
        url=f"/admin/failed-jobs?requeued_all={count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/failed-jobs/by-class/clear")
async def admin_failed_jobs_clear_by_class(
    _: RequireAdminDep, exc_class: Annotated[str, Form()]
):
    count = clear_failed_jobs_by_class(exc_class)
    return RedirectResponse(
        url=f"/admin/failed-jobs?cleared_all={count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- File errors inspector --------------------------------------------


_FILE_ERROR_KIND_LABELS = {
    "archive_open": "Archive open failures",
    "cover_extraction": "Cover extraction failures",
    "comicinfo_parse": "ComicInfo parse failures",
}


@router.get("/file-errors")
async def admin_file_errors(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
):
    """Inspector for files the scanner / cover endpoint / ComicInfo
    parser couldn't process.

    Three kinds are surfaced today (see ``FileErrorKind``). Rows are
    auto-cleared when a subsequent scan / cover view / parse for the
    same path succeeds — the page is "currently broken," not "ever
    broke." The retry button re-attempts the archive open; success
    clears the row in place.
    """
    records = await list_file_errors(db)
    # Group by kind so the operator sees "12 archive-open failures
    # in this scan" as a single section rather than a 12-row mixed
    # list. Sorted by group size descending — the dominant failure
    # mode jumps to the top, mirroring /admin/failed-jobs.
    by_kind: dict[str, list] = {}
    for r in records:
        by_kind.setdefault(r.kind, []).append(r)
    grouped = sorted(by_kind.items(), key=lambda kv: -len(kv[1]))
    return templates.TemplateResponse(
        request,
        "admin_file_errors.html",
        {
            "user": user,
            "grouped": grouped,
            "total": len(records),
            "kind_labels": _FILE_ERROR_KIND_LABELS,
        },
    )


@router.post("/file-errors/{error_id}/try-open")
async def admin_file_error_try_open(
    _: RequireAdminDep,
    db: DbSessionDep,
    error_id: uuid.UUID,
):
    """Run ``open_archive`` against the recorded path.

    On success the row (and any other kinds recorded for the same
    path) is deleted and the redirect carries a ``opened=1`` banner.
    On failure the row's exception fields refresh and the redirect
    carries an ``open_failed=1`` banner — the listing's
    ``last_seen_at`` reflects the latest attempt.
    """
    result = await try_open_archive(db, error_id)
    if result is None:
        # Already cleared by a concurrent scan; no-op feedback is fine.
        return RedirectResponse(
            url="/admin/file-errors",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    qs = "opened=1" if result.ok else "open_failed=1"
    return RedirectResponse(
        url=f"/admin/file-errors?{qs}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/file-errors/{error_id}/dismiss")
async def admin_file_error_dismiss(
    _: RequireAdminDep,
    db: DbSessionDep,
    error_id: uuid.UUID,
):
    """Drop the row without retrying. The right action when the
    operator has already handled the file out-of-band (replaced it,
    deleted it, moved it) and just wants the entry gone."""
    await dismiss_file_error(db, error_id)
    return RedirectResponse(
        url="/admin/file-errors?dismissed=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---- Duplicates inspector --------------------------------------------


@router.get("/duplicates")
async def admin_duplicates(
    request: Request,
    user: RequireAdminDep,
    db: DbSessionDep,
):
    """Triage view for the two duplicate kinds.

    * Hash duplicates — same archive at multiple paths. The on-disk
      file is byte-identical, so deletion is lossless once one path
      is kept.
    * Issue duplicates — different sha256 files all claiming the
      same CV issue. The service ranks each file by quality (format
      / ComicInfo coverage / page count / cover resolution / size /
      recency) and flags a recommended keeper.

    Read-only inspector plus one action: mark a file
    ``excluded_from_matching`` so the matcher stops claiming it as a
    duplicate while you decide what to do with it.
    """
    hash_groups = await list_hash_duplicates(db)
    issue_listing = await list_issue_duplicates(db)

    # Mid-hydration nudge. Some issue-duplicate groups got suppressed
    # because their CV cover hasn't been hydrated yet — fire a
    # ``volume_issues`` bulk-revalidate for each of the affected
    # volumes so the next page load includes them. ``enqueue_revalidate``
    # dedupes via its Redis marker key, so a refresh-spam doesn't pile
    # up jobs; and the ``volume_issues`` entity always lands at the
    # head of the queue (see ``enqueue_revalidate``), so the
    # operator's reload-after-coffee actually shows new groups.
    for vol_cv_id in issue_listing.deferred_volume_cv_ids:
        try:
            enqueue_revalidate("volume_issues", vol_cv_id)
        except Exception:
            # Defensive — a single bad enqueue shouldn't block the
            # whole page render. The revalidate helper already
            # tries to be crash-safe, but belt-and-suspenders here
            # since this is an admin-facing surface.
            pass

    return templates.TemplateResponse(
        request,
        "admin_duplicates.html",
        {
            "user": user,
            "hash_groups": hash_groups,
            "issue_groups": issue_listing.groups,
            "deferred_count": issue_listing.deferred_count,
            # Drives the per-row "Make supplement" dropdown so the
            # vocabulary stays single-sourced in app.services.local.
            "supplement_types": SUPPLEMENT_TYPES,
        },
    )


@router.post("/duplicates/file/{file_id}/exclude")
async def admin_duplicate_exclude(
    _: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
):
    """Flip the file's ``excluded_from_matching`` flag.

    The matcher's existing guard treats excluded files as UNMATCHED
    (writes no row, doesn't claim any issue), so the issue-duplicate
    group will drop to one resolved file on the next match run."""
    await mark_file_excluded(db, file_id)
    return RedirectResponse(
        url="/admin/duplicates?excluded=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/duplicates/file/{file_id}/supplement")
async def admin_duplicate_make_supplement(
    user: RequireAdminDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    supplement_type: Annotated[str, Form()],
):
    """Re-resolve a duplicate file as a supplement on its current CV
    volume.

    The triage workflow: the duplicates inspector shows a 3-page
    archive that's claiming the same issue as a real 25-page comic.
    Often that small archive is a cover gallery / sketch bonus /
    behind-the-scenes page — it belongs on the volume, just not as
    the issue. This route reads the file's currently-matched
    ``cv_issues`` row to learn the volume, then delegates to
    ``attach_supplement`` to rewrite ``file_matches`` to a
    ``SUPPLEMENT`` resolution. The original duplicate disappears from
    the issue-duplicate listing on the next render; the file now
    shows up in the volume page's Supplements section.

    Validation:
      * ``supplement_type`` must be in ``SUPPLEMENT_TYPES``.
      * The file must currently have a ``file_matches`` row with a
        non-null ``issue_cv_id`` — that's how we resolve the volume.
        (A SUPPLEMENT or LOCAL row has no issue, so this is the
        right gate.)
    """
    valid_types = {key for key, _ in SUPPLEMENT_TYPES}
    if supplement_type not in valid_types:
        return RedirectResponse(
            url="/admin/duplicates?bad_supplement_type=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    fm = await db.get(FileMatch, file_id)
    if fm is None or fm.issue_cv_id is None:
        return RedirectResponse(
            url="/admin/duplicates?no_issue=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    issue = await db.get(CvIssue, fm.issue_cv_id)
    if issue is None or issue.volume_cv_id is None:
        return RedirectResponse(
            url="/admin/duplicates?no_volume=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    await attach_supplement(
        db,
        file_id=file_id,
        volume_cv_id=issue.volume_cv_id,
        supplement_type=supplement_type,
        attached_by=user.id,
    )
    return RedirectResponse(
        url="/admin/duplicates?supplemented=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/match-all")
async def match_all(_: RequireAdminDep, db: DbSessionDep):
    """Enqueue a match_file job for every files row that doesn't yet have
    a resolved match (no row, unmatched, or pending). One-shot recovery
    button for situations where the per-file enqueue from the scanner got
    interrupted, or after a CV-side data change.

    Calls the async variant directly with the request-scoped session — the
    sync ``enqueue_match_all_unmatched`` uses ``asyncio.run`` internally,
    which raises inside an already-running FastAPI event loop.
    """
    from app.jobs.match_file import enqueue_match_all_unmatched_async

    count = await enqueue_match_all_unmatched_async(db)
    return RedirectResponse(
        url=f"/admin?match_all_queued={count}", status_code=303
    )


@router.post("/cv-key")
async def save_cv_key(
    _: RequireAdminDep,
    db: DbSessionDep,
    api_key: Annotated[str, Form()],
):
    """Save the ComicVine API key to ``app_settings``.

    Validates against ComicVine before storing — a typo is caught
    here rather than discovered three minutes later when the matcher
    starts failing.

    Match jobs the scanner already enqueued while the key was missing
    are sitting in the worker's held state (each reschedules itself
    on a short cadence — see ``app/jobs/match_file.NO_KEY_RESCHEDULE_SECONDS``).
    They pick up the new key on their next wake, so this route does
    not need to fire a match-all pass to unblock them.
    """
    from app.comicvine.client import validate_cv_api_key

    cleaned = api_key.strip()
    if not cleaned:
        return RedirectResponse(url="/admin?cv_key=empty", status_code=303)
    ok, _err = await validate_cv_api_key(cleaned)
    if not ok:
        # Reuse the existing ?cv_key= surface — admin_home.html
        # renders a rose banner for the "invalid" value.
        return RedirectResponse(url="/admin?cv_key=invalid", status_code=303)

    await set_cv_api_key(db, cleaned)
    await db.commit()
    return RedirectResponse(url="/admin?cv_key=saved", status_code=303)


@router.post("/volume")
async def add_volume(
    _: RequireAdminDep,
    db: DbSessionDep,
    cv_id: Annotated[str, Form()],
):
    """Synchronously fetch a volume by CV ID via the cache layer.

    On success, redirects to ``/admin?volume_added=<id>``; the admin home
    will show it in the "recent volumes" list. Errors set a banner via the
    query string.

    The synchronous fetch is fine for Phase 3 — CV calls take < 1s in the
    happy path, and the rate limiter has plenty of budget for the occasional
    ad-hoc add. Heavy batch ingest would warrant an async-job path; we'll
    revisit if/when that's a real workflow.
    """
    try:
        cv_id_int = int(cv_id.strip())
    except ValueError:
        return RedirectResponse(url="/admin?volume_error=bad_id", status_code=303)

    client = ComicVineClient()
    try:
        cache = ComicVineCache(client, enqueue_revalidate=enqueue_revalidate)
        try:
            # ``force_refresh=True`` makes "Add volume" always go to
            # CV — re-adding an existing volume otherwise short-circuits
            # on the fresh cache and skips the upsert path, which is
            # the only place we enqueue boundary-issue (first/last)
            # hydration for the year-span filter. The synchronous CV
            # call is fine here; same as the original add.
            volume = await cache.get_volume(db, cv_id_int, force_refresh=True)
        except ComicVineKeyMissingError:
            return RedirectResponse(url="/admin?volume_error=no_key", status_code=303)
        except ComicVineKeyInvalidError:
            # The key is set but CV rejected it. Surface distinctly so the
            # admin knows to re-paste rather than to chase a different bug.
            return RedirectResponse(
                url="/admin?volume_error=invalid_key", status_code=303
            )
        except ComicVineNotFoundError:
            return RedirectResponse(
                url="/admin?volume_error=not_found", status_code=303
            )
        except ComicVineError as e:
            # Catch-all: rate limit, network, malformed response.
            return RedirectResponse(
                url=f"/admin?volume_error=cv_error&detail={type(e).__name__}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        await client.aclose()

    return RedirectResponse(
        url=f"/admin?volume_added={volume.cv_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/page-size")
async def save_page_size(
    _: RequireAdminDep,
    db: DbSessionDep,
    page_size: Annotated[str, Form()],
):
    """Update the global page-size setting.

    Used by library cards, publisher arcs, volume issues, arc issues —
    everywhere with ``setupPagination``. The setter clamps to the
    ``MIN_PAGE_SIZE`` / ``MAX_PAGE_SIZE`` range so the form HTML's
    ``min`` / ``max`` attributes aren't load-bearing for safety; the
    explicit range banner on failure mirrors the clamp."""
    try:
        n = int(page_size.strip())
    except ValueError:
        return RedirectResponse(
            url="/admin?page_size=bad_value", status_code=303
        )
    if n < MIN_PAGE_SIZE or n > MAX_PAGE_SIZE:
        return RedirectResponse(
            url="/admin?page_size=out_of_range", status_code=303
        )
    await set_page_size(db, n)
    await db.commit()
    return RedirectResponse(url="/admin?page_size=saved", status_code=303)


@router.post("/archive-backend")
async def save_archive_backend(
    _: RequireAdminDep,
    db: DbSessionDep,
    backend: Annotated[str, Form()],
):
    """Switch the archive reader between comicbox and stdlib.

    The change is immediate — the next scanner/matcher/reader call
    fetches the live setting. No service restart required."""
    try:
        await set_archive_backend(db, backend.strip())
    except ValueError:
        return RedirectResponse(
            url="/admin?archive_backend=bad_value", status_code=303
        )
    await db.commit()
    return RedirectResponse(
        url="/admin?archive_backend=saved", status_code=303
    )


@router.post("/cache/clear")
async def clear_cv_cache(
    _: RequireAdminDep,
    db: DbSessionDep,
):
    """Mark every cached CV entity stale so the next read re-fetches.

    Sets ``fetched_at = NULL`` on cv_volumes / cv_issues / cv_story_arcs
    / cv_publishers. The cache layer's freshness check is "fetched_at
    is not None AND not older than the per-entity TTL," so NULL rows
    fall through to the live-CV branch on the very next request and
    get refreshed automatically.

    Why NULL-the-timestamp rather than ``DELETE FROM cv_*``:

      * File matches (``file_matches.issue_cv_id``) hold FKs into
        cv_issues. Deleting would either cascade-wipe the user's
        library or fail. NULL-ing preserves the FKs.
      * The raw_payload column stays populated, so pages render in
        degraded mode (no covers maybe, but no broken JSON) while
        the SWR refresh is in flight.
      * Stub rows (already ``fetched_at IS NULL``) and bulk-only
        issue rows are untouched by the bulk update — they'd refetch
        anyway under the cache layer's existing stub logic.
    """
    counts: dict[str, int] = {}
    for model, label in [
        (CvVolume, "volume"),
        (CvIssue, "issue"),
        (CvStoryArc, "story_arc"),
        (CvPublisher, "publisher"),
    ]:
        result = await db.execute(
            update(model)
            .where(model.fetched_at.is_not(None))
            .values(fetched_at=None)
        )
        counts[label] = int(result.rowcount or 0)
    await db.commit()
    # Encode counts as ``v=1&i=2&a=3&p=4`` so the admin home banner
    # can show a per-entity tally without parsing dict-form values.
    summary = (
        f"v={counts['volume']}&i={counts['issue']}"
        f"&a={counts['story_arc']}&p={counts['publisher']}"
    )
    return RedirectResponse(
        url=f"/admin?cache_cleared=1&{summary}", status_code=303
    )
