"""Application configuration via pydantic-settings.

Settings are read from environment variables (and optionally from a `.env`
file at the project root). Field names are case-insensitive.
"""

import json
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database — async for app, sync for Alembic
    database_url: str = "postgresql+asyncpg://longboxes:longboxes@db:5432/longboxes"
    database_url_sync: str = "postgresql+psycopg://longboxes:longboxes@db:5432/longboxes"

    # Redis (cache + RQ broker)
    redis_url: str = "redis://redis:6379/0"

    # App
    app_env: str = "dev"
    log_level: str = "INFO"

    # RQ worker queue list. Comma-separated, ordered by priority — RQ
    # drains the first listed queue before checking the next. Defaults
    # to the match lane (``default``). docker-compose wires two more
    # workers: ``worker-interactive`` sets ``WORKER_QUEUES=interactive``
    # so browse-triggered hydration runs unblocked, and ``worker-scan``
    # sets ``WORKER_QUEUES=scan`` so the recurring library walk runs on
    # its own process. See ``app/worker.py`` for the parse + listener
    # wiring.
    worker_queues: str = "default"

    # Sessions
    session_cookie_name: str = "longboxes_session"
    session_ttl_days: int = 30
    # Set true behind HTTPS (production). Browsers reject Secure cookies on plain http://.
    session_cookie_secure: bool = False
    # 'lax' is the right default for cookie-session web apps; only consider 'strict'
    # if you want to drop session on cross-site GETs (which breaks bookmarks).
    session_cookie_samesite: str = "lax"

    # Library — seeds app_settings.library_paths on first boot if that DB row
    # is unset. Thereafter the DB row is authoritative and can be edited via
    # the admin UI; this env var is only consulted when the DB row is missing.
    # Accepts comma-separated paths ("/library/a,/library/b") or a JSON array
    # ('["/library/a", "/library/b"]'). Empty/unset means "no seed."
    #
    # ``NoDecode`` is critical: pydantic-settings would otherwise treat a
    # ``list[str]`` field as "complex" and try to ``json.loads`` the env value
    # *before* invoking field_validator(mode="before"). With NoDecode the raw
    # string is handed straight to ``_parse_library_paths``, which accepts
    # either CSV or JSON.
    library_paths: Annotated[list[str], NoDecode] = []

    @field_validator("library_paths", mode="before")
    @classmethod
    def _parse_library_paths(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            # JSON array form takes precedence; falls through to CSV otherwise.
            if v.startswith("["):
                return json.loads(v)
            return [p.strip() for p in v.split(",") if p.strip()]
        return v


settings = Settings()
