"""Typed wrapper around the ``app_settings`` JSONB key/value store.

The general read/write helpers (``get_setting``, ``set_setting``) are
JSON-typed and used by both the app code and any future admin-edit endpoint.
The typed accessors (``get_library_paths``, ``get_scan_interval_seconds``)
encode the contract: which keys exist, what their value shape is, and what
to do when they're unset.

``seed_defaults`` is called once at process startup (from both the FastAPI
lifespan and the RQ worker entrypoint). It populates rows that should exist
on a fresh install but only if they're missing — it never overwrites an
existing value. The env-var seed for ``library_paths`` flows through here:
if the DB row is unset AND ``settings.library_paths`` is non-empty, the env
value is written into the DB row, after which the env var is ignored.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as env_settings
from app.models import AppSetting

logger = logging.getLogger("longboxes.services.settings")

# ---- Keys ---------------------------------------------------------------

LIBRARY_PATHS = "library_paths"
SCAN_INTERVAL_SECONDS = "scan_interval_seconds"
COMICVINE_API_KEY = "comicvine_api_key"
CV_TTL_OVERRIDES = "cv_ttl_overrides"
PAGE_SIZE = "page_size"
ARCHIVE_BACKEND = "archive_backend"

# ---- Defaults -----------------------------------------------------------

DEFAULT_SCAN_INTERVAL_SECONDS = 3600  # 1 hour, per design §9 scanner
DEFAULT_PAGE_SIZE = 15  # initial pagination window — library, publisher arcs,
                       # volume issues, arc issues all use the same value
# Clamp the admin form's input to a sane range. Too-small windows turn into
# excessive pagination clicks; too-large windows blast the CV rate limiter
# on first-load hydration.
MIN_PAGE_SIZE = 5
MAX_PAGE_SIZE = 100

# Archive backend options. "comicbox" routes archive opens through the
# ``comicbox`` library — broader format support (CBT, PDF), proper
# page sort order, MetronInfo extraction. "stdlib" uses the original
# ``zipfile``/``rarfile``-based readers in ``app/archives/{cbz,cbr}.py``.
# Default is "comicbox" once it's available; admins can flip back to
# "stdlib" via /admin if comicbox surprises us on a specific archive.
ARCHIVE_BACKEND_COMICBOX = "comicbox"
ARCHIVE_BACKEND_STDLIB = "stdlib"
ARCHIVE_BACKENDS = (ARCHIVE_BACKEND_COMICBOX, ARCHIVE_BACKEND_STDLIB)
DEFAULT_ARCHIVE_BACKEND = ARCHIVE_BACKEND_COMICBOX


# ---- Generic JSON accessors --------------------------------------------


async def get_setting(db: AsyncSession, key: str) -> Any:
    """Return the JSON value for ``key`` or ``None`` if not set."""
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row is not None else None


async def set_setting(db: AsyncSession, key: str, value: Any) -> None:
    """Upsert an ``app_settings`` row. Caller is responsible for ``commit()``."""
    stmt = (
        pg_insert(AppSetting)
        .values(key=key, value=value, updated_at=datetime.now(tz=UTC))
        .on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": value, "updated_at": datetime.now(tz=UTC)},
        )
    )
    await db.execute(stmt)


# ---- Typed accessors ----------------------------------------------------


async def get_library_paths(db: AsyncSession) -> list[str]:
    """Library paths the scanner should walk. Always returns a list (possibly empty)."""
    value = await get_setting(db, LIBRARY_PATHS)
    if value is None:
        return []
    if not isinstance(value, list):
        # Defensive: someone hand-edited the row to a non-list value. Treat as
        # unconfigured rather than crashing the scanner.
        logger.warning(
            "app_settings.%s is %s, expected list; treating as empty",
            LIBRARY_PATHS,
            type(value).__name__,
        )
        return []
    return [str(p) for p in value]


async def set_library_paths(db: AsyncSession, paths: list[str]) -> None:
    """Replace the library_paths row. Caller commits."""
    await set_setting(db, LIBRARY_PATHS, list(paths))


async def get_scan_interval_seconds(db: AsyncSession) -> int:
    value = await get_setting(db, SCAN_INTERVAL_SECONDS)
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_SCAN_INTERVAL_SECONDS


async def get_cv_api_key(db: AsyncSession) -> str | None:
    """Return the ComicVine API key, or None if not configured.

    Stored as a plain JSON string in ``app_settings``. Admins paste it via
    the admin UI; we never read it from env (the key is per-deployment and
    deserves UI-driven rotation).
    """
    value = await get_setting(db, COMICVINE_API_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def set_cv_api_key(db: AsyncSession, key: str) -> None:
    """Set (or replace) the ComicVine API key. Caller commits."""
    await set_setting(db, COMICVINE_API_KEY, key.strip())


def redact_cv_api_key(key: str | None) -> str:
    """Format a CV API key for display. Shows the last 4 characters only."""
    if not key:
        return "(not configured)"
    if len(key) <= 4:
        return "••••"
    return f"•••• {key[-4:]}"


async def is_cv_configured(db: AsyncSession) -> bool:
    return (await get_cv_api_key(db)) is not None


async def get_page_size(db: AsyncSession) -> int:
    """Initial pagination window for browse pages.

    One value shared across library, publisher arcs, volume issues,
    and arc issues — keeps the load-more / hydration burst feel
    consistent regardless of which entity the user is paging
    through. Falls back to ``DEFAULT_PAGE_SIZE`` when unset or set
    to a bogus value (defensive guard against a hand-edited
    ``app_settings`` row)."""
    value = await get_setting(db, PAGE_SIZE)
    if isinstance(value, int) and MIN_PAGE_SIZE <= value <= MAX_PAGE_SIZE:
        return value
    return DEFAULT_PAGE_SIZE


async def set_page_size(db: AsyncSession, value: int) -> None:
    """Set the page-size setting. Caller commits. Clamped to
    ``MIN_PAGE_SIZE`` / ``MAX_PAGE_SIZE`` so the admin form can't
    push an absurd value into the DB. Caller should validate the
    raw input separately if it wants to surface "value out of
    range" feedback to the user; this just hard-clamps."""
    clamped = max(MIN_PAGE_SIZE, min(MAX_PAGE_SIZE, int(value)))
    await set_setting(db, PAGE_SIZE, clamped)


