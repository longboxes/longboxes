"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.admin.routes import router as admin_router
from app.auth.dependencies import DbSessionDep, RedirectSignal, RequireUserDep
from app.auth.routes import router as auth_router
from app.config import settings
from app.db import SessionLocal
from app.library_browse.routes import router as library_router
from app.reader.routes import router as reader_router
from app.redis_client import close_redis
from app.review.routes import router as review_router
from app.search.routes import router as search_router
from app.services.library import list_recently_added
from app.services.reader import list_continue_reading, list_recently_read
from app.services.settings import seed_defaults
from app.templates_env import templates

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("longboxes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Longboxes starting (env=%s)", settings.app_env)
    # Seed defaults (library_paths from env, scan interval, etc.). Idempotent —
    # the worker calls this too; whichever boots first wins.
    try:
        async with SessionLocal() as db:
            await seed_defaults(db)
    except Exception:
        # Don't crash the web process on startup if seeding fails — log and
        # continue. The worker will retry on its own boot.
        logger.exception("seed_defaults failed during web startup; continuing")
    yield
    await close_redis()
    logger.info("Longboxes shutting down")


app = FastAPI(
    title="Longboxes",
    description="ComicVine-native, self-hosted comic library manager",
    version="0.0.2",
    lifespan=lifespan,
)


@app.exception_handler(RedirectSignal)
async def _redirect_exception_handler(request: Request, exc: RedirectSignal):
    """Translate a dependency-raised ``RedirectSignal`` into a 303 response."""
    return RedirectResponse(url=exc.location, status_code=303)


# Static assets — third-party logos / favicons we cache locally so we
# don't hotlink. Served at /static/* (e.g., /static/comicvine-icon.png).
# ``mkdir(exist_ok=True)`` keeps the mount from blowing up app startup in
# environments where the directory hasn't been created yet (fresh clone,
# CI, container rebuilds before the asset is downloaded).
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    "/static",
    StaticFiles(directory=str(_STATIC_DIR), check_dir=False),
    name="static",
)


# Favicon assets — RealFaviconGenerator output. Conventionally served
# at root paths (``/favicon.ico``, ``/site.webmanifest``, etc.) because
# (a) browsers probe ``/favicon.ico`` automatically and (b) the
# ``site.webmanifest`` references the 192/512 PNGs by root paths too,
# so moving them under ``/static/`` would require post-processing the
# generated manifest. The actual files live under
# ``app/static/favicons/`` to keep ``app/static/`` tidy. The user can
# regenerate from realfavicongenerator.net and drop the files in
# without touching this code.
_FAVICONS_DIR = _STATIC_DIR / "favicons"
_FAVICONS_DIR.mkdir(parents=True, exist_ok=True)


def _favicon_response(filename: str, media_type: str | None = None) -> FileResponse:
    """Serve one favicon asset from ``app/static/favicons/``.

    404s when the file isn't present yet (e.g., fresh clone before the
    user has dropped in the RealFaviconGenerator output) — browsers
    handle a missing favicon gracefully so this won't crash any page.
    """
    path = _FAVICONS_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(str(path), media_type=media_type)


@app.get("/favicon.ico", include_in_schema=False)
async def _favicon_ico():
    return _favicon_response("favicon.ico", media_type="image/x-icon")


@app.get("/favicon.svg", include_in_schema=False)
async def _favicon_svg():
    return _favicon_response("favicon.svg", media_type="image/svg+xml")


@app.get("/favicon-96x96.png", include_in_schema=False)
async def _favicon_96():
    return _favicon_response("favicon-96x96.png", media_type="image/png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
async def _apple_touch_icon():
    return _favicon_response("apple-touch-icon.png", media_type="image/png")


@app.get("/web-app-manifest-192x192.png", include_in_schema=False)
async def _manifest_192():
    return _favicon_response("web-app-manifest-192x192.png", media_type="image/png")


@app.get("/web-app-manifest-512x512.png", include_in_schema=False)
async def _manifest_512():
    return _favicon_response("web-app-manifest-512x512.png", media_type="image/png")


@app.get("/site.webmanifest", include_in_schema=False)
async def _site_webmanifest():
    return _favicon_response("site.webmanifest", media_type="application/manifest+json")


# Auth routes (login, logout, setup) are registered without prefix.
app.include_router(auth_router)
# Admin routes live under /admin and require the admin role.
app.include_router(admin_router)
# Library browse — /library, /volume/{cv_id}, /issue/{cv_id}.
app.include_router(library_router)
# Review queue — /review and friends, admin-only.
app.include_router(review_router)
# Reader — /read/{file_id} page-by-page viewer (Phase 6).
app.include_router(reader_router)
# Global search — header dropdown + /search results page.
app.include_router(search_router)


@app.get("/")
async def home(request: Request, user: RequireUserDep, db: DbSessionDep):
    """Authenticated home — recently added, plus this user's in-progress
    and recently finished reading (Phase 6)."""
    recently_added = await list_recently_added(db, limit=12)
    continue_reading = await list_continue_reading(db, user.id, limit=12)
    recently_read = await list_recently_read(db, user.id, limit=12)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "recently_added": recently_added,
            "continue_reading": continue_reading,
            "recently_read": recently_read,
        },
    )


@app.get("/health")
async def health() -> dict:
    """Unauthenticated health check — used by Docker / load balancers."""
    return {"status": "ok", "env": settings.app_env}
