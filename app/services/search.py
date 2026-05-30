"""Library-wide search across the local cache.

A single public entry point — ``search_library(db, q, limit_per_kind)`` —
runs one cheap query per entity kind (volumes, local volumes, issues,
characters, creators, teams, story arcs) and returns ``SearchResults``: a
container of per-kind hit lists for the header dropdown + ``/search``
page to render.

This service ONLY hits the local database. Searching the live ComicVine
catalogue is a separate feature (see ``app/services/cv_search.py`` —
already used for fix-match candidate search). The header search box is
"what do I already have" first; reaching into CV will be a follow-up.

Ranking, per kind: case-insensitive substring match, starts-with first,
then alphabetical (shorter names break ties). For volumes (the only kind
where "in your library" is a hard predicate) ownership is computed via
the same join ``list_library_volumes`` uses — at least one ``FileMatch``
in AUTO or CONFIRMED status through ``CvIssue``. For everything else we
either treat every row as owned (local volumes) or skip the split (the
people-and-thing entities, where computing "owned" would require walking
JSON credit lists per row).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.comicvine import ComicVineCache
from app.models import (
    CvCharacter,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvStoryArc,
    CvTeam,
    CvVolume,
    FileMatch,
    LocalVolume,
    MatchStatus,
)
from app.services.cv_helpers import (
    classify_cv_volume,
    classify_volume_format,
    cv_image_url,
    parse_arc_name,
)
from app.services.cv_search import clean_search_query

# Minimum query length below which we return an empty SearchResults.
# 2 chars catches "DC", "Hex", "X-Men" etc. without dragging the entire
# library back on a stray keystroke.
MIN_QUERY_LENGTH = 2

# The seven section keys exposed on ``SearchResults``. Order matters —
# the route and the template iterate over this list to keep both views
# in lockstep. ``search_library``'s optional ``only_kind`` filter must
# name one of these.
SECTION_KEYS: tuple[str, ...] = (
    "volumes",
    "local_volumes",
    "issues",
    "characters",
    "creators",
    "teams",
    "arcs",
)

# Match statuses that count a file as "owned" for ownership predicates.
_OWNED_STATUSES = (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)


# ---- Result dataclasses ------------------------------------------------


@dataclass
class SearchHit:
    """One row in the search results — uniform shape across all kinds.

    ``kind`` is "volume" / "local_volume" / "issue" / "character" /
    "creator" / "team" / "arc". The template branches on it to pick the
    right card / link layout.

    ``subtitle`` is the secondary line shown under ``name`` — publisher
    + year for a volume, "Volume Name #3" for an issue, an empty string
    for kinds with no extra context.

    ``owned`` is only meaningful for kinds that participate in the
    "In your library" / "Other" split (currently volumes + local
    volumes + issues). For the rest it is always True so the dropdown's
    grouping code doesn't need a per-kind exception.

    ``is_stub`` flags rows that haven't been hydrated into their own
    cv_* cache table yet — credit-walk character/creator/team/arc hits
    (no cv_characters row at all) and CvVolume rows carrying the
    ``raw_payload._stub`` marker. The /search route uses this to
    enqueue background hydration on the interactive lane.
    """

    kind: str
    name: str
    subtitle: str
    cover_url: str | None
    detail_url: str
    owned: bool
    # Unique-within-a-result-list token (cv_id or local uuid as a string).
    # Templates use it for ``key=`` / ``id=`` attributes when we render
    # the same list in Alpine.
    key: str
    is_stub: bool = False

    # ---- Optional rich-volume fields ----------------------------------
    #
    # These power the review-style volume card (``_volume_search_card``)
    # so the /search and /search/comicvine volume sections render with
    # the same publisher / format / count / description popover as the
    # matcher's confirm-volume page. Populated only by the volume
    # builders (``_search_volumes``, ``_cv_volume_hit``,
    # ``get_hit_for_hydration``); every other kind leaves them None and
    # the generic row partial ignores them.
    cv_id: int | None = None
    year: int | None = None
    publisher: str | None = None
    format: str | None = None
    issue_count: int | None = None
    description: str | None = None


@dataclass
class SearchResults:
    """All hits for one ``search_library`` call, partitioned by kind.

    ``more_available`` lists the section keys ("volumes", "characters",
    ...) for which the per-kind query found at least one row beyond the
    returned window — i.e. the section has a "View all →" link target.
    Computed by querying ``limit + 1`` rows then trimming to ``limit``,
    so it's free of an extra COUNT round-trip.
    """

    query: str
    volumes: list[SearchHit] = field(default_factory=list)
    local_volumes: list[SearchHit] = field(default_factory=list)
    issues: list[SearchHit] = field(default_factory=list)
    characters: list[SearchHit] = field(default_factory=list)
    creators: list[SearchHit] = field(default_factory=list)
    teams: list[SearchHit] = field(default_factory=list)
    arcs: list[SearchHit] = field(default_factory=list)
    more_available: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return (
            len(self.volumes)
            + len(self.local_volumes)
            + len(self.issues)
            + len(self.characters)
            + len(self.creators)
            + len(self.teams)
            + len(self.arcs)
        )

    @property
    def is_empty(self) -> bool:
        return self.total == 0


# ---- Helpers -----------------------------------------------------------


def _normalize(q: str) -> str:
    """Lowercase, collapse-whitespace the query before matching."""
    return " ".join((q or "").lower().split())


def _starts_with_rank(column, needle: str):
    """SQL expression: 0 if the column starts with the needle, else 1.

    Used as the primary ORDER BY so prefix matches outrank substring
    matches without us having to issue two queries. ``ilike`` is the
    cheapest case-insensitive operator here — every name column in this
    module is already indexed for equality, and the optimizer is happy
    with a leading-anchored pattern.
    """
    return case((func.lower(column).like(f"{needle}%"), 0), else_=1)


# ---- Per-kind queries --------------------------------------------------


def _build_volume_hit(
    *,
    cv_id: int,
    name: str | None,
    year: int | None,
    publisher: str | None,
    cover_url: str | None,
    owned: bool,
    is_stub: bool,
    format: str | None,
    issue_count: int | None,
    description: str | None,
) -> SearchHit:
    """Single source of truth for the rich-volume ``SearchHit`` shape.

    Called by every path that produces a volume hit:
    ``_search_volumes`` (library), ``_cv_volume_hit`` (CV catalogue
    search), and the volume branch of ``get_hit_for_hydration`` (post-
    hydrate poll swap). All three feed the same ``_volume_search_card``
    partial, so the subtitle wording, detail-URL convention, and
    optional-field set must stay in lockstep — this helper enforces
    that.
    """
    bits: list[str] = []
    if publisher:
        bits.append(publisher)
    if year:
        bits.append(str(year))
    return SearchHit(
        kind="volume",
        name=name or "(untitled volume)",
        subtitle=" · ".join(bits),
        cover_url=cover_url,
        detail_url=f"/volume/{cv_id}",
        owned=owned,
        key=str(cv_id),
        is_stub=is_stub,
        cv_id=cv_id,
        year=year,
        publisher=publisher,
        format=format,
        issue_count=issue_count,
        description=description,
    )


async def _search_volumes(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    """CV-backed volumes — hybrid (owned first, then other)."""

    # Owned predicate matches list_library_volumes: any AUTO/CONFIRMED
    # file_matches row joined through cv_issues to this volume.
    owned_subq = (
        select(CvIssue.volume_cv_id)
        .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .where(FileMatch.status.in_(_OWNED_STATUSES))
        .distinct()
        .subquery()
    )
    is_owned = case(
        (CvVolume.cv_id.in_(select(owned_subq.c.volume_cv_id)), True),
        else_=False,
    ).label("is_owned")

    needle = q
    stmt = (
        select(CvVolume, CvPublisher.name.label("publisher_name"), is_owned)
        .outerjoin(CvPublisher, CvPublisher.cv_id == CvVolume.publisher_cv_id)
        .where(func.lower(CvVolume.name).like(f"%{needle}%"))
        .order_by(
            is_owned.desc(),
            _starts_with_rank(CvVolume.name, needle),
            func.length(CvVolume.name),
            CvVolume.name,
        )
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    hits: list[SearchHit] = []
    for vol, publisher_name, owned in rows:
        # Same predicate ``list_library_volumes`` uses: stub rows from
        # ``_upsert_volume_stub`` carry the ``_stub: True`` marker
        # inside raw_payload until a real fetch overwrites them.
        is_stub = isinstance(vol.raw_payload, dict) and vol.raw_payload.get("_stub") is True
        payload = vol.raw_payload if isinstance(vol.raw_payload, dict) else {}
        # ``format`` / ``description`` are payload-derived — keep them
        # blank on stubs so the card doesn't claim data we don't have
        # yet. The post-hydrate poll swap fills them in.
        hits.append(
            _build_volume_hit(
                cv_id=vol.cv_id,
                name=vol.name,
                year=vol.year,
                publisher=publisher_name,
                cover_url=cv_image_url(payload, "thumb"),
                owned=bool(owned),
                is_stub=is_stub,
                format=classify_cv_volume(vol) if not is_stub else None,
                issue_count=vol.count_of_issues,
                description=payload.get("description") if not is_stub else None,
            )
        )
    return hits


async def _search_local_volumes(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    """User-authored local volumes — always treated as owned."""

    needle = q
    stmt = (
        select(LocalVolume)
        .where(func.lower(LocalVolume.name).like(f"%{needle}%"))
        .order_by(
            _starts_with_rank(LocalVolume.name, needle),
            func.length(LocalVolume.name),
            LocalVolume.name,
        )
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    hits: list[SearchHit] = []
    for lv in rows:
        bits: list[str] = []
        if lv.publisher_name:
            bits.append(lv.publisher_name)
        if lv.year:
            bits.append(str(lv.year))
        hits.append(
            SearchHit(
                kind="local_volume",
                name=lv.name,
                subtitle=" · ".join(bits),
                cover_url=None,
                detail_url=f"/local/volume/{lv.id}",
                owned=True,
                key=str(lv.id),
            )
        )
    return hits


async def _search_issues(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    """Owned CV issues — issue name match. We restrict to owned (a file
    matches the issue) because the CvIssue table accumulates tens of
    thousands of un-owned stub rows from volume hydration; surfacing
    those as "other results" would drown the dropdown."""

    needle = q
    stmt = (
        select(CvIssue, CvVolume.name.label("volume_name"))
        .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .outerjoin(CvVolume, CvVolume.cv_id == CvIssue.volume_cv_id)
        .where(
            FileMatch.status.in_(_OWNED_STATUSES),
            CvIssue.name.is_not(None),
            func.lower(CvIssue.name).like(f"%{needle}%"),
        )
        .order_by(
            _starts_with_rank(CvIssue.name, needle),
            func.length(CvIssue.name),
            CvIssue.name,
        )
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    hits: list[SearchHit] = []
    for issue, volume_name in rows:
        # Subtitle: "<Volume Name> #<number>" — when both present.
        bits: list[str] = []
        if volume_name:
            bits.append(volume_name)
        if issue.issue_number:
            bits.append(f"#{issue.issue_number}")
        subtitle = " ".join(bits)
        hits.append(
            SearchHit(
                kind="issue",
                name=issue.name or f"#{issue.issue_number or '?'}",
                subtitle=subtitle,
                cover_url=cv_image_url(issue.raw_payload, "thumb") if issue.raw_payload else None,
                detail_url=f"/issue/{issue.cv_id}",
                owned=True,
                key=str(issue.cv_id),
            )
        )
    return hits


def _entity_query(model, q: str, limit: int):
    """Shared SELECT for the simple (cv_id, name, raw_payload) entities
    — characters, creators (CvPerson), teams, arcs. All four tables
    share the same shape so the query is identical bar the model."""
    needle = q
    return (
        select(model)
        .where(func.lower(model.name).like(f"%{needle}%"))
        .order_by(
            _starts_with_rank(model.name, needle),
            func.length(model.name),
            model.name,
        )
        .limit(limit)
    )


async def _credits_stubs(
    db: AsyncSession,
    credits_key: str,
    q: str,
    limit: int,
    exclude_cv_ids: set[int],
) -> list[tuple[int, str]]:
    """Return (cv_id, name) for entities mentioned in owned issues'
    ``raw_payload[credits_key]`` whose name matches ``q``, skipping any
    cv_ids in ``exclude_cv_ids`` (already covered by the hydrated table
    query). The matching detail page hydrates the record on first
    view, so a stub link is safe — same posture as the volume-stub
    flow used by ``list_library_volumes``.

    Implementation notes:

    - Tests ``(elem->'id') IS NOT NULL`` instead of the JSONB ``?``
      operator. ``?`` is sometimes treated as a paramstyle placeholder
      by SQLAlchemy / DBAPI layers; the IS NOT NULL form is
      unambiguous.
    - The exclude list is inlined into the SQL rather than bound. A
      ``:exclude_ids::bigint[]`` placeholder breaks SQLAlchemy's
      ``text()`` parser (the ``::`` Postgres cast directly after the
      colon-identifier confuses the regex), leaving ``:exclude_ids``
      literal in the rendered SQL — Postgres then errors on the
      missing bind. Inlining the ints is safe because each value goes
      through ``int()`` first.
    """
    if limit <= 0:
        return []

    # Build the optional NOT IN clause from the int-coerced exclude
    # set. Empty set → no clause at all (avoids the SQL needing to
    # cope with an empty IN list, which Postgres rejects).
    exclude_clause = ""
    if exclude_cv_ids:
        ids_csv = ",".join(str(int(i)) for i in exclude_cv_ids)
        exclude_clause = f"AND (elem->>'id')::bigint NOT IN ({ids_csv})"

    sql = text(
        f"""
        SELECT
          (elem->>'id')::bigint AS cv_id,
          MIN(elem->>'name') AS name
        FROM cv_issues
        JOIN file_matches
          ON file_matches.issue_cv_id = cv_issues.cv_id
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(cv_issues.raw_payload -> :credits_key, '[]'::jsonb)
        ) AS elem
        WHERE file_matches.status IN ('auto', 'confirmed')
          AND cv_issues.raw_payload IS NOT NULL
          AND (elem -> 'id') IS NOT NULL
          AND (elem -> 'name') IS NOT NULL
          AND LOWER(elem->>'name') LIKE :pattern
          {exclude_clause}
        GROUP BY (elem->>'id')::bigint
        ORDER BY
          CASE WHEN LOWER(MIN(elem->>'name')) LIKE :prefix_pattern THEN 0 ELSE 1 END,
          MIN(LENGTH(elem->>'name')),
          MIN(elem->>'name')
        LIMIT :limit
        """
    )
    rows = await db.execute(
        sql,
        {
            "credits_key": credits_key,
            "pattern": f"%{q}%",
            "prefix_pattern": f"{q}%",
            "limit": limit,
        },
    )
    return [(int(r[0]), r[1] or "") for r in rows.all()]


def _identity_name(raw: str) -> tuple[str, str]:
    """Default name-transform for the generic entity search: return
    the row's name unchanged with no extra subtitle. Arcs override
    this with ``_arc_name_transform`` below."""
    return raw or "", ""


def _arc_name_transform(raw: str) -> tuple[str, str]:
    """Story-arc name transform — splits CV's ``"<book>" <arc>``
    prefix off the display name and surfaces the parent book as the
    subtitle. Matches the rendering used elsewhere (the issue page's
    arc shelves, the team/creator arc tabs) so a card titled
    ``"Avengers" Dark Reign`` shows as ``Dark Reign`` with an
    ``Avengers`` subtitle on the search page too.
    """
    primary_book, clean_name = parse_arc_name(raw)
    return (clean_name or raw or "", primary_book or "")


async def _entity_search_with_stubs(
    db: AsyncSession,
    model,
    kind: str,
    credits_key: str,
    image_size: str,
    detail_url_prefix: str,
    q: str,
    limit: int,
    transform_name=_identity_name,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    """Search the hydrated entity table, then top up with stubs from
    owned issues' credit lists. Shared body for characters / creators
    / teams / arcs — the only differences across kinds are the model,
    the JSONB credits key, the preferred image size, the link prefix,
    and (for arcs) a name parser that splits CV's quoted-prefix
    convention into ``(display_name, subtitle)``.

    ``include_credits_stubs=False`` skips the JSONB credits walk —
    used by the /search/live header dropdown, which fires on every
    keystroke and doesn't drive hydration. The credits scan is the
    expensive part (JSON @> against every owned issue's payload), so
    skipping it keeps dropdown latency low.
    """
    rows = (await db.execute(_entity_query(model, q, limit))).scalars().all()
    hits: list[SearchHit] = []
    for row in rows:
        display_name, subtitle = transform_name(row.name)
        hits.append(
            SearchHit(
                kind=kind,
                name=display_name,
                subtitle=subtitle,
                cover_url=cv_image_url(row.raw_payload, image_size),
                detail_url=f"{detail_url_prefix}{row.cv_id}",
                owned=True,
                key=str(row.cv_id),
            )
        )

    remaining = limit - len(hits)
    if remaining <= 0 or not include_credits_stubs:
        return hits

    # Don't double-list cv_ids the hydrated query already returned.
    seen_cv_ids = {row.cv_id for row in rows}
    stubs = await _credits_stubs(db, credits_key, q, remaining, seen_cv_ids)
    for cv_id, name in stubs:
        display_name, subtitle = transform_name(name)
        hits.append(
            SearchHit(
                kind=kind,
                name=display_name,
                subtitle=subtitle,
                cover_url=None,  # stub — detail page will hydrate the image
                detail_url=f"{detail_url_prefix}{cv_id}",
                owned=True,
                key=str(cv_id),
                is_stub=True,
            )
        )
    return hits


async def _search_characters(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    return await _entity_search_with_stubs(
        db,
        CvCharacter,
        "character",
        "character_credits",
        "icon",
        "/character/",
        q,
        limit,
        include_credits_stubs=include_credits_stubs,
    )


async def _search_creators(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    return await _entity_search_with_stubs(
        db,
        CvPerson,
        "creator",
        "person_credits",
        "icon",
        "/creator/",
        q,
        limit,
        include_credits_stubs=include_credits_stubs,
    )


async def _search_teams(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    return await _entity_search_with_stubs(
        db,
        CvTeam,
        "team",
        "team_credits",
        "icon",
        "/team/",
        q,
        limit,
        include_credits_stubs=include_credits_stubs,
    )


async def _search_arcs(
    db: AsyncSession,
    q: str,
    limit: int,
    *,
    include_credits_stubs: bool = True,
) -> list[SearchHit]:
    return await _entity_search_with_stubs(
        db,
        CvStoryArc,
        "arc",
        "story_arc_credits",
        "thumb",
        "/arc/",
        q,
        limit,
        transform_name=_arc_name_transform,
        include_credits_stubs=include_credits_stubs,
    )


# ---- Per-kind row lookup for the hydration polling endpoint -----------
#
# Maps a SearchHit.kind onto its (model, image_size, link_prefix) tuple.
# Used by ``get_hit_for_hydration`` to look up a single row by cv_id
# and rebuild a fresh SearchHit if the row is no longer a stub. The
# /search/hydration endpoint calls this on every poll for every
# pending key.

_HYDRATION_ENTITY_SPECS: dict[str, tuple[type, str, str]] = {
    "character": (CvCharacter, "icon", "/character/"),
    "creator": (CvPerson, "icon", "/creator/"),
    "team": (CvTeam, "icon", "/team/"),
    "arc": (CvStoryArc, "thumb", "/arc/"),
}


async def get_hit_for_hydration(db: AsyncSession, kind: str, cv_id: int) -> SearchHit | None:
    """Build a fresh SearchHit for one (kind, cv_id) the polling
    endpoint is checking on. Returns ``None`` if:

    - The kind isn't one we hydrate from the search surface (local
      volumes / issues don't need this path).
    - The row still isn't in its cv_* table — the interactive worker
      hasn't drained the job yet, keep polling.
    - The row's CV payload still says it's a stub (volume only).

    A non-None return means the page can swap the stub row out for
    this hit and stop polling that key.
    """
    if kind == "volume":
        # Volumes already exist in cv_volumes (a stub row carries
        # raw_payload._stub=True). Hydrated means that marker is gone.
        vol = await db.get(CvVolume, cv_id)
        if vol is None:
            return None
        is_stub = isinstance(vol.raw_payload, dict) and vol.raw_payload.get("_stub") is True
        if is_stub:
            return None
        # Owned-ness needs the file_match join again; cheap because
        # we're only checking one volume.
        owned_exists = await db.execute(
            select(FileMatch.file_id)
            .join(CvIssue, FileMatch.issue_cv_id == CvIssue.cv_id)
            .where(
                CvIssue.volume_cv_id == cv_id,
                FileMatch.status.in_(_OWNED_STATUSES),
            )
            .limit(1)
        )
        owned = owned_exists.first() is not None
        # Publisher: single JOIN call instead of two ``db.get``s. Same
        # outerjoin pattern ``_search_volumes`` uses, just narrowed to
        # this one volume — keeps the hydration tick at one SELECT.
        publisher_row = await db.execute(
            select(CvPublisher.name)
            .select_from(CvVolume)
            .outerjoin(CvPublisher, CvPublisher.cv_id == CvVolume.publisher_cv_id)
            .where(CvVolume.cv_id == cv_id)
        )
        publisher = publisher_row.scalar_one_or_none()
        payload = vol.raw_payload if isinstance(vol.raw_payload, dict) else {}
        return _build_volume_hit(
            cv_id=vol.cv_id,
            name=vol.name,
            year=vol.year,
            publisher=publisher,
            cover_url=cv_image_url(payload, "thumb"),
            owned=owned,
            is_stub=False,
            format=classify_cv_volume(vol),
            issue_count=vol.count_of_issues,
            description=payload.get("description"),
        )

    spec = _HYDRATION_ENTITY_SPECS.get(kind)
    if spec is None:
        return None
    model, image_size, link_prefix = spec
    row = await db.get(model, cv_id)
    if row is None:
        return None  # still a credit-walk stub; keep polling
    # Arc names use the parser; everything else passes through.
    transform = _arc_name_transform if kind == "arc" else _identity_name
    display_name, subtitle = transform(row.name)
    return SearchHit(
        kind=kind,
        name=display_name,
        subtitle=subtitle,
        cover_url=cv_image_url(row.raw_payload, image_size),
        detail_url=f"{link_prefix}{cv_id}",
        owned=True,
        key=str(cv_id),
        is_stub=False,
    )


# ---- Top-level entry point --------------------------------------------


_KIND_FETCHERS = {
    "volumes": _search_volumes,
    "local_volumes": _search_local_volumes,
    "issues": _search_issues,
    "characters": _search_characters,
    "creators": _search_creators,
    "teams": _search_teams,
    "arcs": _search_arcs,
}


# ---- ComicVine catalogue search ---------------------------------------
#
# Mirrors ``search_library`` but reaches CV's ``/search/`` endpoint
# instead of the local cache. Used by the "Search ComicVine" view on
# /search/comicvine — opt-in, one CV call per page load. Results link
# back to the local detail URLs so clicking through lazy-hydrates via
# the existing cache layer.


# CV /search/ takes a comma-separated ``resources`` list. We request
# the same six kinds the library surface supports — local_volumes and
# issues-without-an-owned-file aren't CV concepts (the former is
# user-authored, the latter is a library-side filter). One round-trip
# returns mixed results we partition by ``resource_type``.
_CV_SEARCH_RESOURCES = "volume,issue,character,person,team,story_arc"

# Inverse of _RESOURCE_TYPE_TO_KIND, but keyed by SearchResults
# *section* name rather than the UI ``kind``. Used by the
# ``only_kind`` narrowing path — when the route asks for one section,
# we hit CV's /search/ with just the matching resource_type so the
# request budget is spent entirely on that drill-down. Mirrors the
# library kind-filter's tighter query.
_SECTION_KEY_TO_CV_RESOURCE = {
    "volumes": "volume",
    "issues": "issue",
    "characters": "character",
    "creators": "person",
    "teams": "team",
    "arcs": "story_arc",
}

# Map CV's ``resource_type`` string to our SearchHit ``kind``. They
# diverge in two places: CV calls creators ``person`` and arcs
# ``story_arc``; the UI uses ``creator`` and ``arc``. The keys here
# are the only resource_types we render.
_RESOURCE_TYPE_TO_KIND = {
    "volume": "volume",
    "issue": "issue",
    "character": "character",
    "person": "creator",
    "team": "team",
    "story_arc": "arc",
}


def _cv_volume_hit(item: dict) -> SearchHit | None:
    cv_id = item.get("id")
    if not isinstance(cv_id, int):
        return None
    publisher = item.get("publisher") or {}
    publisher_name = publisher.get("name") if isinstance(publisher, dict) else None
    raw_year = item.get("start_year")
    # CV returns ``start_year`` as a string. Coerce to int for the
    # card so it can format consistently. None on non-numeric junk.
    year: int | None = None
    if raw_year is not None:
        try:
            year = int(str(raw_year).strip())
        except (TypeError, ValueError):
            year = None
    first_issue = item.get("first_issue") or {}
    first_issue_name = first_issue.get("name") if isinstance(first_issue, dict) else None
    return _build_volume_hit(
        cv_id=cv_id,
        name=item.get("name"),
        year=year,
        publisher=publisher_name,
        cover_url=cv_image_url(item, "thumb"),
        owned=False,
        is_stub=False,
        format=classify_volume_format(
            name=item.get("name"),
            count_of_issues=item.get("count_of_issues"),
            deck=item.get("deck"),
            description=item.get("description"),
            first_issue_name=first_issue_name,
        ),
        issue_count=item.get("count_of_issues"),
        description=item.get("description"),
    )


def _cv_issue_hit(item: dict) -> SearchHit | None:
    cv_id = item.get("id")
    if not isinstance(cv_id, int):
        return None
    volume = item.get("volume") or {}
    volume_name = volume.get("name") if isinstance(volume, dict) else None
    number = item.get("issue_number")
    bits: list[str] = []
    if volume_name:
        bits.append(volume_name)
    if number:
        bits.append(f"#{number}")
    name = item.get("name")
    return SearchHit(
        kind="issue",
        name=name or (f"#{number}" if number else "(untitled issue)"),
        subtitle=" ".join(bits),
        cover_url=cv_image_url(item, "thumb"),
        detail_url=f"/issue/{cv_id}",
        owned=False,
        key=str(cv_id),
        is_stub=False,
    )


def _cv_simple_hit(
    item: dict,
    kind: str,
    detail_prefix: str,
    image_size: str = "icon",
    transform_name=_identity_name,
) -> SearchHit | None:
    cv_id = item.get("id")
    if not isinstance(cv_id, int):
        return None
    display_name, subtitle = transform_name(item.get("name") or "")
    return SearchHit(
        kind=kind,
        name=display_name or "(unnamed)",
        subtitle=subtitle,
        cover_url=cv_image_url(item, image_size),
        detail_url=f"{detail_prefix}{cv_id}",
        owned=False,
        key=str(cv_id),
        is_stub=False,
    )


async def cv_search_catalogue(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    q: str,
    limit_per_kind: int = 10,
    *,
    only_kind: str | None = None,
) -> SearchResults:
    """Hit CV's ``/search/`` endpoint and shape the results into the
    same ``SearchResults`` container the library search returns.

    Routes the call through ``ComicVineCache.search`` so identical
    queries within the search-cache TTL skip the CV round-trip — one
    refresh per quick "I typed it wrong" retry is plenty.

    When ``only_kind`` is ``None`` (default) the call asks CV for all
    six resource kinds in one round-trip; the envelope's mixed
    ``results`` is partitioned by ``resource_type`` and each bucket is
    capped at ``limit_per_kind``. When ``only_kind`` names one of
    ``_SECTION_KEY_TO_CV_RESOURCE``, the call narrows to that single
    resource_type and the entire CV budget feeds that one bucket —
    the "View all <kind> on ComicVine" drill-down path.

    ``detail_url`` on every hit points at the local app's detail page
    (``/volume/{cv_id}``, ``/character/{cv_id}``, ...). The first
    click on an un-hydrated entity will fetch through
    ``ComicVineCache`` — the standard lazy-hydrate path, no special
    branching needed.

    Queries shorter than ``MIN_QUERY_LENGTH`` short-circuit without a
    CV call (the rate pacer charges per call). Caller catches
    ``ComicVineError`` to surface the standard CV error page if the
    key's missing / rate-limited / network's down.
    """
    needle = _normalize(q)
    results = SearchResults(query=needle)
    if len(needle) < MIN_QUERY_LENGTH:
        return results

    cleaned = clean_search_query(needle)
    if not cleaned:
        return results

    # ``only_kind`` narrows the CV /search/ call to one resource_type
    # so the entire rate budget feeds the drill-down. Unknown values
    # fall through to the multi-resource path — matches search_library.
    narrowed_resource = _SECTION_KEY_TO_CV_RESOURCE.get(only_kind or "")
    if narrowed_resource:
        resources = narrowed_resource
        # One bucket only — ask for limit+1 to drive ``more_available``.
        cv_limit = min(limit_per_kind + 1, 100)
    else:
        resources = _CV_SEARCH_RESOURCES
        # CV's response is sorted by relevance across all resources,
        # so we ask for limit * kinds as a safe upper bound; we trim
        # to limit per bucket after partitioning. 100 is CV's per-call
        # cap so we clamp there.
        cv_limit = min(limit_per_kind * len(_RESOURCE_TYPE_TO_KIND), 100)
    envelope = await cv_cache.search(db, cleaned, resources=resources, limit=cv_limit)
    raw = envelope.get("results")
    if not isinstance(raw, list):
        return results

    # Partition + cap. Builders return None on bad rows; those skip.
    buckets: dict[str, list[SearchHit]] = {key: [] for key in _RESOURCE_TYPE_TO_KIND.values()}
    for item in raw:
        if not isinstance(item, dict):
            continue
        resource_type = item.get("resource_type")
        kind = _RESOURCE_TYPE_TO_KIND.get(resource_type)
        if kind is None:
            continue
        if len(buckets[kind]) >= limit_per_kind + 1:
            # ``+1`` reserves an overflow probe so the template's
            # "View all" link can fire on filled sections — matches
            # the library search's more_available signal.
            continue
        if kind == "volume":
            hit = _cv_volume_hit(item)
        elif kind == "issue":
            hit = _cv_issue_hit(item)
        elif kind == "arc":
            hit = _cv_simple_hit(
                item,
                kind,
                "/arc/",
                image_size="thumb",
                transform_name=_arc_name_transform,
            )
        elif kind == "creator":
            hit = _cv_simple_hit(item, kind, "/creator/")
        elif kind == "team":
            hit = _cv_simple_hit(item, kind, "/team/")
        elif kind == "character":
            hit = _cv_simple_hit(item, kind, "/character/")
        else:
            hit = None
        if hit is not None:
            buckets[kind].append(hit)

    for kind_key, hits in buckets.items():
        # Same overflow signal as the library search — let the
        # template render a "View all" link when CV returned more
        # than the visible cap.
        section_key = (
            "creators" if kind_key == "creator" else "arcs" if kind_key == "arc" else f"{kind_key}s"
        )
        if len(hits) > limit_per_kind:
            results.more_available.add(section_key)
            hits = hits[:limit_per_kind]
        setattr(results, section_key, hits)
    return results


async def search_library(
    db: AsyncSession,
    q: str,
    limit_per_kind: int = 5,
    *,
    only_kind: str | None = None,
    include_credits_stubs: bool = True,
) -> SearchResults:
    """Search the local cache for ``q`` across all entity kinds.

    Returns at most ``limit_per_kind`` hits per kind. Queries shorter
    than ``MIN_QUERY_LENGTH`` return an empty result without touching
    the database — the header dropdown hits this on every keystroke,
    so an early bail keeps the request budget tight.

    ``only_kind`` (one of ``SECTION_KEYS``) restricts the search to a
    single section — the /search page's "View all <kind>" view passes
    it so a kind-filtered URL fetches just that one table. Unknown
    values are treated as ``None`` (fall back to all kinds), so a
    junked-up URL still returns something useful.

    ``include_credits_stubs=False`` skips the JSONB credits walk that
    surfaces un-hydrated characters / creators / teams / arcs. Passed
    by the /search/live dropdown — the credits scan is the expensive
    part of every keystroke, and the live surface doesn't enqueue
    hydration anyway. The full /search page leaves it ``True`` so
    stubs appear (and get hydrated via the polling endpoint).

    Each section's query asks for ``limit_per_kind + 1`` rows so
    ``more_available`` can be set without an extra COUNT — a free
    "is there a next page" signal for the template's "View all" link.
    """
    needle = _normalize(q)
    results = SearchResults(query=needle)
    if len(needle) < MIN_QUERY_LENGTH:
        return results

    probe = limit_per_kind + 1
    kinds = (only_kind,) if only_kind in SECTION_KEYS else SECTION_KEYS
    for key in kinds:
        hits = await _KIND_FETCHERS[key](
            db, needle, probe, include_credits_stubs=include_credits_stubs
        )
        if len(hits) > limit_per_kind:
            results.more_available.add(key)
            hits = hits[:limit_per_kind]
        setattr(results, key, hits)
    return results
