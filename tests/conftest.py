"""Shared pytest fixtures.

Tests run inside the web container (``just test``) and use a dedicated
``longboxes_test`` database on the same Postgres instance that powers the
app. Redis is replaced with ``fakeredis`` per-test, so session state is
fresh and isolated.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.dependencies import get_db, get_redis_dep
from app.main import app
from app.models import Base

# Default points at the docker-compose Postgres; override via env if running
# the suite somewhere else.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://longboxes:longboxes@db:5432/longboxes_test",
)
TEST_DATABASE_URL_ADMIN = os.environ.get(
    "TEST_DATABASE_URL_ADMIN",
    "postgresql://longboxes:longboxes@db:5432/longboxes",
)


async def _ensure_test_db_exists() -> None:
    """Create the ``longboxes_test`` database if it doesn't already exist.

    We connect to the app's regular database (which is guaranteed to exist by
    docker-compose) and use it as a maintenance connection.
    """
    # Parse URL to get target db name.
    # postgresql+asyncpg://user:pass@host:port/dbname
    target_db = TEST_DATABASE_URL.rsplit("/", 1)[-1]
    conn = await asyncpg.connect(TEST_DATABASE_URL_ADMIN)
    try:
        existing = await conn.fetchrow("SELECT 1 FROM pg_database WHERE datname = $1", target_db)
        if not existing:
            # CREATE DATABASE can't run inside a transaction.
            await conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator:
    """Session-scoped engine pointing at the test DB. Schema is created once.

    ``poolclass=NullPool`` disables connection pooling: each session opens
    a fresh asyncpg connection and disposes of it on close. That trades a
    small per-test latency cost for hard isolation — no connection ever
    sees concurrent operations from two coroutines, eliminating asyncpg's
    "another operation is in progress" failure mode.
    """
    await _ensure_test_db_exists()
    eng = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    # Build the schema directly from metadata; we don't need Alembic history
    # in tests, only the resulting tables. This keeps tests independent of
    # migration ordering.
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    """Per-test DB session. Truncates the users table beforehand so each test
    starts from a known empty state."""
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with SessionMaker() as session:
        await session.execute(
            text(
                "TRUNCATE users, file_matches, file_locations, file_errors, "
                "files, app_settings, cv_search_cache, cv_issues, cv_volumes, "
                "cv_publishers, cv_persons, cv_characters, cv_story_arcs, "
                "cv_teams, local_volumes, local_issues, read_progress "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    async with SessionMaker() as session:
        yield session


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator:
    """Per-test fakeredis instance — sessions don't leak between tests."""
    r = FakeAsyncRedis(decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


@pytest_asyncio.fixture
async def client(engine, fake_redis) -> AsyncIterator[AsyncClient]:
    """HTTPX async client wired to the FastAPI app via ASGITransport.

    Overrides the ``get_db`` and ``get_redis_dep`` dependencies so the app
    talks to the test DB and the fakeredis instance for the duration of
    the test.
    """
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with SessionMaker() as session:
            yield session

    def _override_get_redis():
        return fake_redis

    # Truncate before each test for isolation.
    async with SessionMaker() as session:
        await session.execute(
            text(
                "TRUNCATE users, file_matches, file_locations, file_errors, "
                "files, app_settings, cv_search_cache, cv_issues, cv_volumes, "
                "cv_publishers, cv_persons, cv_characters, cv_story_arcs, "
                "cv_teams, local_volumes, local_issues, read_progress "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis_dep] = _override_get_redis
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
