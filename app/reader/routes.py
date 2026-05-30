"""Reader routes — Phase 6.

A page-by-page CBZ / CBR / CB7 / PDF web reader:

  * ``GET  /read/{file_id}`` — the viewer shell (a full-screen page-by-
    page reader), opened at the user's last-read page.
  * ``GET  /read/{file_id}/page/{index}`` — streams one page image,
    0-based.
  * ``POST /read/{file_id}/direction/{direction}`` — persist the
    per-volume left-to-right / right-to-left reading direction.
  * ``POST /read/{file_id}/progress/{page}`` — persist the user's
    reading position (a no-op while tracking is paused).
  * ``POST /read/{file_id}/reset-progress`` — clear that position.
  * ``POST /reading/tracking`` — flip the user's progress-tracking flag.

Page extraction reuses the shared archive readers — the same path the
review cover endpoint takes. The archive is opened per request (sync
work pushed off the event loop with ``to_thread``); the browser's HTTP
cache covers re-views, so there's no server-side page cache.

Reading direction lives on the volume and reading progress in the
per-user ``read_progress`` table — see ``app/services/reader.py``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from app.archives import open_archive
from app.archives.base import ArchiveError, UnsupportedArchiveError
from app.auth.dependencies import DbSessionDep, RequireUserDep
from app.models import File, FileLocation, User
from app.services.reader import (
    READING_DIRECTIONS,
    get_read_progress,
    get_reading_direction,
    is_file_match_resolved,
    reset_read_progress,
    save_read_progress,
    set_reading_direction,
)
from app.services.settings import get_archive_backend
from app.templates_env import templates

logger = logging.getLogger("longboxes.reader")

router = APIRouter()

# Image content type by page-file extension. The reader streams page
# bytes verbatim, so the type is inferred from the archived filename;
# anything unrecognised falls back to JPEG (the common comic-page type).
_PAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


async def _current_location(
    db: DbSessionDep, file_id: uuid.UUID
) -> FileLocation | None:
    """The file's current (non-missing) on-disk location, newest first.

    A file can have several locations (the same content at multiple
    paths); any current one will do for reading."""
    stmt = (
        select(FileLocation)
        .where(FileLocation.file_id == file_id)
        .where(FileLocation.missing_since.is_(None))
        .order_by(FileLocation.last_seen_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _page_count(path: str, backend: str) -> int:
    """Number of pages in the archive. Sync — call via ``to_thread``."""
    return len(open_archive(Path(path), backend=backend).list_pages())


def _read_page(path: str, backend: str, index: int) -> tuple[bytes, str]:
    """Open the archive and return ``(image_bytes, content_type)`` for
    the 0-based page ``index``. Sync — call via ``to_thread``.

    Raises ``ArchiveError`` when ``index`` is out of range so the caller
    can map it to a 404 like any other unreadable-page case."""
    reader = open_archive(Path(path), backend=backend)
    pages = reader.list_pages()
    if index < 0 or index >= len(pages):
        raise ArchiveError(
            f"page {index} out of range (archive has {len(pages)})"
        )
    name = pages[index]
    data = reader.extract_page(name)
    content_type = _PAGE_CONTENT_TYPES.get(
        Path(name).suffix.lower(), "image/jpeg"
    )
    return data, content_type


@router.get("/read/{file_id}")
async def read_file(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    preview: bool = False,
):
    """The reader shell — a full-screen, page-by-page viewer for a file.

    The page count comes from the scanner-recorded ``files.page_count``;
    when that's missing (a file scanned before page counts) the archive
    is opened once to count. A count of 0 renders an "unreadable" state
    rather than a broken viewer.

    ``?preview=1`` is the link convention review surfaces (review queue
    thumbnails, /admin/duplicates, etc.) use to open the reader without
    polluting the user's reading history: progress tracking is
    suppressed and resume-from-saved is skipped, so an admin peeking
    at a 3-page sketch variant doesn't end up with that file on their
    "Continue reading" shelf. The per-user ``track_reading_progress``
    toggle still applies — preview mode is an additional, per-link
    opt-out that doesn't override the user's own pause.
    """
    file_row = await db.get(File, file_id)
    if file_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such file.",
        )
    location = await _current_location(db, file_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This file has no current on-disk location.",
        )

    page_count = file_row.page_count or 0
    if not page_count:
        backend = await get_archive_backend(db)
        try:
            page_count = await asyncio.to_thread(
                _page_count, location.path, backend
            )
        except (ArchiveError, UnsupportedArchiveError, OSError) as e:
            logger.warning(
                "read_file: page count failed for %s: %s", location.path, e
            )
            page_count = 0

    # Reading direction is a per-volume setting (manga reads RTL); the
    # reader opens with whatever the file's volume last had. An
    # unmatched file has no volume, so this is the plain default.
    reading_direction = await get_reading_direction(db, file_id)

    # Resume where this user left off. ``page`` is 0-based; clamp it to
    # the current page count in case the archive changed since.
    # Preview links bypass resume — we want the reader to open from the
    # cover, since the admin is sampling content, not continuing a read.
    if preview:
        start_page = 0
    else:
        progress = await get_read_progress(db, user.id, file_id)
        start_page = 0
        if progress is not None and page_count:
            start_page = min(max(progress.page, 0), page_count - 1)

    # ``track_progress=False`` makes the reader's JS skip the progress
    # POSTs entirely (the saveProgress() handler short-circuits on the
    # ``track`` flag — see read.html). Preview mode forces it off
    # regardless of the user's own setting; otherwise we honour the
    # per-user pause.
    track_progress = (not preview) and user.track_reading_progress

    return templates.TemplateResponse(
        request,
        "read.html",
        {
            "user": user,
            "file_id": file_id,
            "title": Path(location.path).name,
            "page_count": page_count,
            "reading_direction": reading_direction,
            "start_page": start_page,
            "track_progress": track_progress,
        },
    )


@router.get("/read/{file_id}/page/{index}")
async def read_page(
    user: RequireUserDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    index: int,
):
    """Stream one page image (0-based ``index``) as a raw image response.

    Any failure — missing file, unreadable archive, index out of range —
    returns 404, so a stale ``<img>`` in the viewer just shows a broken-
    image icon rather than blowing up the response.
    """
    location = await _current_location(db, file_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No current location for this file.",
        )
    backend = await get_archive_backend(db)
    try:
        # Sync archive work goes through ``to_thread`` so the event loop
        # stays free while ``unar`` / pymupdf does its thing.
        data, content_type = await asyncio.to_thread(
            _read_page, location.path, backend, index
        )
    except (ArchiveError, UnsupportedArchiveError, OSError) as e:
        logger.warning(
            "read_page failed for %s page %d: %s", location.path, index, e
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Couldn't read page {index}.",
        ) from e

    # ``private`` keeps shared caches out of the loop; ``max-age`` lets
    # the browser memoise pages so flipping back and forth is instant.
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/read/{file_id}/direction/{direction}")
async def set_read_direction(
    user: RequireUserDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    direction: str,
):
    """Persist the reader's left-to-right / right-to-left choice.

    Direction is stored on the *volume* the file belongs to, so every
    issue of a series shares it (manga reads RTL). A file with no
    resolved volume — an unmatched file — has nowhere to store it; the
    response reports that via ``persisted: false`` and the reader just
    keeps the choice for the current session.
    """
    if direction not in READING_DIRECTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"direction must be one of {READING_DIRECTIONS}",
        )
    persisted = await set_reading_direction(db, file_id, direction)
    return {"direction": direction, "persisted": persisted}


@router.post("/read/{file_id}/progress/{page}")
async def save_read_progress_route(
    user: RequireUserDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
    page: int,
    total: int,
):
    """Persist this user's current page in the file.

    ``page`` is the 0-based page index; ``total`` is the archive's page
    count as the reader sees it. Reaching the last page stamps the file
    finished. The reader fires this fire-and-forget — debounced, with a
    ``sendBeacon`` flush on unload — so it just 404s an unknown file and
    otherwise returns quietly.
    """
    if page < 0 or total < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page and total must be non-negative",
        )
    # Incognito — the user has tracking paused; accept and skip the write.
    if not user.track_reading_progress:
        return {"ok": True, "tracked": False}
    if await db.get(File, file_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such file."
        )
    # Unmatched files have no issue / local-issue / volume page where
    # the user could reach a reset-progress button. Tracking progress
    # there would strand them with a stale "Continue reading" entry
    # that has no escape hatch — so we no-op the save and surface
    # ``tracked: False``. The reader's JS already treats that as a
    # quiet skip (no toast, no banner).
    if not await is_file_match_resolved(db, file_id):
        return {"ok": True, "tracked": False, "reason": "unmatched"}
    await save_read_progress(db, user.id, file_id, page, total)
    return {"ok": True, "tracked": True}


@router.post("/read/{file_id}/reset-progress")
async def reset_read_progress_route(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
    file_id: uuid.UUID,
):
    """Clear this user's saved reading position for a file — the
    "Reset reading progress" button on the issue pages. Redirects back
    to wherever it was clicked.
    """
    await reset_read_progress(db, user.id, file_id)
    return RedirectResponse(
        request.headers.get("referer") or "/",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/reading/tracking")
async def toggle_reading_tracking(
    request: Request,
    user: RequireUserDep,
    db: DbSessionDep,
):
    """Flip this user's reading-progress tracking on or off — the header
    toggle ("incognito reading").

    With tracking off the reader stops recording progress; existing
    progress and the home reading lists are left as they are. Redirects
    back to the page the toggle was clicked from.
    """
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(track_reading_progress=not user.track_reading_progress)
    )
    await db.commit()
    return RedirectResponse(
        request.headers.get("referer") or "/",
        status_code=status.HTTP_303_SEE_OTHER,
    )
