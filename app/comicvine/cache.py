"""ComicVine cache-aside layer with stale-while-revalidate.

For every CV read the flow is:

1. Look up the cached row (``cv_*`` table) by ``cv_id`` (or by ``request_key``
   for search).
2. Cache hit, fresh (``fetched_at + TTL > now``) → return immediately.
3. Cache hit, stale → return cached, enqueue a revalidation job.
4. Cache miss → fetch from CV, persist, return.

This module owns the *cache* tables (``cv_*``). The client below it owns the
HTTP. Routes call into this layer; the client is used directly only by this
layer and by tests.

Volume fetches additionally upsert stub ``cv_issues`` rows for each entry in
the volume's nested issue list (§8 "nested payload exploitation"). Stub rows
have ``raw_payload`` and ``fetched_at`` NULL; they get hydrated when someone
opens that specific issue.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.comicvine.client import ComicVineClient
from app.models import (
    CvCharacter,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvSearchCache,
    CvStoryArc,
    CvTeam,
    CvVolume,
)
from app.services.cv_helpers import safe_int
from app.services.settings import get_cv_ttl_overrides

logger = logging.getLogger("longboxes.comicvine.cache")

# Default TTLs in seconds, per §8 of the design doc. Per-entity overrides
# can be set in ``app_settings.cv_ttl_overrides`` (dict keyed by entity).
DEFAULT_TTL_SECONDS: dict[str, int] = {
    "volume": 7 * 24 * 3600,
    "issue": 7 * 24 * 3600,
    "publisher": 30 * 24 * 3600,
    "person": 30 * 24 * 3600,
    "character": 30 * 24 * 3600,
    "story_arc": 7 * 24 * 3600,
    "team": 30 * 24 * 3600,
    "search": 3600,
}


EnqueueRevalidateFn = Callable[[str, int], None]


def _noop_enqueue(entity: str, cv_id: int) -> None:
    """Default revalidation enqueue — used by tests that don't care about SWR."""


# ---- Cache ---------------------------------------------------------------