async def get_archive_backend(db: AsyncSession) -> str:
    """Return the configured archive backend.

    One of ``ARCHIVE_BACKEND_COMICBOX`` (default) or
    ``ARCHIVE_BACKEND_STDLIB``. Anything else (typo, hand-edited
    row, etc.) falls back to the default so the app stays usable
    even with a broken setting."""
    value = await get_setting(db, ARCHIVE_BACKEND)
    if value in ARCHIVE_BACKENDS:
        return value  # type: ignore[return-value]
    return DEFAULT_ARCHIVE_BACKEND


async def set_archive_backend(db: AsyncSession, backend: str) -> None:
    """Set the archive backend. Caller commits. Raises if the value
    isn't one of the known backends — the admin form should pick
    from a hardcoded list, but the guard keeps the DB clean if
    someone POSTs directly."""
    if backend not in ARCHIVE_BACKENDS:
        raise ValueError(
            f"unknown archive backend {backend!r}; "
            f"expected one of {ARCHIVE_BACKENDS}"
        )
    await set_setting(db, ARCHIVE_BACKEND, backend)


async def get_cv_ttl_overrides(db: AsyncSession) -> dict[str, int]:
    """Per-entity TTL overrides keyed by entity name. Empty dict if unset.

    Keys correspond to the entity names from §8 (volume, issue, person, ...).
    Values are integer seconds. The cache layer falls back to its built-in
    defaults for anything not in this dict.
    """
    value = await get_setting(db, CV_TTL_OVERRIDES)
    if isinstance(value, dict):
        return {k: int(v) for k, v in value.items() if isinstance(v, int)}
    return {}


# ---- Bootstrap ----------------------------------------------------------


async def seed_defaults(db: AsyncSession) -> None:
    """Idempotent first-boot seeding. Safe to call on every process start.

    - If ``app_settings.library_paths`` is unset AND ``LIBRARY_PATHS`` env is
      non-empty, write the env value into the DB row. Afterwards, edits to
      the env var are ignored — the DB is authoritative.
    - If ``app_settings.scan_interval_seconds`` is unset, write the default.
    """
    existing_paths = await get_setting(db, LIBRARY_PATHS)
    if existing_paths is None and env_settings.library_paths:
        logger.info(
            "Seeding library_paths from LIBRARY_PATHS env: %r",
            env_settings.library_paths,
        )
        await set_library_paths(db, env_settings.library_paths)

    existing_interval = await get_setting(db, SCAN_INTERVAL_SECONDS)
    if existing_interval is None:
        await set_setting(db, SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)

    existing_page_size = await get_setting(db, PAGE_SIZE)
    if existing_page_size is None:
        await set_setting(db, PAGE_SIZE, DEFAULT_PAGE_SIZE)

    existing_archive_backend = await get_setting(db, ARCHIVE_BACKEND)
    if existing_archive_backend is None:
        await set_setting(db, ARCHIVE_BACKEND, DEFAULT_ARCHIVE_BACKEND)

    await db.commit()