class ComicVineCache:
    def __init__(
        self,
        client: ComicVineClient,
        *,
        enqueue_revalidate: EnqueueRevalidateFn | None = None,
    ) -> None:
        self._client = client
        self._enqueue_revalidate = enqueue_revalidate or _noop_enqueue

    def notify_match_winner(self, volume_cv_id: int) -> None:
        """Called by the matcher when it commits a successful match.

        Enqueues the cheap one-call-per-volume bulk ``volume_issues``
        hydration for the winning volume. ``_upsert_volume`` used to
        do this on every first-touch, including for the four Stage 3
        candidates the matcher then rejected. Moving the enqueue here
        means only winners spawn hydration; losing candidates leave
        no trail.

        Idempotent via the deterministic job id in
        ``enqueue_revalidate``: a re-match of an already-hydrated
        volume costs only the Redis ping. Safe to call from inside
        the matcher's session without any extra orchestration."""
        self._enqueue_revalidate("volume_issues", volume_cv_id)

    # ---- Public reads ---------------------------------------------------

    async def get_volume(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvVolume:
        existing = await db.get(CvVolume, cv_id)
        ttl = await self._ttl_seconds(db, "volume")
        # A "stub" row was inserted by ``_upsert_volume_stub`` to keep an FK
        # valid; it has ``_stub: True`` in raw_payload and no issue list. Treat
        # stubs as cache misses (symmetric to the stub-issue handling above)
        # so callers that need the full volume don't silently get a stub back.
        is_stub = (
            existing is not None
            and isinstance(existing.raw_payload, dict)
            and existing.raw_payload.get("_stub") is True
        )
        if existing is not None and not is_stub and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            # Stale row — only fire SWR if there's a real freshness
            # win to be had. A row that already carries an ``image``
            # payload is fully displayable on the browse pages we're
            # rendering from; firing per-volume revalidates on every
            # character / creator / team page load otherwise piles up
            # the interactive queue (50-100 jobs per page, each
            # ~20s on the pacer) for data that almost never
            # meaningfully changes between fetches. The stub branch
            # and the bulk_hydrate_volumes path still cover the
            # cases where image data is genuinely missing or needs
            # to be force-refreshed.
            if not _has_image_payload(existing):
                self._enqueue_revalidate("volume", cv_id)
            return existing
        payload = await self._client.get_volume(db, cv_id)
        return await self._upsert_volume(db, payload)

    async def get_issue(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvIssue:
        existing = await db.get(CvIssue, cv_id)
        ttl = await self._ttl_seconds(db, "issue")
        # Issues have two "needs hydration" states:
        #   1. ``fetched_at IS NULL`` — true stub (from a volume's
        #      nested issues list, before anything's been fetched
        #      individually).
        #   2. ``raw_payload._bulk_hydrated`` — the row came from the
        #      bulk ``/issues/?filter=volume:<id>`` walk, which gives
        #      us cover + name + date but NOT story_arc_credits /
        #      person_credits / character_credits. The issue page
        #      needs those, so we treat bulk-hydrated rows the same
        #      as stubs at fetch time — refetch via ``/issue/<id>/``
        #      to upgrade to a full payload.
        is_bulk = (
            existing is not None
            and isinstance(existing.raw_payload, dict)
            and existing.raw_payload.get("_bulk_hydrated") is True
        )
        if (
            existing is not None
            and existing.fetched_at is not None
            and not is_bulk
            and not force_refresh
        ):
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            self._enqueue_revalidate("issue", cv_id)
            return existing
        payload = await self._client.get_issue(db, cv_id)
        return await self._upsert_issue(db, payload)

    async def get_publisher(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvPublisher:
        """Fetch a publisher. Same SWR pattern as get_volume/get_issue.

        Publishers usually arrive as thin stubs from ``_upsert_publisher_stub``
        when a volume is fetched — they have id/name only and ``_stub: True``
        in raw_payload. Routes that need richer data (image, location, etc.)
        call this; the cache detects the stub and re-fetches just like it
        does for stub volumes."""
        existing = await db.get(CvPublisher, cv_id)
        ttl = await self._ttl_seconds(db, "publisher")
        is_stub = (
            existing is not None
            and isinstance(existing.raw_payload, dict)
            and existing.raw_payload.get("_stub") is True
        )
        if existing is not None and not is_stub and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            self._enqueue_revalidate("publisher", cv_id)
            return existing
        payload = await self._client.get_publisher(db, cv_id)
        return await self._upsert_publisher(db, payload)

    async def get_story_arc(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvStoryArc:
        """Fetch a story arc. Same SWR pattern as get_volume/get_issue.

        Story arcs are valuable because their ``raw_payload.issues`` lists
        every member issue across every volume — so one fetch can populate
        arc membership for an entire small-volume's worth of issues without
        per-issue hydration (see ``get_volume_detail``).
        """
        existing = await db.get(CvStoryArc, cv_id)
        ttl = await self._ttl_seconds(db, "story_arc")
        if existing is not None and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            self._enqueue_revalidate("story_arc", cv_id)
            return existing
        payload = await self._client.get_story_arc(db, cv_id)
        return await self._upsert_story_arc(db, payload)

    async def get_character(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvCharacter:
        """Fetch a character. Same SWR pattern as get_story_arc.

        A character's ``raw_payload.issue_credits`` lists every issue it
        appears in — the character page aggregates that into an owned /
        missing appearance list.
        """
        existing = await db.get(CvCharacter, cv_id)
        ttl = await self._ttl_seconds(db, "character")
        if existing is not None and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            self._enqueue_revalidate("character", cv_id)
            return existing
        payload = await self._client.get_character(db, cv_id)
        return await self._upsert_character(db, payload)

    async def get_person(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvPerson:
        """Fetch a creator/person. Same SWR pattern as get_story_arc.

        A person's ``raw_payload.issue_credits`` lists every issue they
        are credited on — the creator page groups that by series.
        """
        existing = await db.get(CvPerson, cv_id)
        ttl = await self._ttl_seconds(db, "person")
        if existing is not None and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            self._enqueue_revalidate("person", cv_id)
            return existing
        payload = await self._client.get_person(db, cv_id)
        return await self._upsert_person(db, payload)

    async def get_team(
        self,
        db: AsyncSession,
        cv_id: int,
        *,
        force_refresh: bool = False,
    ) -> CvTeam:
        """Fetch a team. Same SWR pattern as get_story_arc.

        A team's ``raw_payload.characters`` lists its members — the
        team page renders that as a paginated portrait grid.
        """
        existing = await db.get(CvTeam, cv_id)
        ttl = await self._ttl_seconds(db, "team")
        if existing is not None and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing
            self._enqueue_revalidate("team", cv_id)
            return existing
        payload = await self._client.get_team(db, cv_id)
        return await self._upsert_team(db, payload)

    async def hydrate_volume_issues(
        self,
        db: AsyncSession,
        volume_cv_id: int,
        *,
        force_refresh: bool = True,
    ) -> int:
        """Bulk-hydrate every issue in a volume via the
        ``/issues/?filter=volume:<id>`` endpoint.

        One paginated API call replaces N per-issue ``get_issue``
        round-trips when a fresh volume needs all its stub rows
        upgraded — for a 904-issue title that's ~10 calls instead of
        904. Each upserted row carries a ``_bulk_hydrated: True``
        marker inside ``raw_payload`` because the bulk endpoint
        returns a *subset* of fields: it gives us name / issue_number
        / cover_date / image / volume — but NOT
        ``story_arc_credits`` / ``person_credits`` /
        ``character_credits`` / ``description``. The marker lets
        ``get_issue`` treat these rows as still-needing the full
        per-issue fetch the next time the user opens the issue page;
        in the meantime the volume page's covers and dates are fully
        populated from the cheap bulk path.

        Rows that were previously hydrated through ``/issue/<id>/``
        (i.e., have full credits and no bulk marker) are LEFT ALONE
        — overwriting them would blow away the very arc/character
        data the volume page relies on for stripes and arrows.

        Returns the count of upserted issue rows.
        """
        offset = 0
        page_size = 100
        upserted = 0
        # Volume cv_ids we've already ensured an FK-target row for. The
        # per-issue stub-upsert below consults this so it fires once per
        # distinct volume rather than once per issue.
        seen_volume_ids: set[int] = set()
        while True:
            envelope = await self._client.list_issues_by_volume(
                db,
                volume_cv_id,
                offset=offset,
                limit=page_size,
            )
            results = envelope.get("results") or []
            if not isinstance(results, list):
                break
            now = datetime.now(tz=UTC)
            for issue_payload in results:
                if not isinstance(issue_payload, dict):
                    continue
                if not issue_payload.get("id"):
                    continue
                cv_id = int(issue_payload["id"])

                # Decide whether this row is safe to overwrite. A
                # stub (``fetched_at IS NULL``) is fair game — it has
                # at best a thin nested-payload dict from the volume
                # fetch. A previously bulk-hydrated row is also fair
                # game — we're just refreshing the same field set.
                # But a fully-hydrated row (per-issue fetched, no
                # ``_bulk_hydrated`` marker) carries full credits we
                # MUST NOT clobber.
                existing = await db.get(CvIssue, cv_id)
                if existing is not None and existing.fetched_at is not None:
                    raw = existing.raw_payload or {}
                    is_bulk = isinstance(raw, dict) and raw.get("_bulk_hydrated") is True
                    if not is_bulk:
                        continue

                # Don't drift volume_cv_id — the filter guarantees
                # they all belong to ``volume_cv_id``, but the
                # payload's own ``volume.id`` is the source of truth.
                nested_volume = issue_payload.get("volume") or {}
                this_vol_id = (
                    int(nested_volume["id"])
                    if nested_volume.get("id") is not None
                    else volume_cv_id
                )
                # Guarantee the FK target exists before inserting the
                # issue. The bulk ``/issues/?filter=volume:<id>`` endpoint
                # can hand back an issue whose own ``volume.id`` differs
                # from the volume we're hydrating (CV volume merges /
                # renumbers), and that other volume need not be cached —
                # inserting against it would trip
                # ``cv_issues_volume_cv_id_fkey``. Mirror
                # ``_upsert_issue``'s stub-the-volume-first pattern; the
                # ``on_conflict_do_nothing`` inside ``_upsert_volume_stub``
                # makes it a no-op when the row already exists.
                if this_vol_id not in seen_volume_ids:
                    await self._upsert_volume_stub(
                        db, this_vol_id, nested_volume.get("name") or "(unknown)"
                    )
                    seen_volume_ids.add(this_vol_id)
                tagged_payload = {**issue_payload, "_bulk_hydrated": True}
                stmt = (
                    pg_insert(CvIssue)
                    .values(
                        cv_id=cv_id,
                        volume_cv_id=this_vol_id,
                        issue_number=issue_payload.get("issue_number"),
                        cover_date=_safe_date(issue_payload.get("cover_date")),
                        name=issue_payload.get("name"),
                        raw_payload=tagged_payload,
                        fetched_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[CvIssue.cv_id],
                        set_={
                            "volume_cv_id": this_vol_id,
                            "issue_number": issue_payload.get("issue_number"),
                            "cover_date": _safe_date(issue_payload.get("cover_date")),
                            "name": issue_payload.get("name"),
                            "raw_payload": tagged_payload,
                            "fetched_at": now,
                        },
                    )
                )
                await db.execute(stmt)
                upserted += 1
            await db.commit()
            # Pagination — keep walking until CV reports we've
            # consumed everything (the page returned fewer than
            # requested, or the running total reached the reported
            # ``number_of_total_results``).
            page_count = int(envelope.get("number_of_page_results") or 0)
            total = int(envelope.get("number_of_total_results") or 0)
            if page_count == 0:
                break
            offset += page_count
            if offset >= total:
                break
        return upserted

    async def search_volumes(
        self,
        db: AsyncSession,
        query: str,
        *,
        limit: int = 25,
        force_refresh: bool = False,
    ) -> dict:
        """Returns the CV envelope's ``results`` (a list)."""
        request_key = _request_key("volumes", {"name": query, "limit": str(limit)})
        existing = await db.get(CvSearchCache, request_key)
        ttl = await self._ttl_seconds(db, "search")
        if existing is not None and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing.response_json
            # Search results don't get SWR — they're cheap to refetch and
            # the staleness window matters more than for stable resources.
        envelope = await self._client.search_volumes(db, query, limit=limit)
        await self._upsert_search(db, request_key, envelope)
        return envelope

    async def search(
        self,
        db: AsyncSession,
        query: str,
        *,
        resources: str = "volume",
        limit: int = 25,
        force_refresh: bool = False,
    ) -> dict:
        """Full-text CV search via ``/search/``. Returns the CV envelope.

        Cached in ``cv_search_cache`` keyed on the (query, resources,
        limit) triple. Like ``search_volumes`` this gets no SWR — search
        results are cheap to refetch and a tight staleness window
        matters more than for stable per-id resources.
        """
        request_key = _request_key(
            "search",
            {"query": query, "resources": resources, "limit": str(limit)},
        )
        existing = await db.get(CvSearchCache, request_key)
        ttl = await self._ttl_seconds(db, "search")
        if existing is not None and not force_refresh:
            if _is_fresh(existing.fetched_at, ttl):
                return existing.response_json
        envelope = await self._client.search(db, query, resources=resources, limit=limit)
        await self._upsert_search(db, request_key, envelope)
        return envelope

    # ---- Upsert helpers -------------------------------------------------

    async def _upsert_volume(self, db: AsyncSession, payload: dict) -> CvVolume:
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        publisher_payload = payload.get("publisher") or {}
        publisher_cv_id = int(publisher_payload["id"]) if publisher_payload.get("id") else None

        # We used to fire a ``volume_issues`` bulk-hydration job here
        # on first-touch, but that turned every losing Stage 3
        # candidate (the matcher fetches up to 5 volumes per file to
        # pick the right one, then rejects 4) into a hydration job
        # for a volume the user will never see. Across an initial
        # 22k-file run that ballooned ~1.5k matched volumes into
        # ~5k hydrated ones and burned the issue-gate rate budget
        # the matcher itself needed. The enqueue now lives next to
        # the *successful match* — ``app/jobs/match_file.py`` calls
        # ``enqueue_revalidate("volume_issues", ...)`` after
        # ``run_matcher`` returns an AUTO result — so bulk hydration
        # fires once per winning volume rather than once per
        # candidate.

        # Upsert the publisher stub if the volume's publisher is referenced
        # and we don't already have it. We only have name/id here (no full
        # publisher payload), so we write a thin row — a later /publisher/X/
        # fetch will replace it with the full record. This keeps the FK valid.
        if publisher_cv_id is not None:
            await self._upsert_publisher_stub(
                db, publisher_cv_id, publisher_payload.get("name") or "(unknown)"
            )

        stmt = (
            pg_insert(CvVolume)
            .values(
                cv_id=cv_id,
                name=payload.get("name") or "",
                year=safe_int(payload.get("start_year")),
                publisher_cv_id=publisher_cv_id,
                count_of_issues=safe_int(payload.get("count_of_issues")),
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvVolume.cv_id],
                set_={
                    "name": payload.get("name") or "",
                    "year": safe_int(payload.get("start_year")),
                    "publisher_cv_id": publisher_cv_id,
                    "count_of_issues": safe_int(payload.get("count_of_issues")),
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)

        # Upsert stub issues from the volume's nested issue list (§8).
        nested_issues = payload.get("issues") or []
        for issue_stub in nested_issues:
            await self._upsert_issue_stub(db, issue_stub, volume_cv_id=cv_id)

        await db.commit()

        # ``populate_existing=True`` forces a re-read so the returned ORM
        # object reflects the just-written row. Without it, if a stale copy
        # of this row was already in the session's identity map (e.g., from
        # an earlier ``existing = await db.get(...)`` in ``get_volume``),
        # ``db.get`` would return that stale Python object — our raw
        # INSERT/UPSERT bypasses the ORM, so the in-memory state doesn't
        # auto-refresh.
        result = await db.get(CvVolume, cv_id, populate_existing=True)
        assert result is not None  # we just upserted
        return result

    async def _upsert_issue(self, db: AsyncSession, payload: dict) -> CvIssue:
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        volume_payload = payload.get("volume") or {}
        volume_cv_id = int(volume_payload["id"]) if volume_payload.get("id") else None

        # Stub the volume row if it's referenced but we don't have it yet —
        # symmetric to the publisher stub in ``_upsert_volume``. A later
        # ``/volume/X/`` fetch replaces the stub with the full record.
        if volume_cv_id is not None:
            await self._upsert_volume_stub(
                db, volume_cv_id, volume_payload.get("name") or "(unknown)"
            )

        stmt = (
            pg_insert(CvIssue)
            .values(
                cv_id=cv_id,
                volume_cv_id=volume_cv_id,
                issue_number=payload.get("issue_number"),
                cover_date=_safe_date(payload.get("cover_date")),
                name=payload.get("name"),
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvIssue.cv_id],
                set_={
                    "volume_cv_id": volume_cv_id,
                    "issue_number": payload.get("issue_number"),
                    "cover_date": _safe_date(payload.get("cover_date")),
                    "name": payload.get("name"),
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
        # See note in ``_upsert_volume``: forces a re-read to override any
        # stale ORM copy left over from the cache-lookup at the top of
        # ``get_issue``.
        result = await db.get(CvIssue, cv_id, populate_existing=True)
        assert result is not None
        return result

    async def _upsert_issue_stub(self, db: AsyncSession, stub: dict, *, volume_cv_id: int) -> None:
        """Insert (don't update) a stub row from a volume fetch.

        We deliberately don't overwrite an existing row — if a previous
        ``/issue/X/`` fetch hydrated it, the stub from a later volume payload
        has less information than what we already have. The PK insert with
        ``on_conflict_do_nothing`` keeps the hydrated row intact.

        ``raw_payload`` is set to the stub dict CV gave us inside the volume
        response (it includes the issue's ``image`` field, useful for table
        thumbnails). ``fetched_at`` stays NULL — that's the marker
        ``get_issue`` uses to know this row still needs a full hydration
        when the user opens the issue detail page.
        """
        if not stub.get("id"):
            return
        stmt = (
            pg_insert(CvIssue)
            .values(
                cv_id=int(stub["id"]),
                volume_cv_id=volume_cv_id,
                issue_number=stub.get("issue_number"),
                cover_date=_safe_date(stub.get("cover_date")),
                name=stub.get("name"),
                raw_payload=stub,
                fetched_at=None,
            )
            .on_conflict_do_nothing(index_elements=[CvIssue.cv_id])
        )
        await db.execute(stmt)

    async def _upsert_publisher(self, db: AsyncSession, payload: dict) -> CvPublisher:
        """Replace any existing (often stub) publisher row with full data.

        Stub rows from ``_upsert_publisher_stub`` carry only id/name plus
        ``_stub: True``; the full CV publisher payload includes the image
        dict, location, deck/description, etc. This upsert overwrites
        with the complete payload."""
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        stmt = (
            pg_insert(CvPublisher)
            .values(
                cv_id=cv_id,
                name=payload.get("name") or "",
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvPublisher.cv_id],
                set_={
                    "name": payload.get("name") or "",
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
        result = await db.get(CvPublisher, cv_id, populate_existing=True)
        assert result is not None
        return result

    async def _upsert_story_arc(self, db: AsyncSession, payload: dict) -> CvStoryArc:
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        stmt = (
            pg_insert(CvStoryArc)
            .values(
                cv_id=cv_id,
                name=payload.get("name") or "",
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvStoryArc.cv_id],
                set_={
                    "name": payload.get("name") or "",
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
        result = await db.get(CvStoryArc, cv_id, populate_existing=True)
        assert result is not None
        return result

    async def _upsert_character(self, db: AsyncSession, payload: dict) -> CvCharacter:
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        stmt = (
            pg_insert(CvCharacter)
            .values(
                cv_id=cv_id,
                name=payload.get("name") or "",
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvCharacter.cv_id],
                set_={
                    "name": payload.get("name") or "",
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
        result = await db.get(CvCharacter, cv_id, populate_existing=True)
        assert result is not None
        return result

    async def _upsert_person(self, db: AsyncSession, payload: dict) -> CvPerson:
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        stmt = (
            pg_insert(CvPerson)
            .values(
                cv_id=cv_id,
                name=payload.get("name") or "",
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvPerson.cv_id],
                set_={
                    "name": payload.get("name") or "",
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
        result = await db.get(CvPerson, cv_id, populate_existing=True)
        assert result is not None
        return result

    async def _upsert_team(self, db: AsyncSession, payload: dict) -> CvTeam:
        now = datetime.now(tz=UTC)
        cv_id = int(payload["id"])
        stmt = (
            pg_insert(CvTeam)
            .values(
                cv_id=cv_id,
                name=payload.get("name") or "",
                raw_payload=payload,
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CvTeam.cv_id],
                set_={
                    "name": payload.get("name") or "",
                    "raw_payload": payload,
                    "fetched_at": now,
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
        result = await db.get(CvTeam, cv_id, populate_existing=True)
        assert result is not None
        return result

    async def _upsert_publisher_stub(self, db: AsyncSession, cv_id: int, name: str) -> None:
        """Thin publisher row to keep the FK valid on volume upsert.

        Only writes if there's no row yet — a real ``/publisher/X/`` call
        later replaces it with the full record.
        """
        now = datetime.now(tz=UTC)
        stmt = (
            pg_insert(CvPublisher)
            .values(
                cv_id=cv_id,
                name=name,
                raw_payload={"id": cv_id, "name": name, "_stub": True},
                fetched_at=now,
            )
            .on_conflict_do_nothing(index_elements=[CvPublisher.cv_id])
        )
        await db.execute(stmt)

    async def _upsert_volume_stub(self, db: AsyncSession, cv_id: int, name: str) -> None:
        """Thin volume row to keep the FK valid on issue upsert.

        Same pattern as ``_upsert_publisher_stub``. A later ``/volume/X/``
        fetch will replace this stub with the full record (which also
        re-populates the volume's nested issue stubs).
        """
        now = datetime.now(tz=UTC)
        stmt = (
            pg_insert(CvVolume)
            .values(
                cv_id=cv_id,
                name=name,
                year=None,
                publisher_cv_id=None,
                count_of_issues=None,
                raw_payload={"id": cv_id, "name": name, "_stub": True},
                fetched_at=now,
            )
            .on_conflict_do_nothing(index_elements=[CvVolume.cv_id])
        )
        await db.execute(stmt)

    async def _upsert_search(self, db: AsyncSession, request_key: str, envelope: dict) -> None:
        now = datetime.now(tz=UTC)
        stmt = (
            pg_insert(CvSearchCache)
            .values(request_key=request_key, response_json=envelope, fetched_at=now)
            .on_conflict_do_update(
                index_elements=[CvSearchCache.request_key],
                set_={"response_json": envelope, "fetched_at": now},
            )
        )
        await db.execute(stmt)
        await db.commit()

    # ---- TTL ------------------------------------------------------------

    async def _ttl_seconds(self, db: AsyncSession, entity: str) -> int:
        overrides = await get_cv_ttl_overrides(db)
        return overrides.get(entity, DEFAULT_TTL_SECONDS[entity])


# ---- Helpers ------------------------------------------------------------


def _is_fresh(fetched_at: datetime | None, ttl_seconds: int) -> bool:
    if fetched_at is None:
        return False
    return datetime.now(tz=UTC) - fetched_at < timedelta(seconds=ttl_seconds)


def _has_image_payload(row) -> bool:
    """True when the cached row already carries enough image data to
    render browse-page thumbnails / banners.

    The skip-SWR guard in ``get_volume`` uses this to avoid firing a
    background revalidate on stale-but-displayable rows. A character
    or creator page that lands on 50 cached volumes shouldn't queue
    50 individual revalidate jobs (each ~20s on the pacer) just to
    refresh data that almost never changes — covers and image URLs
    are stable across CV's revision cycle. The stub branch and the
    explicit bulk-hydrate path still handle the genuinely-missing
    case. Returns False for rows whose ``raw_payload`` isn't a dict
    or whose ``image`` key is missing / falsy — the existing SWR
    fires in those cases."""
    raw = getattr(row, "raw_payload", None)
    if not isinstance(raw, dict):
        return False
    img = raw.get("image")
    if not isinstance(img, dict):
        return False
    # CV's ``image`` block has size-keyed URLs (``small_url``,
    # ``medium_url``, ``thumb_url``, ...). Any one of them being a
    # populated string is enough to render the card.
    return any(isinstance(v, str) and v for k, v in img.items() if k.endswith("_url"))


def _request_key(endpoint: str, params: dict[str, str]) -> str:
    """Stable hash for the search-cache PK. Sorts params for determinism."""
    canonical = json.dumps(
        {"endpoint": endpoint, "params": dict(sorted(params.items()))},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_date(v: Any):
    """Parse ComicVine's YYYY-MM-DD cover_date. Returns None if unparseable."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.fromisoformat(str(v)).date()
    except ValueError:
        return None
