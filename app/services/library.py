"""Library browse queries.

Three public functions, each returning a typed dataclass:

- ``list_library_volumes(db, filters)`` — index page. One row per volume
  the user has at least one matched (auto/confirmed) file in.
- ``get_volume_detail(db, cv_id)`` — volume page. The volume's full issue
  list with owned/missing badges per issue, story-arc names harvested
  from the hydrated issues' raw_payloads.
- ``get_issue_detail(db, cv_cache, cv_id)`` — issue page. Full credits +
  characters + arcs + neighbors (prev/next by issue_number within the
  volume) + the file paths that map to this issue. Hydrates stubs via
  ``cv_cache.get_issue`` if needed.

Also:

- ``list_recently_added(db, limit)`` — for the home page's "Recently added"
  section. One row per volume (grouped), most-recently-matched first.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.comicvine import ComicVineCache
from app.comicvine.errors import (
    ComicVineError,
    ComicVineKeyInvalidError,
    ComicVineKeyMissingError,
    ComicVineNotFoundError,
)

if TYPE_CHECKING:
    from app.services.rail import RailModel
from app.models import (
    CvCharacter,
    CvCharacterVolume,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvStoryArc,
    CvTeam,
    CvVolume,
    FileLocation,
    FileMatch,
    LocalIssue,
    LocalVolume,
    MatchStatus,
    ReadProgress,
)
from app.services.cv_helpers import (
    classify_cv_volume,
    classify_volume_format,
    cv_image_url,
    parse_arc_name,
    safe_int,
    sort_key_issue_number,
    volume_status_from_themes,
)
from app.services.reader import issue_progress_for_volume
from app.services.settings import DEFAULT_PAGE_SIZE, get_page_size

# Page-size lives in ``app.services.settings`` as an admin-editable
# row in ``app_settings`` (key: ``page_size``); browse routes pull
# the live value at request time via ``get_page_size(db)``. The
# fallback ``DEFAULT_PAGE_SIZE`` is used in this module for dataclass
# defaults so a direct construction (mostly tests) gets a sensible
# window without round-tripping through the DB.


# ---- Dataclasses --------------------------------------------------------


@dataclass
class LibraryVolumeRow:
    """One row in the /library index."""

    cv_id: int
    name: str
    year: int | None
    publisher_name: str | None
    count_of_issues: int | None  # CV's total
    owned_count: int             # files we have matched into this volume
    cover_url: str | None
    # True for cv_volumes rows that were inserted as FK placeholders by
    # ``_upsert_volume_stub`` (typically because Stage 1 matched an issue
    # before anything fetched the parent volume). Stubs have no
    # count_of_issues, no year, no cover. The library route detects these
    # and enqueues a revalidation so the next view has full metadata.
    is_stub: bool = False
    # Phase 11C — user-authored local volumes are merged into the same
    # /library list. ``kind`` discriminates "cv" vs "local"; ``local_id``
    # is the ``local_volumes`` uuid on a local row (``cv_id`` is a
    # meaningless 0 there). ``detail_url`` / ``dom_key`` give templates
    # one place to branch on it.
    kind: str = "cv"
    local_id: uuid.UUID | None = None
    # Classified volume format — "ongoing" / "limited" / "one_shot" /
    # "collection" / "unknown". None on a local row (a local volume has
    # no ComicVine data to classify from).
    format: str | None = None
    # Phase 6 (reader). Count of issues in this volume the current user
    # has finished — 0 without a user context or nothing read yet.
    # Populated by list_library_volumes when user_id is supplied.
    read_count: int = 0

    @property
    def detail_url(self) -> str:
        """Link target for this row's card — the CV or local volume page."""
        if self.kind == "local":
            return f"/local/volume/{self.local_id}"
        return f"/volume/{self.cv_id}"

    @property
    def dom_key(self) -> str:
        """Unique, stable token for this row's grid/table element id. CV
        rows keep the bare ``cv_id`` so the hydration endpoint's
        ``grid-volume-<cv_id>`` target still matches; local rows (never
        stubs, never hydrated) get a uuid token so two of them can't
        collide on ``grid-volume-0``."""
        if self.kind == "local":
            return f"local-{self.local_id}"
        return str(self.cv_id)

    @property
    def missing_count(self) -> int | None:
        if self.count_of_issues is None:
            return None
        return max(0, self.count_of_issues - self.owned_count)

    @property
    def has_missing(self) -> bool:
        return self.missing_count is not None and self.missing_count > 0


@dataclass
class ArcCredit:
    """Lightweight reference to a story arc — name + CV id (+ optional book).

    ``primary_book`` carries the parsed-out parent book or theme that
    CV often namespaces an arc with (``"Avengers" Disassembled`` →
    ``"Avengers"``). ``name`` is the cleaned arc name (just
    ``"Disassembled"``). Display surfaces are expected to show ``name``
    prominently and reveal ``primary_book`` only in tooltips / subtitles
    (per the minimalist display choice — see ``parse_arc_name``).
    """

    cv_id: int
    name: str
    primary_book: str | None = None


@dataclass
class ArcSiblingLink:
    """A "the arc continues in another volume" arrow for one row.

    Rendered as a colored ← (in the left arrow column) or → (in the right
    arrow column) on the volume page's issues table. Generated only when
    the immediate prev/next member of an arc lives in a different volume
    — same-volume siblings are reachable by reading the next/previous
    table row, so no arrow needed.

    The optional descriptive fields drive the hover tooltip so the user
    sees where the jump leads before clicking. ``issue_name`` is always
    set when CV's arc payload includes it (very common); the rest is
    populated when the sibling issue happens to be cached locally (which
    is true whenever its parent volume is in our library)."""

    arc: ArcCredit
    issue_cv_id: int  # the sibling issue to jump to
    issue_name: str | None = None      # from the arc payload's member dict
    issue_number: str | None = None    # from cv_issues, if we have it
    volume_name: str | None = None     # from cv_volumes, if we have it
    volume_year: int | None = None     # from cv_volumes, if we have it


@dataclass
class VolumeIssueRow:
    """One row on the /volume/{id} issues table."""

    cv_id: int
    issue_number: str | None
    name: str | None
    cover_date: Any  # date or None
    owned: bool
    cover_url: str | None
    arc_credits: list[ArcCredit] = field(default_factory=list)
    # Boundary navigation: prev/next arc siblings that are in a different
    # volume. Empty for rows whose siblings are all in this volume's list.
    prev_arc_links: list[ArcSiblingLink] = field(default_factory=list)
    next_arc_links: list[ArcSiblingLink] = field(default_factory=list)
    # 0-based index of this row in the volume's sorted issue list. Used
    # by templates to drive per-cover ``x-show`` against the shared
    # pagination window so Arcs/Gallery views stay in lockstep with
    # whatever issues List has loaded. Set after ``issues.sort()``.
    volume_idx: int = 0
    # True when the underlying ``cv_issues`` row has been individually
    # fetched (``fetched_at IS NOT NULL``). Stubs created from a
    # volume's nested issue list have ``False`` here. Drives the
    # ``data-hydrated`` marker on the issues table so the auto-refresh
    # helper can find rows pending background hydration.
    is_hydrated: bool = False
    # Phase 6 (reader). The current user's reading progress for this
    # issue's matched file, as a ProgressBar — None when the issue is
    # unowned, unread, or the page was built without a user. Populated
    # by get_volume_detail when user_id is supplied.
    progress: Any = None


@dataclass
class GalleryArcShelf:
    """One arc's section in the per-arc Arcs view.

    Contains the cover thumbnails of every arc-member issue that lives in
    this volume, ordered as CV orders the arc. Optional ``prev_link`` /
    ``next_link`` carry the very-first / very-last arc members that fall
    outside this volume — rendered as ← / → glyphs at the shelf edges.

    A shelf with one issue is a "singleton" — fine; that's exactly the
    case where the volume contributes one tie-in to a crossover arc.
    """

    arc: ArcCredit
    issues: list[VolumeIssueRow]  # in arc reading order
    prev_link: ArcSiblingLink | None = None
    next_link: ArcSiblingLink | None = None


@dataclass
class GallerySegment:
    """One container-shelf in the volume-order Gallery view.

    A contiguous run of issues (in volume number order) that share the
    same arc set. ``arcs`` is ordered by first-seen so ``arcs[0]`` is the
    *outermost* container, ``arcs[1]`` is the next one nested inside it,
    etc. A segment with no arcs is a bare row of covers with no
    container — rendered when issues belong to none of the volume's
    tracked arcs.

    ``prev_links`` / ``next_links`` are parallel to ``arcs`` — same
    length, same indexing. A ``None`` entry means no arrow for that arc
    on that side (either the arc has no prev/next member at all, or the
    arc continues in the *immediately adjacent* segment, in which case
    we suppress the arrow since the user just scrolls one shelf).

    ``first_volume_idx`` / ``last_volume_idx`` are the 0-based indices
    of this segment's first and last issues in the volume's sorted
    issue list. The Gallery uses them to drive per-segment and
    per-cover ``x-show`` against the pagination window so the gallery
    stays in lockstep with the list view's loaded slice.
    """

    issues: list[VolumeIssueRow]
    arcs: list[ArcCredit] = field(default_factory=list)
    prev_links: list[ArcSiblingLink | None] = field(default_factory=list)
    next_links: list[ArcSiblingLink | None] = field(default_factory=list)
    first_volume_idx: int = 0
    last_volume_idx: int = 0


@dataclass
class CreditFilter:
    """An active "issues credited to X" filter on the volume page.

    Set when ``get_volume_detail`` is given a ``credit_filter`` — i.e.
    the volume page was reached from a volume card on a team / creator
    page. Carries the entity's identity (for the banner + a link back)
    and the before/after issue counts. ``kind`` is ``"team"`` or
    ``"creator"`` so the template can build a ``/team/`` or
    ``/creator/`` link."""

    kind: str
    cv_id: int
    name: str
    matched: int  # issues kept after the intersection
    volume_total: int  # issues in the volume before filtering


@dataclass
class VolumeDetail:
    volume: CvVolume
    publisher_name: str | None
    issues: list[VolumeIssueRow]
    # Count of distinct issues in this volume with at least one matched
    # file (auto or confirmed). Computed by a dedicated SQL count in
    # ``get_volume_detail`` so the value isn't dependent on whatever
    # subset of issues ``self.issues`` happens to contain — gives a
    # true global owned-count for the progress ring even on volumes
    # where the cv_issues stub list is incomplete.
    owned_issue_count: int = 0
    # Phase 6 (reader). Count of distinct issues in this volume the
    # current user has finished. 0 without a user. Drives the sidebar
    # reading-progress ring alongside the completeness ring.
    finished_issue_count: int = 0
    story_arc_names: list[str] = field(default_factory=list)
    cover_url: str | None = None
    banner_url: str | None = None  # CV's landscape screen image, if any
    description: str | None = None  # CV wiki content (HTML)
    # Short one-line summary from CV's ``deck`` field (plain text, no
    # HTML). Distinct from ``description`` (long-form wiki); used as
    # the page-header tagline directly under the stats line.
    deck: str | None = None
    # arc_cv_id → Tailwind bg-* class. Stable per arc for the lifetime of
    # the request; cycles through a small palette.
    arc_color_classes: dict[int, str] = field(default_factory=dict)
    # Parallel text-color map, same keys as ``arc_color_classes``. Used by
    # the arrow columns on the issues table — ``bg-*`` doesn't help when
    # we're rendering a glyph rather than a filled bar.
    arc_text_color_classes: dict[int, str] = field(default_factory=dict)
    # Parallel light-tint map (Tailwind ``-50`` shade) used as the
    # background fill behind a shelf's covers in the gallery view.
    arc_tint_color_classes: dict[int, str] = field(default_factory=dict)
    # Higher-contrast pill background (Tailwind ``-500``/``-600``)
    # used by the legend chips, where light pills with dark text
    # don't read reliably across the full palette. Pairs with
    # ``text-white`` in the template.
    arc_pill_bg_color_classes: dict[int, str] = field(default_factory=dict)
    # Ordered list of arcs in their slot order (left → right on the issues
    # table). Each issue's per-arc stripe occupies the slot whose index
    # matches this list, so the colored column for an arc lines up
    # vertically across rows regardless of which other arcs are present.
    arc_slots: list[ArcCredit] = field(default_factory=list)
    # "Arcs" view: per-arc shelves (ordered like ``arc_slots``) plus
    # whatever's left over (issues that aren't in any of the volume's arcs).
    gallery_shelves: list[GalleryArcShelf] = field(default_factory=list)
    gallery_unaffiliated: list[VolumeIssueRow] = field(default_factory=list)
    # "Gallery" view: volume-order shelves grouped by arc fingerprint.
    # Each segment is a contiguous run of issues sharing the same arc set,
    # rendered with nested tinted containers for the multi-arc case.
    gallery_segments: list[GallerySegment] = field(default_factory=list)
    # Initial pagination window for the List/Gallery views — the slice
    # the page lands on (centered on first owned issue with 2 above for
    # context). Centralised here so the route, the template's Alpine
    # init, and the arc rail all agree on the same window.
    initial_window_start: int = 0
    initial_window_size: int = DEFAULT_PAGE_SIZE
    # Arc-flow rail for the List view's initial window. None when the
    # volume has no arcs at all (nothing to draw).
    list_rail: "RailModel | None" = None
    # Arc-flow rail for the Gallery view's initial visible shelves.
    # None when no arcs span multiple shelves or cross volumes (every
    # arc is silent under the branch-appearance rule).
    gallery_rail: "RailModel | None" = None
    # Compact per-issue rail for the Arcs view (rendered lines-only).
    # Same model shape as ``list_rail``, just built with a smaller
    # ``row_height`` so the vertical extent fits per-arc shelves
    # rather than table rows — at 72px/issue the rail dwarfs the
    # Arcs main pane.
    arcs_rail: "RailModel | None" = None
    # Set when the page was reached from a team / creator volume card:
    # ``issues`` (and every arc / gallery / rail computation) is then
    # narrowed to the issues that entity is credited on. None for a
    # normal, unfiltered volume view.
    credit_filter: "CreditFilter | None" = None
    # Phase 11F. Non-issue files attached to this volume as supplements
    # (cover galleries, extras) — rendered in a section below the issue
    # run. Each is a ``SupplementRef`` (see app/services/local.py).
    supplements: "list[Any]" = field(default_factory=list)

    @property
    def owned_count(self) -> int:
        """Back-compat alias for ``owned_issue_count``."""
        return self.owned_issue_count

    @property
    def volume_format(self) -> str:
        """Classified format — ongoing / limited / one_shot /
        collection — for the page's format badge. Authoritative when
        the volume's scraped themes name a type; otherwise heuristic."""
        return classify_cv_volume(self.volume)

    @property
    def volume_status(self) -> str | None:
        """Publication status — ongoing / complete / cancelled /
        unfinished — derived from the volume's scraped CV themes, or
        None when no status theme is present."""
        return volume_status_from_themes(
            getattr(self.volume, "themes", None)
        )


# Solid colors for the per-row arc stripes. Mid-saturation so the bars
# read as distinct columns running down the table without dominating.
# Cycled by first-seen order when a volume has more arcs than entries.
# Two parallel tuples kept in sync — bg-* for the stripes, text-* for
# the per-row prev/next arrows on the issues table. The text tone is one
# step darker (-500 vs -400) for legibility on white row backgrounds.
_ARC_BG_PALETTE: tuple[str, ...] = (
    "bg-amber-400",
    "bg-emerald-400",
    "bg-sky-400",
    "bg-violet-400",
    "bg-rose-400",
    "bg-cyan-400",
    "bg-lime-400",
    "bg-fuchsia-400",
)
_ARC_TEXT_PALETTE: tuple[str, ...] = (
    "text-amber-500",
    "text-emerald-500",
    "text-sky-500",
    "text-violet-500",
    "text-rose-500",
    "text-cyan-500",
    "text-lime-500",
    "text-fuchsia-500",
)
# The gallery's container fills use the *same* swatch colors as the
# legend, not a muted variant. Kept as its own palette/tuple even though
# it currently mirrors ``_ARC_BG_PALETTE`` so callers can later split
# them again (e.g., for a "subtle theme" option) without churn.
_ARC_TINT_PALETTE: tuple[str, ...] = _ARC_BG_PALETTE
# Higher-contrast tier used as the background fill for the legend's
# arc-name pills, where dark text on the lighter 400-tier doesn't read
# reliably across all eight hues (purple/rose/fuchsia especially get
# muddy). One step deeper (500-tier) pairs cleanly with white text on
# every entry while staying recognizably the same hue as the 400-tier
# stripes / containers — so the legend → container color mapping
# remains obvious at a glance.
_ARC_PILL_BG_PALETTE: tuple[str, ...] = (
    "bg-amber-500",
    "bg-emerald-500",
    "bg-sky-500",
    "bg-violet-500",
    "bg-rose-500",
    "bg-cyan-600",
    "bg-lime-600",
    "bg-fuchsia-500",
)


@dataclass
class CreditRow:
    cv_id: int
    name: str
    role: str | None = None  # person credits include a role
    # Only populated for story-arc credits, where CV occasionally
    # namespaces the arc name with a quoted parent book (parsed out
    # via ``parse_arc_name``). Other credit types leave this None.
    primary_book: str | None = None


@dataclass
class IssueNeighbor:
    cv_id: int
    issue_number: str | None
    name: str | None
    cover_url: str | None = None
    # True when at least one auto/confirmed FileMatch exists for this
    # issue. Drives the "missing" dimming on the issue rail's prev/
    # next thumbnails so the user can see at a glance whether the
    # neighbor is in their library or not.
    owned: bool = False


@dataclass
class IssueArcBranch:
    """One arc branch off the issue page's rail node.

    Represents a prev or next arc member that lives in a different
    volume from the current issue (or in a volume we don't have
    cached at all — we err toward showing the branch in that case
    since the user can still navigate to it and the cv_cache will
    fetch on demand). Renders as a colored horizontal line + arrow
    on the issue rail, left for ``direction='prev'``, right for
    ``direction='next'``.

    In-volume prev/next arc members are NOT branches — they're
    reachable via the rail's prev/next thumbnails (which are
    issue-number neighbors, though not necessarily arc neighbors)
    or the volume page's per-row arrows. The rail's branches are
    specifically about cross-volume continuation.
    """

    arc: "CreditRow"
    color: str  # hex from _ARC_HEX_PALETTE
    direction: str  # "prev" | "next"
    target_cv_id: int
    target_issue_number: str | None = None
    target_issue_name: str | None = None
    target_volume_name: str | None = None
    target_volume_year: int | None = None


@dataclass
class MatchedFile:
    """A file currently matched to this issue — one entry per non-missing
    on-disk location, shown in the issue page's "Files on disk" list.

    Carries ``file_id`` (not just the path) so the page can offer a
    "Fix match" link into the per-file review search flow. That link is
    the only correction path for an AUTO match: AUTO matches clear the
    confidence floor and never enter the review queue, so a wrong one
    (e.g. a DC book matched to an international-publisher volume) has no
    other way to be re-pointed.
    """

    file_id: Any
    path: str
    status: str  # MatchStatus value — "auto" or "confirmed"


@dataclass
class IssueDetail:
    issue: CvIssue
    volume: CvVolume | None
    publisher_name: str | None
    cover_url: str | None
    banner_url: str | None  # CV's landscape screen image, if any
    description: str | None
    # Short one-line summary from CV's ``deck`` field. See VolumeDetail.deck.
    deck: str | None
    persons: list[CreditRow]
    characters: list[CreditRow]
    story_arcs: list[CreditRow]
    teams: list[CreditRow]
    matched_files: list[MatchedFile]
    prev_neighbor: IssueNeighbor | None
    next_neighbor: IssueNeighbor | None
    # True when at least one auto/confirmed FileMatch points at the
    # current issue — i.e., the user has the file. Drives the dim
    # treatment on the hero cover thumb so a missing issue's main
    # visual reads as "not in your library yet."
    owned: bool = False
    # Cross-volume arc branches for the right-sidebar issue rail.
    # One entry per (arc, direction) where the immediate prev/next
    # member of an arc lives outside this issue's volume. In-volume
    # arc members aren't here — those are reachable through the
    # rail's prev/next thumbnails and the volume page.
    arc_branches: list[IssueArcBranch] = field(default_factory=list)
    # True when this volume contains issues earlier/later than the
    # ones shown as the prev/next rail thumbnails. Drives the spine
    # fade extensions on the issue rail so the user knows there's
    # more to navigate beyond the two visible neighbors.
    has_earlier_issues: bool = False
    has_later_issues: bool = False
    # Per-arc pill background classes (500/600-tier ``bg-*`` palette
    # paired with white text — same palette used on the volume page
    # legend). Keyed by arc cv_id. Indexed by position in
    # ``story_arcs`` so the same arc gets the same hue across the
    # rail branches and the per-page pill, which makes "this pill =
    # this rail line" obvious at a glance.
    arc_pill_bg_color_classes: dict[int, str] = field(default_factory=dict)


@dataclass
class RecentlyAddedVolume:
    """One volume in the home page's "Recently added" list.

    The list is grouped by volume — not one entry per file — so
    importing a run of issues for a single series doesn't flood it.
    ``kind`` discriminates CV vs local; ``matched_at`` is the volume's
    most recent match (it drives ordering); ``issue_count`` is the
    distinct matched issues the user owns in the volume."""

    kind: str  # "cv" | "local"
    cv_id: int | None  # CV volume id, on a "cv" row
    local_id: uuid.UUID | None  # local_volumes id, on a "local" row
    name: str
    year: int | None
    cover_url: str | None
    issue_count: int
    matched_at: Any

    @property
    def detail_url(self) -> str:
        """Link target for this volume's card on the home page."""
        if self.kind == "local":
            return f"/local/volume/{self.local_id}"
        return f"/volume/{self.cv_id}"


# ---- Library index ------------------------------------------------------


@dataclass
class LibraryFilters:
    publisher_cv_id: int | None = None
    year: int | None = None
    has_missing_only: bool = False
    sort: str = "name"  # 'name' | 'year' | 'owned' | 'missing'
    # Single uppercase letter ``A``..``Z`` or the literal ``"#"`` to
    # restrict the library list to volumes whose name starts with
    # that letter. ``"#"`` matches anything that doesn't start with
    # an ASCII letter (numeric titles like "100 Bullets", "52",
    # leading punctuation, etc.). ``None`` (default) is "no
    # restriction".
    name_starts_with: str | None = None
    # Case-insensitive substring search on the volume name —
    # ``%query%`` ILIKE match. None / empty string disables it.
    # LIKE wildcards (``%``, ``_``, ``\``) in the user input are
    # escaped so a query like ``"50%"`` matches a literal percent
    # sign instead of triggering wildcard behavior.
    name_query: str | None = None
    # Classified-format facet — one of "ongoing" / "limited" /
    # "one_shot" / "collection". ``None`` means no restriction. Format
    # is a heuristic (not a stored column), so this facet is applied by
    # classifying the candidate volumes in Python — see
    # ``list_library_volumes``. CV-only: local volumes have no format,
    # so the facet excludes them entirely.
    format: str | None = None


@dataclass
class _LibKey:
    """Lightweight sort key for one /library row.

    The CV and local sides of ``list_library_volumes`` are pulled as
    these keys — just enough to merge + sort + paginate — so only the
    final page's rows pay the cost of loading covers / publisher names.
    """

    kind: str  # "cv" | "local"
    cv_id: int  # 0 for local rows
    local_id: uuid.UUID | None
    name: str
    year: int | None
    owned_count: int
    count_of_issues: int | None  # always None for local volumes


def _lib_sort_key(sort: str):
    """Python sort key reproducing the SQL ordering ``list_library_volumes``
    used before local volumes were merged in. ``name`` (case-insensitive)
    is the universal tiebreaker for determinism."""
    if sort == "year":
        # Year descending, nulls last; then name ascending.
        return lambda k: (k.year is None, -(k.year or 0), k.name.lower())
    if sort == "owned":
        return lambda k: (-k.owned_count, k.name.lower())
    if sort == "missing":
        # Mirrors the old SQL ``case`` expr: 0 when the CV total is
        # unknown (true of every local volume), else total - owned —
        # left unclamped, exactly as the SQL was.
        def missing(k: _LibKey) -> int:
            if k.count_of_issues is None:
                return 0
            return k.count_of_issues - k.owned_count

        return lambda k: (-missing(k), k.name.lower())
    # 'name' (default): name ascending, then year ascending nulls last.
    return lambda k: (k.name.lower(), k.year is None, k.year or 0)


async def list_library_volumes(
    db: AsyncSession,
    filters: LibraryFilters | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    user_id: uuid.UUID | None = None,
) -> tuple[list[LibraryVolumeRow], int]:
    """Volumes the user has at least one matched file in — CV-cache
    volumes (auto/confirmed matches) and user-authored local volumes
    (Phase 11) merged into one list.

    Returns ``(rows, total)`` where ``total`` is the unpaginated count
    of volumes matching ``filters``. Callers paginating via
    ``limit``/``offset`` use ``total`` to drive "load more" UI; non-
    paginating callers (e.g., admin tools) can pass ``limit=None`` to
    get the full list and ignore ``total``.

    Local volumes are merged in unless a filter can't apply to them: a
    ``publisher_cv_id`` facet (local publishers are free text, not a
    ``cv_publishers`` FK) or ``has_missing_only`` (a local volume has no
    CV issue total, so "missing" is undefined). Pagination happens after
    the union — both sides are pulled as lightweight ``_LibKey`` sort
    keys, merged + sorted in Python, sliced, and only the page's rows
    then hydrate covers / publisher names.
    """
    f = filters or LibraryFilters()

    # ---- CV side: lightweight sort keys (no raw_payload) -------------
    #
    # Owned count per volume: distinct ISSUES with at least one matched
    # (auto, confirmed) file. Counting issues — not files — keeps this
    # consistent with the volume page's progress ring and makes
    # "X / total · N missing" arithmetic add up even when the user has
    # duplicates (multiple files for the same issue, e.g., variants).
    owned_per_volume = (
        select(
            CvIssue.volume_cv_id.label("volume_cv_id"),
            func.count(func.distinct(FileMatch.issue_cv_id)).label("owned_count"),
        )
        .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .where(
            FileMatch.status.in_(
                (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
            )
        )
        .group_by(CvIssue.volume_cv_id)
        .subquery()
    )

    # Per-volume max issue cover_date year, used by the year filter
    # to express "spans Y" rather than "started in Y". Only built
    # when ``f.year`` is set so the unfiltered case doesn't carry an
    # unused join.
    year_subq = None
    if f.year is not None:
        year_subq = (
            select(
                CvIssue.volume_cv_id.label("vid"),
                func.max(
                    func.extract("year", CvIssue.cover_date)
                ).label("max_year"),
            )
            .where(CvIssue.cover_date.is_not(None))
            .group_by(CvIssue.volume_cv_id)
            .subquery()
        )

    conditions = []
    if f.publisher_cv_id is not None:
        conditions.append(CvVolume.publisher_cv_id == f.publisher_cv_id)
    if f.year is not None:
        # "Spans Y": the volume started in or before Y AND its latest
        # cv_issues.cover_date is in or after Y. When ``last_issue``
        # is unhydrated (cv_issues stub with cover_date=NULL), this
        # underestimates the run — but ``_upsert_volume`` eagerly
        # enqueues hydration for first_issue and last_issue on every
        # volume add, so once the background worker drains the
        # max_year here reflects CV's real run boundary.
        #
        # Fallback when there are no dated issues at all (brand-new
        # volume, complete stub, ``last_issue`` job hasn't drained
        # yet): match if ``start_year == Y`` so plausibly-related
        # one-shots and very-recent adds don't silently disappear.
        conditions.append(CvVolume.year.is_not(None))
        conditions.append(CvVolume.year <= f.year)
        conditions.append(
            or_(
                year_subq.c.max_year >= f.year,
                and_(
                    year_subq.c.max_year.is_(None),
                    CvVolume.year == f.year,
                ),
            )
        )
    if f.has_missing_only:
        conditions.append(CvVolume.count_of_issues.is_not(None))
        conditions.append(CvVolume.count_of_issues > owned_per_volume.c.owned_count)
    if f.name_starts_with:
        if f.name_starts_with == "#":
            # Non-alpha bucket: anything that doesn't start A..Z (case-
            # insensitive). Catches "100 Bullets", "52", etc.
            first = func.lower(func.substr(CvVolume.name, 1, 1))
            conditions.append(~first.between("a", "z"))
        else:
            letter = f.name_starts_with[:1].lower()
            conditions.append(func.lower(CvVolume.name).like(f"{letter}%"))
    if f.name_query and f.name_query.strip():
        # Escape LIKE wildcards in user input so e.g. ``"50%"``
        # matches the literal string "50%" rather than triggering
        # wildcard behavior. Backslash is the escape; it itself
        # gets doubled so a literal backslash in input still works.
        q = f.name_query.strip()
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(CvVolume.name.ilike(f"%{escaped}%", escape="\\"))

    keys_stmt = select(
        CvVolume.cv_id,
        CvVolume.name,
        CvVolume.year,
        CvVolume.count_of_issues,
        owned_per_volume.c.owned_count,
    ).join(
        owned_per_volume,
        owned_per_volume.c.volume_cv_id == CvVolume.cv_id,
    )
    if year_subq is not None:
        keys_stmt = keys_stmt.outerjoin(
            year_subq, year_subq.c.vid == CvVolume.cv_id
        )
    for cond in conditions:
        keys_stmt = keys_stmt.where(cond)

    keys: list[_LibKey] = [
        _LibKey(
            kind="cv",
            cv_id=cv_id,
            local_id=None,
            name=name or "",
            year=year,
            owned_count=owned,
            count_of_issues=count,
        )
        for cv_id, name, year, count, owned in (
            await db.execute(keys_stmt)
        ).all()
    ]

    # ---- Format facet — classify and filter, when requested ---------
    # A volume's format is a heuristic over its issue count plus name /
    # deck / description keywords, not a stored column, so it's computed
    # here only when the facet is on. ``keys`` is still all-CV at this
    # point (locals are added below) — which is right, a local volume
    # has no format. Only the JSON text fields the classifier needs are
    # pulled, not the whole (issue-list-heavy) raw_payload.
    if f.format is not None and keys:
        fmt_stmt = select(
            CvVolume.cv_id,
            CvVolume.name,
            CvVolume.count_of_issues,
            CvVolume.raw_payload["deck"].astext,
            CvVolume.raw_payload["description"].astext,
            CvVolume.raw_payload["first_issue"]["name"].astext,
        ).where(CvVolume.cv_id.in_([k.cv_id for k in keys]))
        matching: set[int] = set()
        for cv_id, name, count, deck, desc, first_name in (
            await db.execute(fmt_stmt)
        ).all():
            if (
                classify_volume_format(
                    name=name,
                    count_of_issues=count,
                    deck=deck,
                    description=desc,
                    first_issue_name=first_name,
                )
                == f.format
            ):
                matching.add(cv_id)
        keys = [k for k in keys if k.cv_id in matching]

    # ---- Local side: merged unless a CV-only filter rules it out -----
    # A ``publisher_cv_id`` facet can't match a free-text local
    # publisher, ``has_missing_only`` needs a CV issue total a local
    # volume doesn't have, and the format facet is CV-only — in any of
    # those cases local volumes are simply absent from the results.
    if (
        f.publisher_cv_id is None
        and not f.has_missing_only
        and f.format is None
    ):
        keys.extend(await _local_library_keys(db, f))

    # ---- Merge: sort the union, then paginate ------------------------
    keys.sort(key=_lib_sort_key(f.sort))
    total = len(keys)
    page = keys if limit is None else keys[offset : offset + limit]

    # ---- Hydrate only the page's rows --------------------------------
    cv_ids = [k.cv_id for k in page if k.kind == "cv"]
    local_ids = [k.local_id for k in page if k.kind == "local"]
    owned_by_cv = {k.cv_id: k.owned_count for k in page if k.kind == "cv"}
    owned_by_local = {
        k.local_id: k.owned_count for k in page if k.kind == "local"
    }

    cv_rows: dict[int, LibraryVolumeRow] = {}
    if cv_ids:
        hstmt = (
            select(CvVolume, CvPublisher.name.label("publisher_name"))
            .outerjoin(
                CvPublisher, CvPublisher.cv_id == CvVolume.publisher_cv_id
            )
            .where(CvVolume.cv_id.in_(cv_ids))
        )
        for vol, publisher_name in (await db.execute(hstmt)).all():
            is_stub = (
                isinstance(vol.raw_payload, dict)
                and vol.raw_payload.get("_stub") is True
            )
            cv_rows[vol.cv_id] = LibraryVolumeRow(
                cv_id=vol.cv_id,
                name=vol.name,
                year=vol.year,
                publisher_name=publisher_name,
                count_of_issues=vol.count_of_issues,
                owned_count=owned_by_cv.get(vol.cv_id, 0),
                # "medium" (~400-600px tall) is the right tier for grid
                # cards: crisp on retina without burning bandwidth on the
                # super_url (~1500px) every other card would otherwise pull.
                cover_url=cv_image_url(vol.raw_payload, "medium"),
                is_stub=is_stub,
                # Classified for the card's format badge — cheap here,
                # the full volume row is already loaded.
                format=classify_cv_volume(vol),
            )

    local_rows: dict[uuid.UUID, LibraryVolumeRow] = {}
    if local_ids:
        covers = await _local_volume_cover_files(db, local_ids)
        lstmt = select(LocalVolume).where(LocalVolume.id.in_(local_ids))
        for lv in (await db.execute(lstmt)).scalars():
            file_id = covers.get(lv.id)
            local_rows[lv.id] = LibraryVolumeRow(
                cv_id=0,
                name=lv.name,
                year=lv.year,
                publisher_name=lv.publisher_name,
                # A local volume has no CV issue total — "owned / total"
                # collapses to a bare owned count, and "missing" is
                # undefined (``missing_count`` returns None).
                count_of_issues=None,
                owned_count=owned_by_local.get(lv.id, 0),
                # A local cover is the matched file's own first page,
                # served by the (Phase 11C non-admin) file-cover route.
                cover_url=(
                    f"/review/file/{file_id}/cover" if file_id else None
                ),
                is_stub=False,
                kind="local",
                local_id=lv.id,
            )

    # ---- Reading progress: finished-issue count per volume (Phase 6) -
    # Scoped to the page's volumes, like cover hydration. Counted from
    # file_matches joined to the user's finished read_progress rows, so
    # ``read_count <= owned_count`` holds. Skipped without a user.
    if user_id is not None and cv_ids:
        cv_read = (
            await db.execute(
                select(
                    CvIssue.volume_cv_id,
                    func.count(func.distinct(FileMatch.issue_cv_id)),
                )
                .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
                .join(
                    ReadProgress, ReadProgress.file_id == FileMatch.file_id
                )
                .where(CvIssue.volume_cv_id.in_(cv_ids))
                .where(
                    FileMatch.status.in_(
                        (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                    )
                )
                .where(ReadProgress.user_id == user_id)
                .where(ReadProgress.finished_at.is_not(None))
                .group_by(CvIssue.volume_cv_id)
            )
        ).all()
        for vid, n in cv_read:
            if vid in cv_rows:
                cv_rows[vid].read_count = n
    if user_id is not None and local_ids:
        local_read = (
            await db.execute(
                select(
                    LocalIssue.local_volume_id,
                    func.count(func.distinct(FileMatch.local_issue_id)),
                )
                .join(FileMatch, FileMatch.local_issue_id == LocalIssue.id)
                .join(
                    ReadProgress, ReadProgress.file_id == FileMatch.file_id
                )
                .where(LocalIssue.local_volume_id.in_(local_ids))
                .where(FileMatch.status == MatchStatus.LOCAL.value)
                .where(ReadProgress.user_id == user_id)
                .where(ReadProgress.finished_at.is_not(None))
                .group_by(LocalIssue.local_volume_id)
            )
        ).all()
        for lvid, n in local_read:
            if lvid in local_rows:
                local_rows[lvid].read_count = n

    # Reassemble in the merged sort order; a row absent from hydration
    # (e.g. deleted between the key scan and now) is skipped.
    out: list[LibraryVolumeRow] = []
    for k in page:
        row = (
            cv_rows.get(k.cv_id)
            if k.kind == "cv"
            else local_rows.get(k.local_id)
        )
        if row is not None:
            out.append(row)
    return out, total


async def _local_library_keys(
    db: AsyncSession, f: LibraryFilters
) -> list[_LibKey]:
    """Sort keys for the local-volume side of ``list_library_volumes``.

    Applies the name / year filters that make sense for a local volume;
    the caller has already ruled out the publisher facet and
    ``has_missing_only``. A local volume appears only when it has at
    least one matched (``LOCAL``) file — the same "owned" gate the CV
    side applies via ``owned_per_volume``."""
    local_owned = (
        select(
            LocalIssue.local_volume_id.label("lv_id"),
            func.count(func.distinct(FileMatch.local_issue_id)).label(
                "owned_count"
            ),
        )
        .join(FileMatch, FileMatch.local_issue_id == LocalIssue.id)
        .where(FileMatch.status == MatchStatus.LOCAL.value)
        .group_by(LocalIssue.local_volume_id)
        .subquery()
    )
    stmt = select(
        LocalVolume.id,
        LocalVolume.name,
        LocalVolume.year,
        local_owned.c.owned_count,
    ).join(local_owned, local_owned.c.lv_id == LocalVolume.id)
    if f.year is not None:
        # No "spans Y" notion for a local volume — match the start year.
        stmt = stmt.where(LocalVolume.year == f.year)
    if f.name_starts_with:
        if f.name_starts_with == "#":
            first = func.lower(func.substr(LocalVolume.name, 1, 1))
            stmt = stmt.where(~first.between("a", "z"))
        else:
            letter = f.name_starts_with[:1].lower()
            stmt = stmt.where(
                func.lower(LocalVolume.name).like(f"{letter}%")
            )
    if f.name_query and f.name_query.strip():
        q = f.name_query.strip()
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(LocalVolume.name.ilike(f"%{escaped}%", escape="\\"))
    return [
        _LibKey(
            kind="local",
            cv_id=0,
            local_id=lv_id,
            name=name or "",
            year=year,
            owned_count=owned,
            count_of_issues=None,
        )
        for lv_id, name, year, owned in (await db.execute(stmt)).all()
    ]


async def _local_volume_cover_files(
    db: AsyncSession, local_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    """Pick one representative cover file per local volume — the file
    matched to the volume's lowest-numbered issue. Returns
    ``{local_volume_id: file_id}``; a volume with no matched file is
    simply absent (its card renders the no-cover placeholder)."""
    stmt = (
        select(
            LocalIssue.local_volume_id,
            LocalIssue.issue_number,
            FileMatch.file_id,
        )
        .join(FileMatch, FileMatch.local_issue_id == LocalIssue.id)
        .where(FileMatch.status == MatchStatus.LOCAL.value)
        .where(LocalIssue.local_volume_id.in_(local_ids))
    )
    # lv_id -> (issue sort key, file_id) for the best (lowest) issue seen.
    best: dict[uuid.UUID, tuple] = {}
    for lv_id, issue_number, file_id in (await db.execute(stmt)).all():
        sort_key = sort_key_issue_number(issue_number)
        if lv_id not in best or sort_key < best[lv_id][0]:
            best[lv_id] = (sort_key, file_id)
    return {lv_id: file_id for lv_id, (_sk, file_id) in best.items()}


async def get_hydrated_library_rows(
    db: AsyncSession,
    volume_ids: list[int],
) -> list[LibraryVolumeRow]:
    """Resolve the subset of ``volume_ids`` whose ``cv_volumes`` row
    is no longer a stub, returning a fully-formed ``LibraryVolumeRow``
    for each. Stub IDs are silently dropped — callers infer "still
    pending" by absence.

    Used by the /library page's hydration-polling endpoint. The
    rendering side reuses the same ``library_grid_card`` and
    ``library_table_row`` macros as the initial page render so the
    swapped-in card is byte-for-byte identical to one that arrived
    hydrated on first load.
    """
    if not volume_ids:
        return []

    owned_per_volume = (
        select(
            CvIssue.volume_cv_id.label("volume_cv_id"),
            func.count(func.distinct(FileMatch.issue_cv_id)).label("owned_count"),
        )
        .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .where(
            FileMatch.status.in_(
                (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
            )
        )
        .group_by(CvIssue.volume_cv_id)
        .subquery()
    )
    stmt = (
        select(
            CvVolume,
            CvPublisher.name.label("publisher_name"),
            owned_per_volume.c.owned_count,
        )
        .join(
            owned_per_volume,
            owned_per_volume.c.volume_cv_id == CvVolume.cv_id,
        )
        .outerjoin(CvPublisher, CvPublisher.cv_id == CvVolume.publisher_cv_id)
        .where(CvVolume.cv_id.in_(volume_ids))
    )
    out: list[LibraryVolumeRow] = []
    for vol, publisher_name, owned in (await db.execute(stmt)).all():
        # ``_stub`` marker means the row is still a placeholder. The
        # auto-refresh client just leaves these in its pending set
        # and asks again on the next poll tick.
        is_stub = (
            isinstance(vol.raw_payload, dict)
            and vol.raw_payload.get("_stub") is True
        )
        if is_stub:
            continue
        out.append(
            LibraryVolumeRow(
                cv_id=vol.cv_id,
                name=vol.name,
                year=vol.year,
                publisher_name=publisher_name,
                count_of_issues=vol.count_of_issues,
                owned_count=owned,
                cover_url=cv_image_url(vol.raw_payload, "medium"),
                is_stub=False,
            )
        )
    return out


async def list_publishers_in_library(
    db: AsyncSession,
) -> list[tuple[int, str, str | None]]:
    """For the publisher filter dropdown and the active-filter chip on
    the library page — publishers represented in the library (i.e.,
    publisher of any volume the user owns at least one issue from).
    Sorted by name.

    Each entry is ``(cv_id, name, icon_url)``. ``icon_url`` comes from
    ``raw_payload.image.icon_url`` via the shared ``cv_image_url``
    helper and is ``None`` for stub publishers that don't yet have
    image data."""
    owned_volume_ids = (
        select(CvIssue.volume_cv_id)
        .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .where(
            FileMatch.status.in_(
                (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
            )
        )
    )
    publisher_ids = (
        select(CvVolume.publisher_cv_id)
        .where(CvVolume.cv_id.in_(owned_volume_ids))
        .where(CvVolume.publisher_cv_id.is_not(None))
    )
    # Filter via IN (subquery) rather than JOIN + DISTINCT. Postgres rejects
    # ``SELECT DISTINCT ... ORDER BY lower(name)`` because the ORDER BY
    # expression isn't in the select list; the IN form sidesteps that
    # restriction and is also (marginally) more readable.
    stmt = (
        select(CvPublisher.cv_id, CvPublisher.name, CvPublisher.raw_payload)
        .where(CvPublisher.cv_id.in_(publisher_ids))
        .order_by(func.lower(CvPublisher.name))
    )
    return [
        (cv_id, name, cv_image_url(raw_payload, "icon"))
        for cv_id, name, raw_payload in (await db.execute(stmt)).all()
    ]


# ---- Volume detail ------------------------------------------------------


def _credited_issue_ids(raw: dict) -> set[int]:
    """Issue cv_ids a team / creator is credited on.

    Read from CV's ``issue_credits`` (the team resource's key), with
    ``issues`` tolerated as a fallback — the person resource names the
    same per-issue list differently. A missing / malformed entry is
    skipped rather than raising, so a partial payload still yields a
    usable set.
    """
    refs = raw.get("issue_credits") or raw.get("issues") or []
    out: set[int] = set()
    for ref in refs:
        if isinstance(ref, dict) and ref.get("id") is not None:
            out.add(int(ref["id"]))
    return out


async def get_volume_detail(
    db: AsyncSession,
    cv_id: int,
    *,
    cv_cache: ComicVineCache | None = None,
    rail_window_start: int | None = None,
    rail_window_size: int | None = None,
    from_issue_cv_id: int | None = None,
    user_id: uuid.UUID | None = None,
    credit_filter: tuple[str, int] | None = None,
) -> VolumeDetail | None:
    """Volume page payload. Returns None if the volume doesn't exist.

    When ``cv_cache`` is provided, the function makes one extra step:
    after collecting arc IDs from already-hydrated issues' payloads, it
    fetches each arc via ``cv_cache.get_story_arc`` and uses the arc's
    ``raw_payload.issues`` (which lists every member issue across every
    volume) to populate arc membership for *all* of this volume's issues,
    including stubs that haven't been individually hydrated. This trades
    N per-issue calls for K per-arc calls — usually a big win, since
    most small volumes have only one or two arcs.

    ``credit_filter`` — given as ``(kind, cv_id)`` where ``kind`` is
    ``"team"``, ``"creator"`` or ``"character"`` — narrows the page to
    the issues that entity is credited on. The entity is fetched through ``cv_cache``,
    its credited-issue list intersected with this volume's issues, and
    everything downstream (arc fill-in, gallery, segments, rails,
    pagination) then describes just the matching run. Needs
    ``cv_cache``; ignored without one. An entity that can't be resolved
    leaves the volume unfiltered.
    """
    # Source the volume through the cache when one's available:
    # ``cv_cache.get_volume`` covers three cases in one place —
    #   1. Brand-new volume the user is navigating to for the first
    #      time (no DB row at all) → fetches and upserts on demand.
    #   2. FK-only stub row (created by ``_upsert_volume_stub`` when
    #      some issue/arc/search payload referenced a volume we
    #      hadn't fetched yet) → detected via the ``_stub: True``
    #      marker and re-fetched.
    #   3. Already-hydrated row → returns it (revalidates in the
    #      background if stale, but that's its own concern).
    # ``_upsert_volume`` writes both the volume payload and a stub
    # for every issue in CV's nested list, which is what populates
    # the volume page's issues table for a never-visited volume.
    #
    # ``ComicVineNotFoundError`` is folded into the existing
    # "return None → route 404" path; the rate-limit / generic CV
    # errors propagate up to the route, which renders the friendly
    # ``_load_error.html`` page via ``_cv_error_response``.
    #
    # Tests that pass ``cv_cache=None`` keep the bare ``db.get`` path
    # so they don't need a stand-in client.
    volume: CvVolume | None
    if cv_cache is not None:
        try:
            volume = await cv_cache.get_volume(db, cv_id)
        except (
            ComicVineNotFoundError,
            ComicVineKeyMissingError,
            ComicVineKeyInvalidError,
        ):
            # Three reasons CV says "no" that we treat as cache-miss
            # rather than transient errors:
            #   * NotFound (101) — CV explicitly doesn't know this ID.
            #   * KeyMissing — no API key configured at all; we can't
            #     ask CV for anything. Fall back to whatever we have
            #     locally; the admin sees a separate banner about
            #     configuring the key.
            #   * KeyInvalid — key present but CV rejects it. Same
            #     story; admin needs to re-paste.
            # In all three cases, route 404 is the right user-facing
            # answer: from their POV the entity isn't visible.
            volume = await db.get(CvVolume, cv_id)
    else:
        volume = await db.get(CvVolume, cv_id)
    if volume is None:
        return None

    publisher_name: str | None = None
    if volume.publisher_cv_id is not None:
        pub = await db.get(CvPublisher, volume.publisher_cv_id)
        if pub is not None:
            publisher_name = pub.name

    # ---- Credit filter: resolve the team / creator up front ----------
    # When the page was reached from a volume card on a team / creator
    # page, ``credit_filter`` is ``(kind, cv_id)``. Resolve that entity
    # and its credited-issue id set here so the intersection can be
    # applied to the raw issue rows *before* the arc / gallery / rail
    # machinery runs — every downstream computation then naturally
    # describes just the matching run. An entity that can't be fetched
    # leaves ``credited_ids`` None, i.e. the volume renders unfiltered.
    credit_kind: str | None = None
    credit_entity_cv_id: int | None = None
    credit_entity_name = ""
    credited_ids: set[int] | None = None
    if credit_filter is not None and cv_cache is not None:
        credit_kind, credit_entity_cv_id = credit_filter
        entity: Any = None
        try:
            if credit_kind == "team":
                entity = await cv_cache.get_team(db, credit_entity_cv_id)
            elif credit_kind == "creator":
                entity = await cv_cache.get_person(db, credit_entity_cv_id)
            elif credit_kind == "character":
                entity = await cv_cache.get_character(
                    db, credit_entity_cv_id
                )
        except ComicVineError:
            entity = None
        if entity is not None:
            entity_payload = entity.raw_payload or {}
            credited_ids = _credited_issue_ids(entity_payload)
            credit_entity_name = (
                entity.name or entity_payload.get("name") or ""
            )
        else:
            credit_kind = None  # unresolved → no filter

    # Count of distinct owned issues in this volume — the global value
    # used by the progress ring. Computed straight from file_matches
    # so it's accurate even when the cv_issues stub list is partial
    # (e.g., CV's volume payload truncated the nested issues array).
    owned_issue_count_stmt = (
        select(func.count(func.distinct(FileMatch.issue_cv_id)))
        .join(CvIssue, CvIssue.cv_id == FileMatch.issue_cv_id)
        .where(
            CvIssue.volume_cv_id == cv_id,
            FileMatch.status.in_(
                (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
            ),
        )
    )
    owned_issue_count = (
        (await db.execute(owned_issue_count_stmt)).scalar() or 0
    )

    # Reading progress: distinct issues the user has finished in this
    # volume — the reading ring's numerator. Mirrors the owned-count
    # query with a join onto the user's finished read_progress rows;
    # stays 0 without a user (e.g. the rail fragment).
    finished_issue_count = 0
    if user_id is not None:
        finished_issue_count = (
            await db.execute(
                select(func.count(func.distinct(FileMatch.issue_cv_id)))
                .join(CvIssue, CvIssue.cv_id == FileMatch.issue_cv_id)
                .join(
                    ReadProgress, ReadProgress.file_id == FileMatch.file_id
                )
                .where(
                    CvIssue.volume_cv_id == cv_id,
                    FileMatch.status.in_(
                        (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                    ),
                    ReadProgress.user_id == user_id,
                    ReadProgress.finished_at.is_not(None),
                )
            )
        ).scalar() or 0

    # All issues for this volume, plus a flag for whether we own each.
    issues_stmt = (
        select(
            CvIssue,
            # boolean: do we have a matched file pointing at this issue?
            func.coalesce(
                func.bool_or(
                    FileMatch.status.in_(
                        (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                    )
                ),
                False,
            ).label("owned"),
        )
        .outerjoin(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .where(CvIssue.volume_cv_id == cv_id)
        .group_by(CvIssue.cv_id)
    )
    issue_rows = (await db.execute(issues_stmt)).all()

    # Credit filter — narrow to the team / creator's credited issues
    # before anything else looks at the rows, so arc collection,
    # gallery segmentation and the rails all see only the matching
    # run. ``credit`` (built here) carries the before/after counts for
    # the page banner.
    credit: CreditFilter | None = None
    if credited_ids is not None and credit_kind is not None:
        volume_total = len(issue_rows)
        issue_rows = [
            row for row in issue_rows if row[0].cv_id in credited_ids
        ]
        credit = CreditFilter(
            kind=credit_kind,
            cv_id=credit_entity_cv_id,
            name=credit_entity_name,
            matched=len(issue_rows),
            volume_total=volume_total,
        )

    issues: list[VolumeIssueRow] = []
    # Cleaned arc names (display string, no quoted-book prefix) keyed by
    # arc CV id. ``arc_primary_books`` parallels it with the parsed-out
    # parent-book name (None when the raw arc name didn't carry the
    # ``"<book>" <arc>`` convention).
    arc_names: dict[int, str] = {}
    arc_primary_books: dict[int, str | None] = {}
    arc_first_seen_order: list[int] = []  # preserve order for stable colors

    for cv_issue, owned in issue_rows:
        issue_arc_credits: list[ArcCredit] = []
        # Harvest story arc credits from hydrated payloads (stubs have None
        # for the arc list — those issues get filled in below from the arc
        # payload, if cv_cache was provided).
        if isinstance(cv_issue.raw_payload, dict):
            for arc in cv_issue.raw_payload.get("story_arc_credits") or []:
                aid = arc.get("id")
                raw_name = arc.get("name")
                if aid is None or not raw_name:
                    continue
                primary_book, clean_name = parse_arc_name(raw_name)
                issue_arc_credits.append(ArcCredit(
                    cv_id=int(aid),
                    name=clean_name,
                    primary_book=primary_book,
                ))
                if aid not in arc_names:
                    arc_names[aid] = clean_name
                    arc_primary_books[aid] = primary_book
                    arc_first_seen_order.append(int(aid))

        issues.append(
            VolumeIssueRow(
                cv_id=cv_issue.cv_id,
                issue_number=cv_issue.issue_number,
                name=cv_issue.name,
                cover_date=cv_issue.cover_date,
                owned=bool(owned),
                cover_url=cv_image_url(cv_issue.raw_payload, "thumb"),
                arc_credits=issue_arc_credits,
                is_hydrated=cv_issue.fetched_at is not None,
            )
        )

    # Reading-progress bars for the issue list — one batch lookup over
    # the owned issues' matched files. Skipped without a user (e.g. the
    # rail fragment), leaving every row's ``progress`` at None.
    if user_id is not None:
        progress_by_issue = await issue_progress_for_volume(
            db, user_id, [i.cv_id for i in issues if i.owned]
        )
        for issue_row in issues:
            issue_row.progress = progress_by_issue.get(issue_row.cv_id)

    # With a credit filter active the SQL owned / finished counts above
    # span the whole volume — re-derive them from the filtered rows so
    # the sidebar rings agree with the issue list the page shows.
    if credit is not None:
        owned_issue_count = sum(1 for row in issues if row.owned)
        finished_issue_count = sum(
            1 for row in issues
            if row.progress is not None and row.progress.finished
        )

    # ---- Arc-driven fill-in + boundary arrows + gallery shelves --------
    # Each arc's CV payload contains an "issues" list spanning every
    # volume the arc touches. We use it for three things:
    #
    # 1. Fill arc membership on stub issues — those whose own payload
    #    doesn't yet have ``story_arc_credits`` get the arc attached so
    #    the stripe column lights up.
    # 2. Compute per-row boundary arrows — for each in-this-volume member
    #    of the arc, if its immediate prev/next member belongs to a
    #    different volume, attach a navigation link. Members whose
    #    neighbors are in this volume don't need arrows (the user can
    #    just read the next/previous row), so we omit those.
    # 3. Build gallery shelves — one per arc, containing the in-volume
    #    members in arc reading order, plus the very-first / very-last
    #    out-of-volume members as edge arrows for the whole shelf.
    arc_credit_for_aid: dict[int, ArcCredit] = {
        aid: ArcCredit(
            cv_id=aid,
            name=arc_names[aid],
            primary_book=arc_primary_books.get(aid),
        )
        for aid in arc_first_seen_order
    }
    arc_members_by_aid: dict[int, list[dict]] = {}
    issue_index = {row.cv_id: row for row in issues}
    this_volume_issue_ids = set(issue_index.keys())

    if cv_cache is not None and arc_first_seen_order:
        for aid in arc_first_seen_order:
            try:
                arc = await cv_cache.get_story_arc(db, aid)
            except ComicVineError:
                # A failed arc fetch shouldn't break the page — we just
                # fall back to whatever issue-payload data we already have.
                continue
            arc_members_by_aid[aid] = (arc.raw_payload or {}).get("issues") or []

    # Pass 1: per-row arc_credits + boundary arrows.
    for aid, members in arc_members_by_aid.items():
        arc_credit = arc_credit_for_aid[aid]
        for pos, member in enumerate(members):
            member_id = member.get("id")
            if member_id is None:
                continue
            mid = int(member_id)
            row = issue_index.get(mid)
            if row is None:
                continue
            if not any(ac.cv_id == aid for ac in row.arc_credits):
                row.arc_credits.append(arc_credit)
            if pos > 0:
                prev_member = members[pos - 1]
                prev_id = prev_member.get("id")
                if (
                    prev_id is not None
                    and int(prev_id) not in this_volume_issue_ids
                ):
                    row.prev_arc_links.append(
                        ArcSiblingLink(
                            arc=arc_credit,
                            issue_cv_id=int(prev_id),
                            issue_name=prev_member.get("name"),
                        )
                    )
            if pos + 1 < len(members):
                next_member = members[pos + 1]
                next_id = next_member.get("id")
                if (
                    next_id is not None
                    and int(next_id) not in this_volume_issue_ids
                ):
                    row.next_arc_links.append(
                        ArcSiblingLink(
                            arc=arc_credit,
                            issue_cv_id=int(next_id),
                            issue_name=next_member.get("name"),
                        )
                    )

    # ---- Arc display order: by earliest in-volume cover_date ----------
    # The Arcs view (and stripe legend) read better when arcs are
    # ordered by the publication date of their first in-volume member
    # rather than by discovery order. Arcs without dates (every member
    # still a stub) sort last and tie-break by first-seen index so the
    # result is deterministic.
    arc_first_volume_date: dict[int, date] = {}
    for issue in issues:
        if issue.cover_date is None:
            continue
        for ac in issue.arc_credits:
            existing = arc_first_volume_date.get(ac.cv_id)
            if existing is None or issue.cover_date < existing:
                arc_first_volume_date[ac.cv_id] = issue.cover_date
    arc_first_seen_idx = {aid: i for i, aid in enumerate(arc_first_seen_order)}
    arc_display_order = sorted(
        arc_first_seen_order,
        key=lambda aid: (
            arc_first_volume_date.get(aid, date.max),
            arc_first_seen_idx[aid],
        ),
    )

    # Pass 2: gallery shelves. Walk in publication order so shelves and
    # the legend/stripe colors share the same arc ordering.
    gallery_shelves: list[GalleryArcShelf] = []
    for aid in arc_display_order:
        members = arc_members_by_aid.get(aid)
        if not members:
            continue
        arc_credit = arc_credit_for_aid[aid]
        in_volume_in_order: list[VolumeIssueRow] = []
        first_pos: int | None = None
        last_pos: int | None = None
        for pos, member in enumerate(members):
            member_id = member.get("id")
            if member_id is None:
                continue
            row = issue_index.get(int(member_id))
            if row is None:
                continue
            in_volume_in_order.append(row)
            if first_pos is None:
                first_pos = pos
            last_pos = pos
        if not in_volume_in_order:
            continue
        prev_link: ArcSiblingLink | None = None
        if first_pos is not None and first_pos > 0:
            prev_member = members[first_pos - 1]
            prev_id = prev_member.get("id")
            if prev_id is not None:
                prev_link = ArcSiblingLink(
                    arc=arc_credit,
                    issue_cv_id=int(prev_id),
                    issue_name=prev_member.get("name"),
                )
        next_link: ArcSiblingLink | None = None
        if last_pos is not None and last_pos + 1 < len(members):
            next_member = members[last_pos + 1]
            next_id = next_member.get("id")
            if next_id is not None:
                next_link = ArcSiblingLink(
                    arc=arc_credit,
                    issue_cv_id=int(next_id),
                    issue_name=next_member.get("name"),
                )
        gallery_shelves.append(
            GalleryArcShelf(
                arc=arc_credit,
                issues=in_volume_in_order,
                prev_link=prev_link,
                next_link=next_link,
            )
        )

    # Unaffiliated: issues belonging to none of the volume's arcs.
    in_any_arc_ids = {
        row.cv_id for shelf in gallery_shelves for row in shelf.issues
    }
    gallery_unaffiliated = [
        row for row in issues if row.cv_id not in in_any_arc_ids
    ]

    # Sort the flat issues list into volume order. The Arcs view's
    # per-arc shelves keep their CV-reading order (set above); only the
    # ``issues`` list and the Gallery's volume-order segments use this
    # natural sort. Once sorted, stamp each row with its volume index
    # so templates can drive per-cover ``x-show`` (Gallery / Arcs) and
    # per-row ``x-show`` (List) against the same pagination window.
    issues.sort(key=lambda r: sort_key_issue_number(r.issue_number))
    for idx, row in enumerate(issues):
        row.volume_idx = idx

    # ---- Gallery segments (volume-order, gap-aware) -------------------
    # Walk the sorted issues and break into a new segment when EITHER:
    #   (a) the arc-membership "fingerprint" (set of arc IDs) changes, OR
    #   (b) for any arc shared with the previous issue, the arc's next
    #       member isn't this issue — i.e., the arc visits other volumes
    #       between the two.
    #
    # Rule (b) is the important one: an arc like Secret Invasion that
    # dips into this volume for #14, leaves for a tie-in, comes back
    # for #15, etc., should render as multiple shelves so the user
    # sees the gap visually (each shelf gets its own ← / → arrows
    # pointing at the elsewhere members between segments).
    arc_pos_by_aid: dict[int, dict[int, int]] = {}
    for aid, members in arc_members_by_aid.items():
        pos_map: dict[int, int] = {}
        for pos, m in enumerate(members):
            mid = m.get("id")
            if mid is not None:
                pos_map[int(mid)] = pos
        arc_pos_by_aid[aid] = pos_map

    # Volume indices are tracked alongside segmentation so each segment
    # knows its [first_volume_idx, last_volume_idx] span — used by the
    # template to drive ``x-show`` against the shared list pagination
    # window. (No no-arc cap: per-cover ``x-show`` lets a large no-arc
    # segment render naturally as a wrap-flowing block of just the
    # visible covers.)
    gallery_segments: list[GallerySegment] = []
    current_fp: frozenset[int] | None = None
    current_bucket: list[VolumeIssueRow] = []
    current_bucket_start: int = 0
    prev_issue: VolumeIssueRow | None = None
    for vol_idx, issue in enumerate(issues):
        fp = frozenset(ac.cv_id for ac in issue.arc_credits)
        split_here = False
        if current_bucket:
            if fp != current_fp:
                split_here = True
            else:
                # Same fingerprint — check arc-position adjacency for
                # any shared arc. A non-consecutive position means the
                # arc continued elsewhere between these two issues.
                for aid in fp:
                    pos_map = arc_pos_by_aid.get(aid) or {}
                    prev_pos = pos_map.get(prev_issue.cv_id) if prev_issue else None
                    curr_pos = pos_map.get(issue.cv_id)
                    if (
                        prev_pos is not None
                        and curr_pos is not None
                        and curr_pos != prev_pos + 1
                    ):
                        split_here = True
                        break
        if split_here:
            gallery_segments.append(
                GallerySegment(
                    issues=current_bucket,
                    first_volume_idx=current_bucket_start,
                    last_volume_idx=current_bucket_start + len(current_bucket) - 1,
                )
            )
            current_bucket = [issue]
            current_bucket_start = vol_idx
        else:
            if not current_bucket:
                current_bucket_start = vol_idx
            current_bucket.append(issue)
        current_fp = fp
        prev_issue = issue
    if current_bucket:
        gallery_segments.append(
            GallerySegment(
                issues=current_bucket,
                first_volume_idx=current_bucket_start,
                last_volume_idx=current_bucket_start + len(current_bucket) - 1,
            )
        )

    # Per-segment fill: arcs (ordered for nesting), and per-arc prev/next
    # links with the new suppression rule — suppress only when the prev/
    # next arc member is the actual last/first issue of the immediately
    # adjacent segment. This means arc shelves split by arc gaps DO get
    # arrows (because the prev/next member is the elsewhere issue, not
    # whatever's in the adjacent shelf), while arcs that visually
    # continue from one shelf to the next still suppress.
    for seg_idx, seg in enumerate(gallery_segments):
        seg_arc_ids = {ac.cv_id for ac in seg.issues[0].arc_credits}
        seg.arcs = [
            arc_credit_for_aid[aid]
            for aid in arc_display_order
            if aid in seg_arc_ids and aid in arc_credit_for_aid
        ]
        prev_seg_last_id: int | None = (
            gallery_segments[seg_idx - 1].issues[-1].cv_id
            if seg_idx > 0
            else None
        )
        next_seg_first_id: int | None = (
            gallery_segments[seg_idx + 1].issues[0].cv_id
            if seg_idx + 1 < len(gallery_segments)
            else None
        )
        first_id = seg.issues[0].cv_id
        last_id = seg.issues[-1].cv_id
        for arc in seg.arcs:
            aid = arc.cv_id
            members = arc_members_by_aid.get(aid) or []
            pos_map = arc_pos_by_aid.get(aid) or {}
            first_pos = pos_map.get(first_id)
            last_pos = pos_map.get(last_id)
            # ← arrow: a prev arc member exists AND it isn't the last
            # issue of the immediately preceding segment (i.e., the arc
            # isn't visually continuing from one shelf above).
            prev_link: ArcSiblingLink | None = None
            if first_pos is not None and first_pos > 0:
                prev_member = members[first_pos - 1]
                prev_id = prev_member.get("id")
                if prev_id is not None and int(prev_id) != prev_seg_last_id:
                    prev_link = ArcSiblingLink(
                        arc=arc,
                        issue_cv_id=int(prev_id),
                        issue_name=prev_member.get("name"),
                    )
            seg.prev_links.append(prev_link)
            # → arrow: symmetric.
            next_link: ArcSiblingLink | None = None
            if last_pos is not None and last_pos + 1 < len(members):
                next_member = members[last_pos + 1]
                next_id = next_member.get("id")
                if next_id is not None and int(next_id) != next_seg_first_id:
                    next_link = ArcSiblingLink(
                        arc=arc,
                        issue_cv_id=int(next_id),
                        issue_name=next_member.get("name"),
                    )
            seg.next_links.append(next_link)

    # ---- Back-fill sibling metadata for richer tooltips ---------------
    # The arc payload gave us the sibling's name. For sibling issues we
    # happen to have locally (because they belong to another volume in
    # our library, or because some prior arc fetch dropped a stub row),
    # we can also surface the issue number + volume name+year. One batch
    # query covers every sibling — per-row arrows, per-arc shelves,
    # *and* the new volume-order segments.
    sibling_ids: set[int] = set()
    for row in issues:
        for link in row.prev_arc_links:
            sibling_ids.add(link.issue_cv_id)
        for link in row.next_arc_links:
            sibling_ids.add(link.issue_cv_id)
    for shelf in gallery_shelves:
        if shelf.prev_link is not None:
            sibling_ids.add(shelf.prev_link.issue_cv_id)
        if shelf.next_link is not None:
            sibling_ids.add(shelf.next_link.issue_cv_id)
    for seg in gallery_segments:
        for link in (*seg.prev_links, *seg.next_links):
            if link is not None:
                sibling_ids.add(link.issue_cv_id)
    if sibling_ids:
        sib_stmt = (
            select(
                CvIssue.cv_id,
                CvIssue.issue_number,
                CvVolume.name,
                CvVolume.year,
            )
            .outerjoin(CvVolume, CvVolume.cv_id == CvIssue.volume_cv_id)
            .where(CvIssue.cv_id.in_(sibling_ids))
        )
        sib_meta: dict[int, tuple[str | None, str | None, int | None]] = {
            int(cv_id): (issue_number, volume_name, volume_year)
            for cv_id, issue_number, volume_name, volume_year in (
                await db.execute(sib_stmt)
            ).all()
        }

        def _enrich(link: ArcSiblingLink) -> None:
            meta = sib_meta.get(link.issue_cv_id)
            if meta is not None:
                link.issue_number, link.volume_name, link.volume_year = meta

        for row in issues:
            for link in (*row.prev_arc_links, *row.next_arc_links):
                _enrich(link)
        for shelf in gallery_shelves:
            if shelf.prev_link is not None:
                _enrich(shelf.prev_link)
            if shelf.next_link is not None:
                _enrich(shelf.next_link)
        for seg in gallery_segments:
            for link in (*seg.prev_links, *seg.next_links):
                if link is not None:
                    _enrich(link)

    # Assign each arc a stable color from the palette based on the
    # publication-order display sort. Two volumes that share an arc may
    # render different colors — that's fine; consistency only matters
    # within a single volume view.
    arc_color_classes = {
        aid: _ARC_BG_PALETTE[idx % len(_ARC_BG_PALETTE)]
        for idx, aid in enumerate(arc_display_order)
    }
    arc_text_color_classes = {
        aid: _ARC_TEXT_PALETTE[idx % len(_ARC_TEXT_PALETTE)]
        for idx, aid in enumerate(arc_display_order)
    }
    arc_tint_color_classes = {
        aid: _ARC_TINT_PALETTE[idx % len(_ARC_TINT_PALETTE)]
        for idx, aid in enumerate(arc_display_order)
    }
    arc_pill_bg_color_classes = {
        aid: _ARC_PILL_BG_PALETTE[idx % len(_ARC_PILL_BG_PALETTE)]
        for idx, aid in enumerate(arc_display_order)
    }
    arc_slots = [
        ArcCredit(
            cv_id=aid,
            name=arc_names[aid],
            primary_book=arc_primary_books.get(aid),
        )
        for aid in arc_display_order
    ]

    # Initial pagination window — the slice the volume page lands on.
    # Centered on first owned issue (with 2 above for context); if no
    # owned issue exists in the first ``window_size`` issues, start at
    # 0. Same logic used to be inlined in the route + template; living
    # here lets the arc rail (computed below) use the same window.
    # Window size is the admin-editable ``page_size`` setting (see
    # ``app.services.settings``). Fetched live so a setting change
    # propagates without restart. The volume page hydrates one CV
    # call per issue in the initial slice (no bulk endpoint returns
    # ``story_arc_credits``), so this is the tightest budget;
    # everything else aligns to it.
    initial_window_size = await get_page_size(db)

    # Window anchoring — pick a "target" issue the window should sit
    # on, then open the whole page that contains it:
    #
    #   * Caller-supplied ``from_issue_cv_id`` wins when given AND
    #     that issue is actually in this volume. This is the
    #     "arrived from an arc / link" case where the user has an
    #     explicit entry point we should land them on.
    #   * Otherwise default to the first owned issue, so a user
    #     opening a volume they partially collect lands near their
    #     existing run rather than at issue 1.
    #   * Otherwise start at 0.
    #
    # The start is page-aligned (floored to a multiple of the page
    # size) so the volume page's range tabs land on the whole
    # pageSize-issue page that holds the target, not a context-
    # shifted window straddling a page boundary.
    target_idx = -1
    if from_issue_cv_id is not None:
        target_idx = next(
            (i for i, row in enumerate(issues) if row.cv_id == from_issue_cv_id),
            -1,
        )
    if target_idx < 0:
        target_idx = next(
            (i for i, row in enumerate(issues) if row.owned), -1
        )
    initial_window_start = (
        (target_idx // initial_window_size) * initial_window_size
        if target_idx >= 0
        else 0
    )

    # Build the List view's arc rail for the requested window. When
    # callers don't pass an override, default to the initial window
    # (the slice the page lands on). The rail-fragment route uses the
    # overrides to re-render for arbitrary pagination states without
    # touching the rest of the volume detail. Lazy import to avoid the
    # circular dependency (rail.py imports types from this module).
    list_rail = None
    arcs_rail = None
    if arc_first_seen_order:
        from app.services.rail import build_list_rail

        in_volume_issue_ids: set[int] = {issue.cv_id for issue in issues}
        effective_start = (
            rail_window_start
            if rail_window_start is not None
            else initial_window_start
        )
        effective_size = (
            rail_window_size
            if rail_window_size is not None
            else initial_window_size
        )
        # Clamp to valid bounds — out-of-range start defaults to 0.
        effective_start = max(0, min(effective_start, len(issues)))
        effective_size = max(0, effective_size)
        # Half-open window bounds shared by the list, arcs, and
        # gallery rail builds so they all describe the same slice.
        window_lo = effective_start
        window_hi = effective_start + effective_size
        window_for_rail = issues[window_lo:window_hi]
        list_rail = build_list_rail(
            window_issues=window_for_rail,
            arc_members_by_aid=arc_members_by_aid,
            arc_credit_for_aid=arc_credit_for_aid,
            arc_display_order=arc_display_order,
            in_volume_issue_ids=in_volume_issue_ids,
        )
        # Compact variant for the Arcs view's lines-only rendering.
        # Row pitch is derived from the Arcs main pane's actual shelf
        # height in this window so the rail's vertical extent tracks
        # the shelves naturally:
        #
        #   • Many arcs (lots of shelves) → tall rail.
        #   • One-shot (single small shelf) → short rail, but
        #     clamped to a minimum so it doesn't collapse.
        #
        # Estimates use the per-arc shelf's typical desktop layout:
        # a ~26px header + p-2 padding + cover-rows worth of cover
        # height (~170px each at the common 5-7 col grid). Six
        # covers per row is the rough average across the responsive
        # grid breakpoints (md:5, lg:6, xl:7). All numbers are
        # ballpark — the rail doesn't have to align with the shelves,
        # it just has to share their order-of-magnitude vertical
        # extent.
        #
        # ``ARCS_HEIGHT_SCALE_*`` is the rail-to-shelves ratio: the
        # rail's job is showing temporal overlap, not mirroring shelf
        # heights, so it can afford to be visibly shorter than the
        # main pane on arc-heavy volumes without losing legibility.
        # 4/5 (= 0.8) tightens crowded volumes without crushing
        # simpler ones — the per-shelf base estimates do most of the
        # work; the scale is a final trim.
        ARCS_SHELF_HEADER_PX = 26
        ARCS_SHELF_PADDING_PX = 16
        ARCS_SHELF_GAP_PX = 16
        ARCS_COVER_ROW_PX = 170
        ARCS_COVERS_PER_GRID_ROW = 6
        ARCS_MIN_ROW_HEIGHT_PX = 24
        ARCS_HEIGHT_SCALE_NUM = 4
        ARCS_HEIGHT_SCALE_DEN = 5

        def _shelf_height_for(in_window_count: int) -> int:
            cover_rows = max(
                1,
                (in_window_count + ARCS_COVERS_PER_GRID_ROW - 1)
                // ARCS_COVERS_PER_GRID_ROW,
            )
            return (
                ARCS_SHELF_HEADER_PX
                + ARCS_SHELF_PADDING_PX
                + cover_rows * ARCS_COVER_ROW_PX
                + ARCS_SHELF_GAP_PX
            )

        arcs_content_px = 0
        for shelf in gallery_shelves:
            in_win = sum(
                1 for i in shelf.issues
                if window_lo <= i.volume_idx < window_hi
            )
            if in_win:
                arcs_content_px += _shelf_height_for(in_win)
        un_in_win = sum(
            1 for i in gallery_unaffiliated
            if window_lo <= i.volume_idx < window_hi
        )
        if un_in_win:
            arcs_content_px += _shelf_height_for(un_in_win)

        window_issue_count = max(1, len(window_for_rail))
        arcs_row_height = max(
            ARCS_MIN_ROW_HEIGHT_PX,
            (arcs_content_px * ARCS_HEIGHT_SCALE_NUM)
            // (window_issue_count * ARCS_HEIGHT_SCALE_DEN),
        )
        # Bend curves scale with the row pitch so they stay
        # recognizable but don't intrude into adjacent rows. The
        # 4-to-1 ratio mirrors the default (72 px row → ~16 px bend).
        arcs_bend_offset = max(4, arcs_row_height // 4)

        arcs_rail = build_list_rail(
            window_issues=window_for_rail,
            arc_members_by_aid=arc_members_by_aid,
            arc_credit_for_aid=arc_credit_for_aid,
            arc_display_order=arc_display_order,
            in_volume_issue_ids=in_volume_issue_ids,
            row_height=arcs_row_height,
            bend_offset=arcs_bend_offset,
        )

    # Gallery rail — shelf-indexed, branch-appearance rule applied.
    # Visible shelves are the gallery segments whose [first_volume_idx,
    # last_volume_idx] span overlaps the issue window. Same window
    # bounds as the list rail (since gallery/list share pagination).
    gallery_rail = None
    if arc_first_seen_order and gallery_segments:
        from app.services.rail import build_gallery_rail

        visible_segments_for_rail = [
            seg for seg in gallery_segments
            if seg.first_volume_idx < window_hi
            and seg.last_volume_idx >= window_lo
        ]
        if visible_segments_for_rail:
            gallery_rail = build_gallery_rail(
                visible_segments=visible_segments_for_rail,
                all_segments=gallery_segments,
                arc_members_by_aid=arc_members_by_aid,
                arc_credit_for_aid=arc_credit_for_aid,
                arc_display_order=arc_display_order,
                in_volume_issue_ids=in_volume_issue_ids,
                # Window bounds so the rail counts only the COVERS
                # actually rendered (DOM hides out-of-window covers
                # via x-show, shrinking partial-edge shelves).
                window_lo=window_lo,
                window_hi=window_hi,
            )

    # Phase 11F — non-issue files attached to this volume as supplements.
    # Local import: ``app.services.local`` reaches the review/reader
    # services, so importing it at module scope risks an import cycle.
    from app.services.local import list_volume_supplements

    supplements = await list_volume_supplements(db, cv_id)

    return VolumeDetail(
        volume=volume,
        publisher_name=publisher_name,
        issues=issues,
        supplements=supplements,
        owned_issue_count=owned_issue_count,
        finished_issue_count=finished_issue_count,
        initial_window_start=initial_window_start,
        initial_window_size=initial_window_size,
        list_rail=list_rail,
        gallery_rail=gallery_rail,
        arcs_rail=arcs_rail,
        story_arc_names=list(arc_names.values()),
        cover_url=cv_image_url(volume.raw_payload, "medium"),
        banner_url=cv_image_url(volume.raw_payload, "banner"),
        description=(volume.raw_payload or {}).get("description"),
        deck=(volume.raw_payload or {}).get("deck"),
        arc_color_classes=arc_color_classes,
        arc_text_color_classes=arc_text_color_classes,
        arc_tint_color_classes=arc_tint_color_classes,
        arc_pill_bg_color_classes=arc_pill_bg_color_classes,
        arc_slots=arc_slots,
        gallery_shelves=gallery_shelves,
        gallery_unaffiliated=gallery_unaffiliated,
        gallery_segments=gallery_segments,
        credit_filter=credit,
    )


# ---- Issue detail -------------------------------------------------------


async def get_issue_detail(
    db: AsyncSession, cv_cache: ComicVineCache, cv_id: int
) -> IssueDetail | None:
    """Issue page payload. Hydrates the issue via ``cv_cache.get_issue``
    if it's a stub or missing — §8's "second hop" pattern.

    Error policy:
      * CV unavailable + cached row present → degrade to the cached
        row (stub or stale; the page renders, banner can warn).
      * CV unavailable + nothing cached → re-raise the CV error so
        the route can render an HTML "couldn't load" page instead
        of 404-ing with a misleading "not found" message.
      * CV says explicitly not found (101) + nothing cached → returns
        None (route 404s).
    """
    try:
        issue = await cv_cache.get_issue(db, cv_id)
    except (
        ComicVineNotFoundError,
        ComicVineKeyMissingError,
        ComicVineKeyInvalidError,
    ):
        # Config / "doesn't exist on CV" errors: fall back to
        # whatever we have locally. Route 404s when nothing's
        # cached — from the user's POV the issue isn't visible
        # regardless of which sub-reason CV refused.
        issue = await db.get(CvIssue, cv_id)
    except ComicVineError:
        # Transient errors (rate limit, generic API failure). Fall
        # back to whatever we already had cached so the page can
        # render in degraded mode if there's a stub. If there's
        # nothing, surface the CV error to the route — a 404 with
        # "issue X not found" lies about the cause (the issue may
        # well exist; CV just isn't answering right now).
        issue = await db.get(CvIssue, cv_id)
        if issue is None:
            raise
    if issue is None:
        return None

    payload = issue.raw_payload or {}

    volume: CvVolume | None = None
    publisher_name: str | None = None
    if issue.volume_cv_id is not None:
        volume = await db.get(CvVolume, issue.volume_cv_id)
        if volume is not None and volume.publisher_cv_id is not None:
            pub = await db.get(CvPublisher, volume.publisher_cv_id)
            if pub is not None:
                publisher_name = pub.name

    persons = [
        CreditRow(cv_id=int(p["id"]), name=p.get("name") or "", role=p.get("role"))
        for p in (payload.get("person_credits") or [])
        if p.get("id") is not None
    ]
    characters = [
        CreditRow(cv_id=int(c["id"]), name=c.get("name") or "")
        for c in (payload.get("character_credits") or [])
        if c.get("id") is not None
    ]
    story_arcs: list[CreditRow] = []
    for a in payload.get("story_arc_credits") or []:
        if a.get("id") is None:
            continue
        primary_book, clean_name = parse_arc_name(a.get("name") or "")
        story_arcs.append(CreditRow(
            cv_id=int(a["id"]),
            name=clean_name,
            primary_book=primary_book,
        ))
    teams = [
        CreditRow(cv_id=int(t["id"]), name=t.get("name") or "")
        for t in (payload.get("team_credits") or [])
        if t.get("id") is not None
    ]

    # Per-arc pill bg classes — keyed by arc cv_id, indexed by the
    # arc's position in ``story_arcs`` so the SAME arc gets the SAME
    # hue on both the pill (this dict) and any rail branch derived
    # from it below (which uses the parallel 400-tier hex palette
    # via ``arc_hex_for_index``).
    arc_pill_bg_color_classes = {
        arc.cv_id: _ARC_PILL_BG_PALETTE[i % len(_ARC_PILL_BG_PALETTE)]
        for i, arc in enumerate(story_arcs)
    }

    # Matched files: every current (non-missing) location of any file
    # matched to this issue, paired with its file_id and match status.
    # Most issues have 0 or 1 file; duplicates yield more. The file_id
    # is carried so the issue page can offer a per-file "Fix match"
    # link into the review search flow — the only correction path for
    # an AUTO match, which never enters the review queue.
    matched_stmt = (
        select(FileMatch.file_id, FileMatch.status, FileLocation.path)
        .join(FileLocation, FileLocation.file_id == FileMatch.file_id)
        .where(
            FileMatch.issue_cv_id == cv_id,
            FileMatch.status.in_(
                (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
            ),
            FileLocation.missing_since.is_(None),
        )
        .order_by(FileLocation.path)
    )
    matched_files = [
        MatchedFile(file_id=row.file_id, path=row.path, status=row.status)
        for row in (await db.execute(matched_stmt)).all()
    ]

    # Owned iff at least one auto/confirmed match points here. We
    # could infer it from ``len(matched_files) > 0`` but ``matched_files``
    # is filtered to non-missing locations only, so a file that's
    # gone missing on disk would wrongly read as "not owned". A
    # dedicated query against the match table is the source of truth.
    owned_stmt = select(
        func.coalesce(
            func.bool_or(
                FileMatch.status.in_(
                    (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                )
            ),
            False,
        )
    ).where(FileMatch.issue_cv_id == cv_id)
    owned = bool((await db.execute(owned_stmt)).scalar())

    # Neighbors: previous/next issue within the same volume, by sorted
    # issue_number. SQL doesn't have a clean natural-sort for our mixed
    # values, so we load the volume's issue list and find this issue's
    # position in Python.
    prev_neighbor: IssueNeighbor | None = None
    next_neighbor: IssueNeighbor | None = None
    has_earlier_issues = False
    has_later_issues = False
    if issue.volume_cv_id is not None:
        # Pull each sibling alongside an "owned" boolean
        # (``bool_or(FileMatch.status IN auto/confirmed)``) so the
        # rail can dim the prev/next thumbnails for issues we don't
        # have files for. Mirrors the per-row ``owned`` computation
        # on the volume page's issue table.
        neighbors_stmt = (
            select(
                CvIssue,
                func.coalesce(
                    func.bool_or(
                        FileMatch.status.in_(
                            (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                        )
                    ),
                    False,
                ).label("owned"),
            )
            .outerjoin(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
            .where(CvIssue.volume_cv_id == issue.volume_cv_id)
            .group_by(CvIssue.cv_id)
        )
        sibling_rows = list((await db.execute(neighbors_stmt)).all())
        sibling_rows.sort(key=lambda r: sort_key_issue_number(r[0].issue_number))
        for i, (sib, _owned) in enumerate(sibling_rows):
            if sib.cv_id == cv_id:
                if i > 0:
                    p, p_owned = sibling_rows[i - 1]
                    prev_neighbor = IssueNeighbor(
                        cv_id=p.cv_id,
                        issue_number=p.issue_number,
                        name=p.name,
                        cover_url=cv_image_url(p.raw_payload, "thumb"),
                        owned=bool(p_owned),
                    )
                if i + 1 < len(sibling_rows):
                    n, n_owned = sibling_rows[i + 1]
                    next_neighbor = IssueNeighbor(
                        cv_id=n.cv_id,
                        issue_number=n.issue_number,
                        name=n.name,
                        cover_url=cv_image_url(n.raw_payload, "thumb"),
                        owned=bool(n_owned),
                    )
                # "Earlier exists" iff there's a sibling before the
                # prev neighbor (i.e., at least 2 issues before this
                # one). Same shape for later.
                has_earlier_issues = i > 1
                has_later_issues = i < len(sibling_rows) - 2
                break

    # Issue rail: cross-volume arc branches. For each story arc the
    # current issue is part of, fetch the arc's full member list and
    # check whether the immediate prev/next member lives outside this
    # volume. Those become left/right horizontal branches on the rail.
    # Members in the SAME volume are skipped — the user can navigate
    # to them via the prev/next thumbnails or the volume page (which
    # show same-volume neighbors directly).
    #
    # Members not in our cache at all are still surfaced as branches
    # (with limited metadata) — we have no way to confirm they live
    # outside this volume from the cache, but the only realistic
    # reason an arc-member issue isn't cached is that its parent
    # volume isn't in our library, which is exactly the "elsewhere"
    # signal the user wants to see.
    arc_branches: list[IssueArcBranch] = []
    if issue.volume_cv_id is not None and story_arcs:
        # Lazy import to avoid the rail ↔ library circular: rail.py
        # imports types from this module.
        from app.services.rail import arc_hex_for_index

        for arc_idx, arc_credit in enumerate(story_arcs):
            color = arc_hex_for_index(arc_idx)
            try:
                arc_payload = await cv_cache.get_story_arc(
                    db, arc_credit.cv_id
                )
            except ComicVineError:
                # Skip arcs we can't fetch — the rail can render the
                # rest without crashing.
                continue
            if arc_payload is None:
                # Cache miss with no exception (e.g., the arc isn't in
                # the DB yet and the stub cache returns ``None`` rather
                # than raising). Nothing to do for this arc; skip.
                continue
            members = (arc_payload.raw_payload or {}).get("issues") or []
            pos = next(
                (
                    i for i, m in enumerate(members)
                    if m.get("id") is not None and int(m["id"]) == cv_id
                ),
                None,
            )
            if pos is None:
                continue

            # Bind ``arc_credit`` and ``color`` as default args so the
            # closure captures the CURRENT loop iteration's values
            # instead of the late-bound names (which would all resolve
            # to the last iteration's values once the loop finished).
            # The standard B023 fix for closures-in-a-loop.
            async def _maybe_branch(
                member: dict,
                direction: str,
                _arc=arc_credit,
                _color=color,
            ):
                mid_raw = member.get("id")
                if mid_raw is None:
                    return None
                target_id = int(mid_raw)
                target_issue = await db.get(CvIssue, target_id)
                # Skip in-volume members — they're not "elsewhere".
                if (
                    target_issue is not None
                    and target_issue.volume_cv_id == issue.volume_cv_id
                ):
                    return None
                branch = IssueArcBranch(
                    arc=_arc,
                    color=_color,
                    direction=direction,
                    target_cv_id=target_id,
                    target_issue_name=member.get("name"),
                )
                if target_issue is not None:
                    branch.target_issue_number = target_issue.issue_number
                    if target_issue.volume_cv_id is not None:
                        target_vol = await db.get(
                            CvVolume, target_issue.volume_cv_id
                        )
                        if target_vol is not None:
                            branch.target_volume_name = target_vol.name
                            branch.target_volume_year = target_vol.year
                return branch

            if pos > 0:
                br = await _maybe_branch(members[pos - 1], "prev")
                if br is not None:
                    arc_branches.append(br)
            if pos < len(members) - 1:
                br = await _maybe_branch(members[pos + 1], "next")
                if br is not None:
                    arc_branches.append(br)

    return IssueDetail(
        issue=issue,
        volume=volume,
        publisher_name=publisher_name,
        cover_url=cv_image_url(payload, "large"),
        banner_url=cv_image_url(payload, "banner"),
        description=payload.get("description"),
        deck=payload.get("deck"),
        persons=persons,
        characters=characters,
        story_arcs=story_arcs,
        teams=teams,
        matched_files=matched_files,
        prev_neighbor=prev_neighbor,
        next_neighbor=next_neighbor,
        arc_branches=arc_branches,
        has_earlier_issues=has_earlier_issues,
        has_later_issues=has_later_issues,
        arc_pill_bg_color_classes=arc_pill_bg_color_classes,
        owned=owned,
    )


# ---- Arc detail --------------------------------------------------------


@dataclass
class ArcIssueRow:
    """One issue's row on the /arc/<id> page.

    Mirrors ``VolumeIssueRow`` but adds the parent-volume context an
    arc issue page needs (volume name + year + cv_id) since the arc
    spans multiple volumes. ``cover_url``/``cover_date``/``owned`` may
    be ``None``/``False`` for arc members we haven't cached locally —
    we fall back to whatever the arc payload itself provides without
    triggering N CV fetches.
    """

    cv_id: int
    issue_number: str | None
    name: str | None
    cover_url: str | None
    cover_date: Any  # date or None
    owned: bool
    volume_cv_id: int | None
    volume_name: str | None
    volume_year: int | None
    # Position in arc reading order — same index used by the arc
    # page's pagination ``start`` / ``count`` to decide whether this
    # row is in the current window. Defaulted to 0 so a freshly-
    # constructed row sorts to the top; the service sets the real
    # value on every row.
    arc_idx: int = 0
    # True once the underlying ``cv_issues`` row has been fetched
    # (either by the bulk ``volume_issues`` job, which fills cover /
    # name / cover_date, OR by an individual ``/issue/<id>/`` call).
    # Drives the arc page's per-row auto-refresh — bulk-hydrated is
    # enough for the arc view since it doesn't show arc credits, but
    # the route still enqueues per-issue revalidates to upgrade
    # ``_bulk_hydrated`` rows to full payloads in the background.
    is_hydrated: bool = False
    # Parent volume's classified format — ongoing / limited /
    # one_shot / collection — for the badge in the List view's
    # VOLUME column. ``None`` when the volume isn't cached.
    volume_format: str | None = None
    # ComicVine site-page URL for this issue. Every ``issue_credits``
    # entry carries one; the character / creator pages use it to drive
    # the best-effort site scrape for volume-less appearances. Unset on
    # the arc page (it has no use for it).
    site_detail_url: str | None = None
    # True once the site scraper has attempted this issue
    # (``cv_issues.site_scraped_at`` set). Lets the character / creator
    # routes skip re-enqueueing a scrape they've already run.
    site_scraped: bool = False


@dataclass
class ArcVolumeShelf:
    """One shelf-per-volume the arc touches, used by the gallery view.

    Issues within a shelf preserve the arc's reading order (i.e., the
    order they appear in the arc's CV payload, restricted to this
    volume's members). Multiple shelves are emitted in the order each
    volume is first encountered in arc-reading order, so the gallery
    reads top-to-bottom as the arc unfolds.
    """

    volume_cv_id: int | None
    volume_name: str
    volume_year: int | None
    # Classified volume format — ongoing / limited / one_shot /
    # collection — for the shelf header's badge. ``None`` when the
    # volume isn't in our cv_volumes cache (nothing to classify).
    format: str | None = None
    issues: list[ArcIssueRow] = field(default_factory=list)
    # Min / max arc-reading-order index across this shelf's issues.
    # Drives the gallery view's per-shelf ``x-show`` — a shelf
    # renders whenever its index range overlaps the user's current
    # pagination window (``start`` .. ``start + count``). Once the
    # shelf is visible, ALL its covers render, even ones outside the
    # window — keeps the volume's contribution to the arc legible
    # as a single unit instead of fragmented across page boundaries.
    min_arc_idx: int = 0
    max_arc_idx: int = 0


@dataclass
class ArcDetail:
    arc: Any  # CvStoryArc — kept loose to avoid circular type pressure
    name: str                       # CV name with the quoted-book prefix stripped
    primary_book: str | None        # parsed-out prefix, if any
    description: str | None
    # Short one-line summary from CV's ``deck`` field. See VolumeDetail.deck.
    deck: str | None
    cover_url: str | None
    banner_url: str | None
    # CV publishes its arc payload with a single ``publisher`` field,
    # so the arc page can show one publisher chip directly — no need
    # to count across the arc's volumes. ``None`` for arcs whose
    # payload lacks the field (rare; some indie or legacy entries).
    publisher_cv_id: int | None = None
    issues: list[ArcIssueRow] = field(default_factory=list)
    total_count: int = 0
    owned_count: int = 0
    volume_shelves: list[ArcVolumeShelf] = field(default_factory=list)


async def get_arc_detail(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    cv_id: int,
) -> ArcDetail | None:
    """Arc page payload. Returns ``None`` if the arc can't be hydrated.

    Strategy: fetch the arc (cache-aside via SWR), pull the member-issue
    list out of its ``raw_payload.issues``, then run a single batch
    query against ``cv_issues`` + ``cv_volumes`` + ``file_matches`` to
    enrich each member with cover, owned status, and parent-volume
    name/year. Members we don't have cached are kept on the list with
    whatever the arc payload itself provided — no per-member CV fetch.
    """
    try:
        arc = await cv_cache.get_story_arc(db, cv_id)
    except ComicVineError:
        # Fall back to whatever's already cached so the page can
        # render in degraded mode. If we have nothing at all,
        # re-raise so the route can show a "rate-limited / CV
        # unreachable" page instead of a misleading 404.
        arc = await db.get(CvStoryArc, cv_id)
        if arc is None:
            raise
    if arc is None:
        return None

    raw_payload = arc.raw_payload or {}
    primary_book, clean_name = parse_arc_name(arc.name or "")
    description = raw_payload.get("description")
    deck = raw_payload.get("deck")
    cover_url = cv_image_url(raw_payload, "large")
    banner_url = cv_image_url(raw_payload, "banner")
    publisher_payload = raw_payload.get("publisher") or {}
    publisher_cv_id = (
        int(publisher_payload["id"])
        if publisher_payload.get("id") is not None
        else None
    )

    # Members are kept in CV's order — that's the arc's reading order.
    members: list[dict] = raw_payload.get("issues") or []
    member_ids: list[int] = []
    for m in members:
        mid = m.get("id")
        if mid is not None:
            member_ids.append(int(mid))

    enriched_by_id: dict[int, ArcIssueRow] = {}
    if member_ids:
        # Batch enrich every member that exists in our cv_issues table
        # — single query joining the parent volume and aggregating
        # owned status from file_matches. ``bool_or`` over a left-join
        # cleanly yields False when no match exists.
        owned_expr = func.coalesce(
            func.bool_or(
                FileMatch.status.in_(
                    (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                )
            ),
            False,
        ).label("owned")
        stmt = (
            select(
                CvIssue.cv_id,
                CvIssue.issue_number,
                CvIssue.name,
                CvIssue.cover_date,
                CvIssue.raw_payload,
                CvIssue.volume_cv_id,
                CvIssue.fetched_at,
                CvVolume.name.label("volume_name"),
                CvVolume.year.label("volume_year"),
                owned_expr,
            )
            .outerjoin(CvVolume, CvVolume.cv_id == CvIssue.volume_cv_id)
            .outerjoin(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
            .where(CvIssue.cv_id.in_(member_ids))
            .group_by(
                CvIssue.cv_id,
                CvVolume.cv_id,
                CvVolume.name,
                CvVolume.year,
            )
        )
        for row in (await db.execute(stmt)).all():
            enriched_by_id[row.cv_id] = ArcIssueRow(
                cv_id=row.cv_id,
                issue_number=row.issue_number,
                name=row.name,
                cover_url=cv_image_url(row.raw_payload, "thumb"),
                cover_date=row.cover_date,
                owned=bool(row.owned),
                volume_cv_id=row.volume_cv_id,
                volume_name=row.volume_name,
                volume_year=row.volume_year,
                is_hydrated=row.fetched_at is not None,
            )

    # Walk members in arc-reading order, falling back to the arc
    # payload's nested data for any member we don't have cached.
    # The position in this loop is the issue's ``arc_idx`` — used
    # by the arc page's pagination ``x-show`` checks to decide
    # whether the row is in the current window.
    issues: list[ArcIssueRow] = []
    for m in members:
        mid_raw = m.get("id")
        if mid_raw is None:
            continue
        mid = int(mid_raw)
        arc_idx = len(issues)  # 0-based reading-order position
        if mid in enriched_by_id:
            row = enriched_by_id[mid]
            row.arc_idx = arc_idx
            issues.append(row)
        else:
            # Fall back to the arc payload's nested volume summary.
            nested_volume = m.get("volume") or {}
            issues.append(ArcIssueRow(
                cv_id=mid,
                issue_number=m.get("issue_number"),
                name=m.get("name"),
                cover_url=None,
                cover_date=None,
                owned=False,
                volume_cv_id=(
                    int(nested_volume["id"]) if nested_volume.get("id") else None
                ),
                volume_name=nested_volume.get("name"),
                volume_year=safe_int(nested_volume.get("start_year")),
                arc_idx=arc_idx,
            ))

    # Build per-volume shelves for the gallery view. Keyed by
    # ``volume_cv_id`` so members from the same volume cluster
    # together regardless of where they sit in arc reading order
    # (CV occasionally interleaves the same volume across an arc).
    # Insertion order = each volume's first appearance in the arc.
    # ``min_arc_idx`` / ``max_arc_idx`` track the shelf's reading-
    # order range; the gallery view uses them to decide which
    # shelves are visible for a given pagination window. When a
    # shelf has any overlap with the window, ALL its covers render
    # — keeps the volume's contribution to the arc legible as one
    # unit rather than fragmented across page boundaries.
    shelves: dict[int | None, ArcVolumeShelf] = {}
    for issue in issues:
        key = issue.volume_cv_id
        shelf = shelves.get(key)
        if shelf is None:
            shelf = ArcVolumeShelf(
                volume_cv_id=key,
                volume_name=issue.volume_name or "Unknown volume",
                volume_year=issue.volume_year,
                min_arc_idx=issue.arc_idx,
                max_arc_idx=issue.arc_idx,
            )
            shelves[key] = shelf
        else:
            shelf.max_arc_idx = max(shelf.max_arc_idx, issue.arc_idx)
            shelf.min_arc_idx = min(shelf.min_arc_idx, issue.arc_idx)
        shelf.issues.append(issue)
    volume_shelves = list(shelves.values())

    # Classify each shelf's volume (ongoing / limited / one_shot /
    # collection) from the cached cv_volumes row. One batch query;
    # volumes the arc touches that we haven't ingested get no badge.
    shelf_volume_ids = {
        s.volume_cv_id for s in volume_shelves if s.volume_cv_id is not None
    }
    if shelf_volume_ids:
        fmt_by_id = {
            v.cv_id: classify_cv_volume(v)
            for v in (
                await db.execute(
                    select(CvVolume).where(CvVolume.cv_id.in_(shelf_volume_ids))
                )
            ).scalars()
        }
        for shelf in volume_shelves:
            shelf.format = fmt_by_id.get(shelf.volume_cv_id)
        # Same classification onto each issue row, for the List
        # view's VOLUME column badge.
        for issue in issues:
            issue.volume_format = fmt_by_id.get(issue.volume_cv_id)

    owned_count = sum(1 for i in issues if i.owned)

    return ArcDetail(
        arc=arc,
        name=clean_name,
        primary_book=primary_book,
        description=description,
        deck=deck,
        cover_url=cover_url,
        banner_url=banner_url,
        publisher_cv_id=publisher_cv_id,
        issues=issues,
        total_count=len(issues),
        owned_count=owned_count,
        volume_shelves=volume_shelves,
    )


# ---- Publisher detail --------------------------------------------------


@dataclass
class PublisherArcRow:
    """One story arc's row on the /publisher/{id} page.

    The publisher's CV payload includes a top-level ``story_arcs``
    list — usually stubs (id, name, api_detail_url) but with enough
    to render a link. When we happen to have hydrated the arc locally
    (because the user opened its page, or it was pulled in via volume
    enrichment), we surface the cover too. ``is_hydrated`` drives the
    background-revalidate enqueue in the route — false rows get a
    ``revalidate("story_arc", id)`` job so the worker fills them in.
    """

    cv_id: int
    name: str                  # cleaned (quoted-book prefix stripped)
    primary_book: str | None
    cover_url: str | None      # None until the arc is hydrated locally
    is_hydrated: bool = False  # True if we have a CvStoryArc row in DB


@dataclass
class PublisherDetail:
    """Lightweight payload for the /publisher/{id} page.

    The publisher page intentionally doesn't enumerate volumes — at
    Marvel / DC scale that would be thousands of cards. Instead we
    show identity (name, icon, description) and a CTA into the
    library prefiltered by this publisher. ``library_volume_count``
    drives the CTA's label text.

    Story arcs are different: CV bundles a manageable list of arcs
    directly in the publisher payload (usually under 100), and they
    map cleanly to the existing /arc/<id> page — so a list+gallery
    of those is a useful navigation aid without scale issues.
    """

    publisher: Any  # CvPublisher
    name: str
    description: str | None
    # Short one-line summary from CV's ``deck`` field. See VolumeDetail.deck.
    deck: str | None
    icon_url: str | None
    site_detail_url: str | None
    library_volume_count: int = 0
    # Sliced page of arcs (size = caller's ``arcs_limit``). The full
    # list isn't returned — for Marvel/DC scale that'd be hundreds of
    # rows the page never renders.
    arcs: list[PublisherArcRow] = field(default_factory=list)
    # Total arcs the publisher carries (pre-pagination). Drives the
    # "Showing N of M" / "Load more" UI.
    arcs_total: int = 0


async def get_publisher_detail(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    cv_id: int,
    *,
    arcs_limit: int = DEFAULT_PAGE_SIZE,
    arcs_offset: int = 0,
    arcs_query: str | None = None,
) -> PublisherDetail | None:
    """Publisher page payload. Returns ``None`` if we can't hydrate.

    The cache layer handles stub-detection and SWR refresh, so a
    publisher that only entered our DB as a thin FK target (from a
    volume upsert) gets upgraded to the full payload here on first
    page view.

    ``arcs_limit`` / ``arcs_offset`` page through the publisher's
    story-arc list (CV bundles up to ~100 stubs per publisher). The
    full list is parsed + sorted server-side so pagination is
    consistent across visits; only the visible window is enriched
    against ``cv_story_arcs`` and returned.

    ``arcs_query`` is an optional case-insensitive substring filter
    applied to each arc's parsed name + ``primary_book``. Filtering
    happens AFTER the parse-and-sort pass but BEFORE pagination, so
    ``arcs_total`` reflects the post-filter count and the infinite-
    scroll fragment endpoint can keep paging through the filtered
    set without re-fetching the publisher payload from CV.
    """
    try:
        publisher = await cv_cache.get_publisher(db, cv_id)
    except ComicVineError:
        # Fall back to whatever's already cached — the page still
        # renders, the user can refresh if the CV outage clears.
        # When we don't have anything cached either, re-raise so
        # the route can render a CV-error page instead of 404'ing.
        publisher = await db.get(CvPublisher, cv_id)
        if publisher is None:
            raise
    if publisher is None:
        return None

    # "Volumes in your library" count for the CTA. ``DISTINCT`` keeps
    # multi-issue volumes from being double-counted.
    count_stmt = (
        select(func.count(func.distinct(CvVolume.cv_id)))
        .join(CvIssue, CvIssue.volume_cv_id == CvVolume.cv_id)
        .join(FileMatch, FileMatch.issue_cv_id == CvIssue.cv_id)
        .where(
            CvVolume.publisher_cv_id == cv_id,
            FileMatch.status.in_(
                (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
            ),
        )
    )
    library_volume_count = int((await db.execute(count_stmt)).scalar() or 0)

    raw = publisher.raw_payload or {}

    # Story arcs from the publisher payload. CV bundles each entry
    # as a stub (id + name + api_detail_url), so we have enough to
    # SORT the full list deterministically before paginating but not
    # enough to render covers without a per-arc fetch. Strategy:
    #   1. Parse every stub's name (so quoted-book prefix is split).
    #   2. Sort the parsed list — cleaned name primary, book tiebreak.
    #   3. Slice to the requested window.
    #   4. Batch-load CvStoryArc rows for the window only — that's
    #      where covers come from. The route enqueues a background
    #      revalidate for any window arc we don't have hydrated, so
    #      successive visits gradually fill in covers without ever
    #      blocking page render on synchronous CV calls.
    arc_stubs = raw.get("story_arcs") or []
    parsed_stubs: list[dict] = []
    for stub in arc_stubs:
        sid = stub.get("id")
        if sid is None:
            continue
        raw_name = stub.get("name") or ""
        primary_book, clean_name = parse_arc_name(raw_name)
        parsed_stubs.append({
            "cv_id": int(sid),
            "primary_book": primary_book,
            "clean_name": clean_name,
        })
    parsed_stubs.sort(
        key=lambda s: (
            s["clean_name"].lower(),
            (s["primary_book"] or "").lower(),
        )
    )

    # Optional name filter — case-insensitive substring match across
    # both the cleaned arc name and the parsed parent book. Applied
    # after the sort but before pagination so ``arcs_total`` reflects
    # the filtered count (drives the "Showing N of M" label + sentinel
    # short-circuit).
    if arcs_query:
        needle = arcs_query.strip().lower()
        if needle:
            parsed_stubs = [
                s for s in parsed_stubs
                if needle in s["clean_name"].lower()
                or needle in (s["primary_book"] or "").lower()
            ]

    arcs_total = len(parsed_stubs)
    window = parsed_stubs[arcs_offset : arcs_offset + arcs_limit]

    window_ids = [s["cv_id"] for s in window]
    hydrated_arcs: dict[int, CvStoryArc] = {}
    if window_ids:
        stmt = select(CvStoryArc).where(CvStoryArc.cv_id.in_(window_ids))
        for row in (await db.execute(stmt)).scalars():
            hydrated_arcs[row.cv_id] = row

    arcs: list[PublisherArcRow] = []
    for s in window:
        aid = s["cv_id"]
        name = s["clean_name"]
        primary_book = s["primary_book"]
        cover_url: str | None = None
        is_hydrated = aid in hydrated_arcs
        if is_hydrated:
            # Prefer the hydrated row's canonical name (in case CV
            # later cleaned up a typo) and the cover image.
            arc_row = hydrated_arcs[aid]
            if arc_row.name:
                primary_book, name = parse_arc_name(arc_row.name)
            cover_url = cv_image_url(arc_row.raw_payload, "medium")
        arcs.append(PublisherArcRow(
            cv_id=aid,
            name=name,
            primary_book=primary_book,
            cover_url=cover_url,
            is_hydrated=is_hydrated,
        ))

    return PublisherDetail(
        publisher=publisher,
        name=publisher.name,
        description=raw.get("description"),
        deck=raw.get("deck"),
        icon_url=cv_image_url(raw, "icon"),
        site_detail_url=raw.get("site_detail_url"),
        library_volume_count=library_volume_count,
        arcs=arcs,
        arcs_total=arcs_total,
    )


async def get_hydrated_arc_rows(
    db: AsyncSession,
    arc_ids: list[int],
) -> list[PublisherArcRow]:
    """Resolve the subset of ``arc_ids`` that are currently hydrated
    locally (have a ``cv_story_arcs`` row) and return one
    ``PublisherArcRow`` each. Unhydrated IDs are silently dropped —
    callers infer "still pending" by absence.

    Used by the publisher page's hydration-polling endpoint to
    refresh arc cards once background revalidate jobs drain. The
    rendering side reuses the same macros as the initial page
    render, so a "newly hydrated" row looks identical to one that
    arrived hydrated on first load.
    """
    if not arc_ids:
        return []
    stmt = select(CvStoryArc).where(CvStoryArc.cv_id.in_(arc_ids))
    rows = list((await db.execute(stmt)).scalars())
    out: list[PublisherArcRow] = []
    for arc_row in rows:
        primary_book, clean_name = parse_arc_name(arc_row.name or "")
        out.append(PublisherArcRow(
            cv_id=arc_row.cv_id,
            name=clean_name,
            primary_book=primary_book,
            cover_url=cv_image_url(arc_row.raw_payload, "medium"),
            is_hydrated=True,
        ))
    return out


# ---- Home: Recently added ----------------------------------------------


async def list_recently_added(
    db: AsyncSession, limit: int = 12
) -> list[RecentlyAddedVolume]:
    """Volumes with recently-matched files — CV volumes (auto/confirmed
    matches) and local volumes (Phase 11) — most recent first.

    Grouped by volume, not one entry per file: importing a run of issues
    for a single series would otherwise flood the list. A volume's
    position is its most recent ``matched_at``; ``issue_count`` is the
    distinct matched issues the user owns in it."""
    out: list[RecentlyAddedVolume] = []

    # ---- CV volumes --------------------------------------------------
    cv_groups = (
        await db.execute(
            select(
                CvIssue.volume_cv_id.label("vid"),
                func.max(FileMatch.matched_at).label("recent"),
                func.count(func.distinct(FileMatch.issue_cv_id)).label("n"),
            )
            .join(CvIssue, CvIssue.cv_id == FileMatch.issue_cv_id)
            .where(
                FileMatch.status.in_(
                    (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                )
            )
            .where(CvIssue.volume_cv_id.is_not(None))
            .group_by(CvIssue.volume_cv_id)
            .order_by(func.max(FileMatch.matched_at).desc())
            .limit(limit)
        )
    ).all()
    if cv_groups:
        vols = {
            v.cv_id: v
            for v in (
                await db.execute(
                    select(CvVolume).where(
                        CvVolume.cv_id.in_([g.vid for g in cv_groups])
                    )
                )
            ).scalars()
        }
        for vid, recent, n in cv_groups:
            vol = vols.get(vid)
            if vol is None:
                continue
            out.append(
                RecentlyAddedVolume(
                    kind="cv",
                    cv_id=vid,
                    local_id=None,
                    name=vol.name,
                    year=vol.year,
                    cover_url=cv_image_url(vol.raw_payload, "medium"),
                    issue_count=n,
                    matched_at=recent,
                )
            )

    # ---- Local volumes (Phase 11) ------------------------------------
    local_groups = (
        await db.execute(
            select(
                LocalIssue.local_volume_id.label("lvid"),
                func.max(FileMatch.matched_at).label("recent"),
                func.count(func.distinct(FileMatch.local_issue_id)).label(
                    "n"
                ),
            )
            .join(LocalIssue, LocalIssue.id == FileMatch.local_issue_id)
            .where(FileMatch.status == MatchStatus.LOCAL.value)
            .group_by(LocalIssue.local_volume_id)
            .order_by(func.max(FileMatch.matched_at).desc())
            .limit(limit)
        )
    ).all()
    if local_groups:
        lvids = [g.lvid for g in local_groups]
        lvols = {
            v.id: v
            for v in (
                await db.execute(
                    select(LocalVolume).where(LocalVolume.id.in_(lvids))
                )
            ).scalars()
        }
        # A local volume has no CV image — its cover is a matched file's
        # own first page (the lowest-numbered issue's).
        covers = await _local_volume_cover_files(db, lvids)
        for lvid, recent, n in local_groups:
            lv = lvols.get(lvid)
            if lv is None:
                continue
            file_id = covers.get(lvid)
            out.append(
                RecentlyAddedVolume(
                    kind="local",
                    cv_id=None,
                    local_id=lvid,
                    name=lv.name,
                    year=lv.year,
                    cover_url=(
                        f"/review/file/{file_id}/cover" if file_id else None
                    ),
                    issue_count=n,
                    matched_at=recent,
                )
            )

    # Merge: each side is already matched_at-ordered; re-sort the union
    # and take the most recently-active ``limit`` volumes.
    out.sort(key=lambda v: v.matched_at, reverse=True)
    return out[:limit]


# ---- Character + Creator pages -----------------------------------------
#
# Two ComicVine-backed entity pages. A character / person is fetched from
# CV; its ``raw_payload.issue_credits`` lists every issue it is credited
# in. Those stubs are enriched from the local ``cv_issues`` cache (cover,
# volume, owned status) and grouped into per-volume shelves — the same
# owned/missing aggregation the story-arc page does over an arc's member
# issues. The ``ArcIssueRow`` / ``ArcVolumeShelf`` row + shelf types are
# reused verbatim (``arc_idx`` is just an ordering index here).


@dataclass
class InfoRef:
    """A linked reference shown in the character info card.

    ``cv_id`` + ``name`` cover the simple case (creators). The issue
    fields — ``issue_number`` / ``cover_url`` / ``is_hydrated`` — are
    filled only for issue refs (the first appearance, the death
    issues). The first-appearance issue renders as a cover thumbnail,
    which is why ``get_character_detail`` hydrates that row's cover."""

    cv_id: int
    name: str
    issue_number: str | None = None
    cover_url: str | None = None
    is_hydrated: bool = False


@dataclass
class CharacterInfo:
    """The "General Information" sidebar card on the character page —
    parsed straight from the ComicVine character ``raw_payload``. Every
    field is optional; the template omits the rows it has no data for."""

    real_name: str | None = None
    # CV stores aliases as one newline-separated string; split to a list.
    aliases: list[str] = field(default_factory=list)
    # CV ``creators`` — each a (cv_id, name) ref for a /creator/<id> link.
    creators: list[InfoRef] = field(default_factory=list)
    gender: str | None = None
    # CV ``origin`` — "Human" / "Mutant" / "Robot" ... ("Character Type").
    character_type: str | None = None
    # CV ``first_appeared_in_issue`` — an issue ref, rendered as a cover
    # thumbnail. Its ``cover_url`` / ``is_hydrated`` are filled by
    # ``get_character_detail`` from the cv_issues cache.
    first_appearance: InfoRef | None = None
    # CV ``issues_died_in`` — every issue the character has died in.
    died_in: list[InfoRef] = field(default_factory=list)
    # CV ``count_of_issue_appearances`` — the catalogue-wide total (which
    # differs from the owned/total ring — that counts only the cache).
    appearance_count: int | None = None
    birthday: str | None = None
    powers: list[str] = field(default_factory=list)


@dataclass
class EntityCard:
    """One linkable entity in a person / team grid — id, name, and a
    small square ``icon`` image pulled from its CV cache table
    (``cv_characters`` for friends / enemies / team members,
    ``cv_teams`` for a character's teams). ``icon_url`` is None and
    ``is_hydrated`` False until the entity has been fetched; the route
    enqueues a hydration pass for the displayed window so the avatars
    fill in on a later render.

    ``badge`` is an optional small pill shown on the card before the
    name — used for a story arc's parsed ``"<book>"`` prefix."""

    cv_id: int
    name: str
    icon_url: str | None = None
    is_hydrated: bool = False
    badge: str | None = None


@dataclass
class CharacterDetail:
    """The /character/{cv_id} page payload."""

    character: Any  # CvCharacter
    name: str
    real_name: str | None
    deck: str | None
    description: str | None  # CV wiki content (HTML)
    cover_url: str | None
    banner_url: str | None  # CV's landscape screen image (screen_large_url)
    publisher_cv_id: int | None = None
    # Issue-appearance counts for the sidebar completeness ring —
    # ``total_count`` is the number of issues CV credits the character
    # in (its ``issue_credits`` list); ``owned_count`` how many are in
    # the user's library.
    total_count: int = 0
    owned_count: int = 0
    # "Appearances" tab — the volumes the character appears in. The CV
    # API's per-character volume data is unreliable, so this list is
    # scraped from the character's ``issues-cover`` web page into
    # ``cv_character_volumes`` and rendered as a card grid; each card
    # links to the volume page filtered to this character's issues.
    # ``appearance_volumes`` is the current page's window;
    # ``appearance_volumes_total`` the full (pre-filter) volume count.
    appearance_volumes: "list[VolumeCredit]" = field(default_factory=list)
    appearance_volumes_total: int = 0
    # True when the character's volume list hasn't been scraped yet —
    # the page shows a "building the volume list" state and the route
    # enqueues the scrape.
    volumes_scraping: bool = False
    # Server-side pagination of the appearance volume list — 1-based.
    page: int = 1
    page_count: int = 1
    # Active alphabet-bar letter filter (A-Z / "#"), or None.
    letter: str | None = None
    # Volume count after the letter filter — what the page tabs span.
    filtered_count: int = 0
    # CV-sourced facts for the "General Information" sidebar card.
    info: CharacterInfo = field(default_factory=CharacterInfo)
    # "Friends" tab — the character's allies, paginated as portrait
    # cards. ``friends`` is the current page's window; ``friends_total``
    # the full count. Page is 1-based, independent of the appearance
    # pager (its own ``?fpage=`` query param).
    friends: list[EntityCard] = field(default_factory=list)
    friends_page: int = 1
    friends_page_count: int = 1
    friends_total: int = 0
    # "Enemies" tab — same shape as Friends, with its own ``?epage=``
    # pager.
    enemies: list[EntityCard] = field(default_factory=list)
    enemies_page: int = 1
    enemies_page_count: int = 1
    enemies_total: int = 0
    # "Teams" tab — the character's team affiliations, same shape, its
    # own ``?tpage=`` pager. Cards link to /team/<id>.
    teams: list[EntityCard] = field(default_factory=list)
    teams_page: int = 1
    teams_page_count: int = 1
    teams_total: int = 0


@dataclass
class VolumeCredit:
    """One volume a creator is credited on — from CV ``volume_credits``,
    enriched with the cached volume's cover / year / format.

    ``cover_url`` / ``year`` / ``format`` are filled from ``cv_volumes``
    when the volume is cached; ``is_hydrated`` is False for a volume
    that's missing or still a stub, which the route uses to enqueue a
    hydration pass."""

    cv_id: int
    name: str
    cover_url: str | None = None
    year: int | None = None
    format: str | None = None
    is_hydrated: bool = False


async def get_hydrated_volume_credits(
    db: AsyncSession, cv_ids: list[int]
) -> list[VolumeCredit]:
    """Look up ``cv_volumes`` rows for ``cv_ids`` and return one
    ``VolumeCredit`` per row that is *no longer a stub* (i.e. its
    ``raw_payload._stub`` marker is gone). Rows still missing from
    the table or still flagged stub are filtered out — the
    /volume-credits/hydration endpoint uses that filtered list to
    drive ``setupAutoRefresh`` swaps and ``completed_ids``.
    """
    if not cv_ids:
        return []
    rows = (
        await db.execute(select(CvVolume).where(CvVolume.cv_id.in_(cv_ids)))
    ).scalars().all()
    out: list[VolumeCredit] = []
    for vol in rows:
        payload = vol.raw_payload if isinstance(vol.raw_payload, dict) else {}
        if payload.get("_stub") is True:
            continue
        out.append(
            VolumeCredit(
                cv_id=vol.cv_id,
                name=vol.name,
                cover_url=cv_image_url(payload, "thumb"),
                year=vol.year,
                format=classify_cv_volume(vol),
                is_hydrated=True,
            )
        )
    return out


@dataclass
class CreatorDetail:
    """The /creator/{cv_id} page payload — a ComicVine person.

    A creator's CV payload has no ``issue_credits`` (that's a character
    field); it carries ``volume_credits`` — the volumes they worked on,
    each with an issue count. The page shows that volume list. Drilling
    into the specific issues per volume is a later addition."""

    person: Any  # CvPerson
    name: str
    deck: str | None
    description: str | None  # CV wiki content (HTML)
    cover_url: str | None
    # "Credited volumes" tab — the current page's window of volumes.
    volume_credits: list[VolumeCredit] = field(default_factory=list)
    # Server-side pagination of the volume list — 1-based.
    page: int = 1
    page_count: int = 1
    # Total credited-volume count (before the letter filter).
    total: int = 0
    # Active alphabet-bar letter filter (A-Z / "#"), or None.
    letter: str | None = None
    # Volume count after the letter filter — what the pager spans.
    filtered_count: int = 0
    # "Created characters" tab — characters this creator created, as
    # avatar cards (own ``?cpage=`` pager + ``?cletter=`` alphabet bar,
    # sorted by name). ``filtered_count`` is the post-filter count.
    created_characters: list[EntityCard] = field(default_factory=list)
    created_characters_page: int = 1
    created_characters_page_count: int = 1
    created_characters_total: int = 0
    created_characters_letter: str | None = None
    created_characters_filtered_count: int = 0
    # "Story arcs" tab — arcs the creator is credited on (own
    # ``?apage=`` pager + ``?aletter=`` alphabet bar, sorted by name).
    story_arcs: list[EntityCard] = field(default_factory=list)
    story_arcs_page: int = 1
    story_arcs_page_count: int = 1
    story_arcs_total: int = 0
    story_arcs_letter: str | None = None
    story_arcs_filtered_count: int = 0


@dataclass
class TeamInfo:
    """The "General Information" sidebar card on the team page —
    parsed from the CV team ``raw_payload``. Deliberately holds only
    facts not shown elsewhere on the page (publisher, member count,
    name and bio live in the hero / chip / About card)."""

    aliases: list[str] = field(default_factory=list)
    # CV ``first_appeared_in_issue`` — rendered as a cover thumbnail;
    # its cover is filled by ``get_team_detail`` from the cv_issues
    # cache.
    first_appearance: InfoRef | None = None
    # CV ``issues_disbanded_in`` — every issue the team disbanded in.
    disbanded_in: list[InfoRef] = field(default_factory=list)


@dataclass
class TeamDetail:
    """The /team/{cv_id} page payload — a ComicVine team."""

    team: Any  # CvTeam
    name: str
    deck: str | None
    description: str | None  # CV wiki content (HTML)
    cover_url: str | None
    banner_url: str | None  # CV's landscape screen image (screen_large_url)
    publisher_cv_id: int | None = None
    # "Members" — the team's characters, paginated as avatar cards.
    members: list[EntityCard] = field(default_factory=list)
    members_page: int = 1
    members_page_count: int = 1
    members_total: int = 0
    # "Friends" tab — characters CV lists as allies of the team
    # (``character_friends``), paginated as avatar cards with their own
    # ``?fpage=`` pager. Cards link to /character/.
    friends: list[EntityCard] = field(default_factory=list)
    friends_page: int = 1
    friends_page_count: int = 1
    friends_total: int = 0
    # "Enemies" tab — same shape as Friends (``character_enemies``),
    # with its own ``?epage=`` pager.
    enemies: list[EntityCard] = field(default_factory=list)
    enemies_page: int = 1
    enemies_page_count: int = 1
    enemies_total: int = 0
    # "Volumes" tab — volumes the team is credited on (CV
    # ``volume_credits``), same shape as the creator page: its own
    # ``?vpage=`` pager + ``?vletter=`` alphabet bar.
    # ``volumes_filtered_count`` is the post-filter count.
    volumes: list[VolumeCredit] = field(default_factory=list)
    volumes_page: int = 1
    volumes_page_count: int = 1
    volumes_total: int = 0
    volumes_letter: str | None = None
    volumes_filtered_count: int = 0
    # "Story arcs" tab — arcs the team is credited on (CV
    # ``story_arc_credits``), as avatar cards sorted by name with their
    # own ``?apage=`` pager + ``?aletter=`` alphabet bar. Cards link to
    # /arc/. ``story_arcs_filtered_count`` is the post-filter count.
    story_arcs: list[EntityCard] = field(default_factory=list)
    story_arcs_page: int = 1
    story_arcs_page_count: int = 1
    story_arcs_total: int = 0
    story_arcs_letter: str | None = None
    story_arcs_filtered_count: int = 0
    # CV-sourced facts for the "General Information" sidebar card.
    info: TeamInfo = field(default_factory=TeamInfo)


def _appearance_matches_letter(volume_name: str | None, letter: str) -> bool:
    """True when ``volume_name`` falls in the alphabet-bar ``letter``
    bucket. ``'#'`` is the bucket for names starting with a non-letter;
    an issue with no volume name is in no bucket."""
    if not volume_name:
        return False
    first = volume_name.strip()[:1].upper()
    if letter == "#":
        return not ("A" <= first <= "Z")
    return first == letter


def _split_aliases(value: object) -> list[str]:
    """Split CV's newline-separated ``aliases`` string into a trimmed,
    de-duped list (CV occasionally repeats an alias)."""
    return list(
        dict.fromkeys(
            line.strip()
            for line in str(value or "").splitlines()
            if line.strip()
        )
    )


def _character_info(raw: dict) -> CharacterInfo:
    """Parse a CV character ``raw_payload`` into the info-card payload.

    Defensive throughout — a payload missing any given field just
    yields an empty value, and the template drops the empty rows.
    """
    aliases = _split_aliases(raw.get("aliases"))

    creators = [
        InfoRef(cv_id=int(c["id"]), name=c.get("name") or "")
        for c in (raw.get("creators") or [])
        if isinstance(c, dict) and c.get("id") is not None
    ]

    # CV gender is an int code: 1 male, 2 female, 0/other unknown.
    gender = {1: "Male", 2: "Female"}.get(raw.get("gender"))

    origin = raw.get("origin")
    character_type = (
        origin.get("name") if isinstance(origin, dict) else None
    ) or None

    fa = raw.get("first_appeared_in_issue")
    first_appearance: InfoRef | None = None
    if isinstance(fa, dict) and fa.get("id") is not None:
        first_appearance = InfoRef(
            cv_id=int(fa["id"]),
            name=fa.get("name") or "",
            issue_number=fa.get("issue_number") or None,
        )

    # CV ``issues_died_in`` — issue refs for every recorded death.
    # ``died`` is tolerated as a fallback key.
    died_raw = raw.get("issues_died_in") or raw.get("died") or []
    died_in = [
        InfoRef(
            cv_id=int(d["id"]),
            name=d.get("name") or "",
            issue_number=d.get("issue_number") or None,
        )
        for d in died_raw
        if isinstance(d, dict) and d.get("id") is not None
    ]

    powers = [
        p["name"]
        for p in (raw.get("powers") or [])
        if isinstance(p, dict) and p.get("name")
    ]

    appearance_count = raw.get("count_of_issue_appearances")
    if not isinstance(appearance_count, int):
        appearance_count = None

    return CharacterInfo(
        real_name=raw.get("real_name") or None,
        aliases=aliases,
        creators=creators,
        gender=gender,
        character_type=character_type,
        first_appearance=first_appearance,
        died_in=died_in,
        appearance_count=appearance_count,
        birthday=raw.get("birth") or None,
        powers=powers,
    )


async def _entity_card_page(
    db: AsyncSession,
    refs: list[dict],
    model: type,
    *,
    page: int,
    page_size: int,
    sort: bool = False,
    letter: str | None = None,
    name_transform: Callable[[str], tuple[str, str | None]] | None = None,
) -> tuple[list[EntityCard], int, int, int, int]:
    """Page a list of CV entity refs into avatar cards.

    ``model`` is the CV cache model to enrich from — ``CvCharacter``
    for ``character_friends`` / ``character_enemies`` / a team's
    ``characters`` / a creator's ``created_characters``, ``CvTeam`` for
    a character's ``teams``, ``CvStoryArc`` for ``story_arc_credits``.

    ``name_transform`` (given for story arcs) maps a raw ref name to a
    ``(name, badge)`` pair — used to split CV's quoted ``"<book>"``
    prefix off an arc name. ``sort`` orders the cards by name;
    ``letter`` narrows to a single alphabet-bar bucket (run *after* the
    transform, so it filters / sorts by the displayed name).

    Returns ``(cards, page, page_count, total, filtered_count)``. Refs
    are de-duped by id (CV order preserved unless ``sort``). Only the
    requested page is enriched — one batch lookup fills each avatar; an
    entity not in the cache gets ``icon_url`` None / ``is_hydrated``
    False and the route enqueues a hydration pass.
    """
    seen: set[int] = set()
    cards: list[EntityCard] = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("id") is None:
            continue
        cid = int(ref["id"])
        if cid in seen:
            continue
        seen.add(cid)
        raw_name = ref.get("name") or ""
        if name_transform is not None:
            name, badge = name_transform(raw_name)
        else:
            name, badge = raw_name, None
        cards.append(EntityCard(cv_id=cid, name=name, badge=badge))

    if sort:
        cards.sort(key=lambda c: c.name.casefold())
    total = len(cards)

    if letter:
        cards = [
            c for c in cards if _appearance_matches_letter(c.name, letter)
        ]
    filtered_count = len(cards)

    page_count = max(1, (filtered_count + page_size - 1) // page_size)
    page = min(max(page, 1), page_count)
    window = cards[(page - 1) * page_size : page * page_size]

    window_ids = [c.cv_id for c in window]
    if window_ids:
        rows = (
            await db.execute(
                select(model).where(model.cv_id.in_(window_ids))
            )
        ).scalars()
        enriched: dict[int, Any] = {row.cv_id: row for row in rows}
        for card in window:
            row = enriched.get(card.cv_id)
            if row is not None:
                card.icon_url = cv_image_url(row.raw_payload, "icon")
                card.is_hydrated = True
    return window, page, page_count, total, filtered_count


async def _count_owned_appearances(
    db: AsyncSession, issue_stubs: list[dict]
) -> tuple[int, int]:
    """``(total, owned)`` issue-appearance counts for a character — the
    figures behind the sidebar completeness ring.

    ``total`` is the distinct issue count in the CV ``issue_credits``
    list; ``owned`` how many of those issues have a matched file. One
    COUNT query; an empty credit list short-circuits to ``(0, 0)``.
    """
    member_ids = {
        int(s["id"])
        for s in issue_stubs
        if isinstance(s, dict) and s.get("id") is not None
    }
    if not member_ids:
        return 0, 0
    owned = (
        await db.execute(
            select(func.count(func.distinct(FileMatch.issue_cv_id))).where(
                FileMatch.issue_cv_id.in_(member_ids),
                FileMatch.status.in_(
                    (MatchStatus.AUTO.value, MatchStatus.CONFIRMED.value)
                ),
            )
        )
    ).scalar() or 0
    return len(member_ids), int(owned)


async def _character_volume_page(
    db: AsyncSession,
    character_cv_id: int,
    *,
    page: int,
    page_size: int,
    letter: str | None = None,
) -> tuple[list[VolumeCredit], int, int, int, int]:
    """Page a character's scraped volume-appearance list into volume
    cards.

    Reads ``cv_character_volumes`` (populated by the issues-cover
    scraper), builds one ``VolumeCredit`` per volume sorted by name,
    optionally narrows to an alphabet-bar ``letter``, then paginates.
    The page's window is enriched from ``cv_volumes``: a cached volume
    contributes its year / format / cover thumbnail and is marked
    hydrated; an uncached one keeps the scraped name + cover, and the
    route enqueues a hydration pass.

    Returns ``(cards, page, page_count, total, filtered_count)``.
    """
    rows = (
        (
            await db.execute(
                select(CvCharacterVolume).where(
                    CvCharacterVolume.character_cv_id == character_cv_id
                )
            )
        )
        .scalars()
        .all()
    )
    cards = [
        VolumeCredit(cv_id=r.volume_cv_id, name=r.name, cover_url=r.cover_url)
        for r in rows
    ]
    cards.sort(key=lambda c: c.name.casefold())
    total = len(cards)

    if letter:
        cards = [
            c for c in cards if _appearance_matches_letter(c.name, letter)
        ]
    filtered_count = len(cards)

    page_count = max(1, (filtered_count + page_size - 1) // page_size)
    page = min(max(page, 1), page_count)
    window = cards[(page - 1) * page_size : page * page_size]

    window_ids = [c.cv_id for c in window]
    if window_ids:
        volumes = {
            v.cv_id: v
            for v in (
                await db.execute(
                    select(CvVolume).where(CvVolume.cv_id.in_(window_ids))
                )
            ).scalars()
        }
        for card in window:
            volume = volumes.get(card.cv_id)
            if volume is None:
                continue
            payload = (
                volume.raw_payload
                if isinstance(volume.raw_payload, dict)
                else {}
            )
            # A stub row (``_stub: True``) only has id/name — treat it
            # as un-hydrated so the route fetches the real volume.
            card.is_hydrated = payload.get("_stub") is not True
            cached_cover = cv_image_url(payload, "thumb")
            if cached_cover:
                card.cover_url = cached_cover
            # Prefer the canonical cv_volumes title over the scraped
            # one — the issues-cover gallery's name (from a cover's alt
            # text) can be terse or off.
            if volume.name:
                card.name = volume.name
            card.year = volume.year
            card.format = classify_cv_volume(volume)
    return window, page, page_count, total, filtered_count


async def get_character_detail(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    cv_id: int,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    letter: str | None = None,
    friends_page: int = 1,
    enemies_page: int = 1,
    teams_page: int = 1,
) -> CharacterDetail | None:
    """Character page payload — the character fetched from ComicVine,
    plus the volumes it appears in (scraped into ``cv_character_volumes``
    from CV's issues-cover page, since the JSON API's per-character
    volume data is unreliable) rendered as a paginated card grid.

    Falls back to a cached row if a live CV fetch fails; returns None
    only when the character is unknown to both CV and the cache.
    """
    try:
        character = await cv_cache.get_character(db, cv_id)
    except ComicVineError:
        character = await db.get(CvCharacter, cv_id)
        if character is None:
            raise
    if character is None:
        return None

    raw = character.raw_payload or {}
    publisher_payload = raw.get("publisher") or {}
    publisher_cv_id = (
        int(publisher_payload["id"])
        if publisher_payload.get("id") is not None
        else None
    )
    # Issue-appearance counts for the sidebar completeness ring.
    total_count, owned_count = await _count_owned_appearances(
        db, raw.get("issue_credits") or []
    )
    # "Appearances" tab — the volumes scraped from CV's issues-cover
    # page. ``volumes_scraping`` stays True until that scrape has run;
    # the route enqueues it and the page shows a "building" state.
    volumes_scraping = character.volumes_scraped_at is None
    (
        appearance_volumes,
        page,
        page_count,
        appearance_volumes_total,
        filtered_count,
    ) = await _character_volume_page(
        db, cv_id, page=page, page_size=page_size, letter=letter
    )

    info = _character_info(raw)
    # The "First appearance" row renders a cover thumbnail — pull that
    # issue's cached cover + hydration state from cv_issues. A miss
    # (no row, or an un-hydrated stub) leaves ``cover_url`` None and
    # ``is_hydrated`` False, and the route then walks the issue.
    if info.first_appearance is not None:
        fa_row = await db.get(CvIssue, info.first_appearance.cv_id)
        if fa_row is not None:
            info.first_appearance.cover_url = cv_image_url(
                fa_row.raw_payload, "thumb"
            )
            info.first_appearance.is_hydrated = fa_row.fetched_at is not None

    # "Friends" / "Enemies" / "Teams" tabs — paginated avatar cards,
    # each with its own pager.
    friend_cards, friends_page, friends_page_count, friends_total, _ = (
        await _entity_card_page(
            db,
            raw.get("character_friends") or [],
            CvCharacter,
            page=friends_page,
            page_size=page_size,
        )
    )
    enemy_cards, enemies_page, enemies_page_count, enemies_total, _ = (
        await _entity_card_page(
            db,
            raw.get("character_enemies") or [],
            CvCharacter,
            page=enemies_page,
            page_size=page_size,
        )
    )
    team_cards, teams_page, teams_page_count, teams_total, _ = (
        await _entity_card_page(
            db,
            raw.get("teams") or [],
            CvTeam,
            page=teams_page,
            page_size=page_size,
        )
    )

    return CharacterDetail(
        character=character,
        name=character.name or raw.get("name") or "Unknown character",
        real_name=raw.get("real_name"),
        deck=raw.get("deck"),
        description=raw.get("description"),
        cover_url=cv_image_url(raw, "large"),
        banner_url=cv_image_url(raw, "banner"),
        publisher_cv_id=publisher_cv_id,
        total_count=total_count,
        owned_count=owned_count,
        appearance_volumes=appearance_volumes,
        appearance_volumes_total=appearance_volumes_total,
        volumes_scraping=volumes_scraping,
        page=page,
        page_count=page_count,
        letter=letter,
        filtered_count=filtered_count,
        info=info,
        friends=friend_cards,
        friends_page=friends_page,
        friends_page_count=friends_page_count,
        friends_total=friends_total,
        enemies=enemy_cards,
        enemies_page=enemies_page,
        enemies_page_count=enemies_page_count,
        enemies_total=enemies_total,
        teams=team_cards,
        teams_page=teams_page,
        teams_page_count=teams_page_count,
        teams_total=teams_total,
    )


async def _volume_credit_page(
    db: AsyncSession,
    refs: list[dict],
    *,
    page: int,
    page_size: int,
    letter: str | None = None,
) -> tuple[list[VolumeCredit], int, int, int, int]:
    """Page a CV ``volume_credits`` list into volume-credit rows.

    Returns ``(rows, page, page_count, total, filtered_count)``. Refs
    are de-duped by id and sorted by volume name; ``total`` counts them
    all. An optional alphabet-bar ``letter`` narrows to volumes whose
    name starts with it (``'#'`` is the non-letter bucket);
    ``filtered_count`` is the post-filter count the pager spans. Only
    the requested page's volumes are enriched — one ``cv_volumes``
    batch lookup fills each row's cover / year / format. A volume
    that's missing or only a stub gets no cover and ``is_hydrated``
    False; the route enqueues a hydration pass.
    """
    seen: set[int] = set()
    rows: list[VolumeCredit] = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("id") is None:
            continue
        cid = int(ref["id"])
        if cid in seen:
            continue
        seen.add(cid)
        rows.append(VolumeCredit(cv_id=cid, name=ref.get("name") or ""))
    rows.sort(key=lambda r: r.name.casefold())
    total = len(rows)

    # Alphabet-bar filter — by the volume name's first letter.
    if letter:
        rows = [
            r for r in rows if _appearance_matches_letter(r.name, letter)
        ]
    filtered_count = len(rows)

    page_count = max(1, (filtered_count + page_size - 1) // page_size)
    page = min(max(page, 1), page_count)
    window = rows[(page - 1) * page_size : page * page_size]

    window_ids = [r.cv_id for r in window]
    if window_ids:
        volumes = {
            v.cv_id: v
            for v in (
                await db.execute(
                    select(CvVolume).where(CvVolume.cv_id.in_(window_ids))
                )
            ).scalars()
        }
        for row in window:
            volume = volumes.get(row.cv_id)
            if volume is None:
                continue
            payload = (
                volume.raw_payload
                if isinstance(volume.raw_payload, dict)
                else {}
            )
            # A stub row (``_stub: True``) has only id/name — treat it
            # as un-hydrated so the route fetches the real volume.
            row.is_hydrated = payload.get("_stub") is not True
            row.cover_url = cv_image_url(payload, "thumb")
            row.year = volume.year
            row.format = classify_cv_volume(volume)
    return window, page, page_count, total, filtered_count


def _arc_card_name(raw: str) -> tuple[str, str | None]:
    """Name-transform for story-arc cards — split CV's quoted
    ``"<book>"`` prefix off, returning ``(clean_name, book)`` for
    ``_entity_card_page`` (it sets ``EntityCard.name`` / ``.badge``)."""
    book, clean = parse_arc_name(raw)
    return clean, book


async def get_creator_detail(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    cv_id: int,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    letter: str | None = None,
    characters_page: int = 1,
    characters_letter: str | None = None,
    arcs_page: int = 1,
    arcs_letter: str | None = None,
) -> CreatorDetail | None:
    """Creator page payload — the person fetched from ComicVine, plus
    three tabbed lists: the volumes they are credited on (CV
    ``volume_credits``, with an alphabet-bar letter filter), the
    characters they created (``created_characters``), and the story
    arcs they are credited on (``story_arc_credits``).

    Falls back to a cached row if a live CV fetch fails; returns None
    only when the person is unknown to both CV and the cache.
    """
    try:
        person = await cv_cache.get_person(db, cv_id)
    except ComicVineError:
        person = await db.get(CvPerson, cv_id)
        if person is None:
            raise
    if person is None:
        return None

    raw = person.raw_payload or {}
    volume_credits, page, page_count, total, filtered_count = (
        await _volume_credit_page(
            db,
            raw.get("volume_credits") or [],
            page=page,
            page_size=page_size,
            letter=letter,
        )
    )
    # Both avatar tabs are sorted by name and alphabet-bar filterable.
    # Story arcs additionally split CV's quoted ``"<book>"`` prefix off
    # the name (``_arc_card_name``) — done before the sort / filter so
    # they key on the displayed name, with the book as the card badge.
    char_cards, cpage, cpage_count, ctotal, cfiltered = (
        await _entity_card_page(
            db,
            raw.get("created_characters") or [],
            CvCharacter,
            page=characters_page,
            page_size=page_size,
            sort=True,
            letter=characters_letter,
        )
    )
    arc_cards, apage, apage_count, atotal, afiltered = (
        await _entity_card_page(
            db,
            raw.get("story_arc_credits") or [],
            CvStoryArc,
            page=arcs_page,
            page_size=page_size,
            sort=True,
            letter=arcs_letter,
            name_transform=_arc_card_name,
        )
    )

    return CreatorDetail(
        person=person,
        name=person.name or raw.get("name") or "Unknown creator",
        deck=raw.get("deck"),
        description=raw.get("description"),
        cover_url=cv_image_url(raw, "large"),
        volume_credits=volume_credits,
        page=page,
        page_count=page_count,
        total=total,
        letter=letter,
        filtered_count=filtered_count,
        created_characters=char_cards,
        created_characters_page=cpage,
        created_characters_page_count=cpage_count,
        created_characters_total=ctotal,
        created_characters_letter=characters_letter,
        created_characters_filtered_count=cfiltered,
        story_arcs=arc_cards,
        story_arcs_page=apage,
        story_arcs_page_count=apage_count,
        story_arcs_total=atotal,
        story_arcs_letter=arcs_letter,
        story_arcs_filtered_count=afiltered,
    )


def _team_info(raw: dict) -> TeamInfo:
    """Parse a CV team ``raw_payload`` into the info-card payload —
    aliases, first appearance, and the issues the team disbanded in.
    Defensive: a missing field just yields an empty value.
    """
    fa = raw.get("first_appeared_in_issue")
    first_appearance: InfoRef | None = None
    if isinstance(fa, dict) and fa.get("id") is not None:
        first_appearance = InfoRef(
            cv_id=int(fa["id"]),
            name=fa.get("name") or "",
            issue_number=fa.get("issue_number") or None,
        )

    # CV ``issues_disbanded_in`` — ``disbanded_in_issues`` tolerated as
    # a fallback key.
    disbanded_raw = (
        raw.get("issues_disbanded_in")
        or raw.get("disbanded_in_issues")
        or []
    )
    disbanded_in = [
        InfoRef(
            cv_id=int(d["id"]),
            name=d.get("name") or "",
            issue_number=d.get("issue_number") or None,
        )
        for d in disbanded_raw
        if isinstance(d, dict) and d.get("id") is not None
    ]
    return TeamInfo(
        aliases=_split_aliases(raw.get("aliases")),
        first_appearance=first_appearance,
        disbanded_in=disbanded_in,
    )


async def get_team_detail(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    cv_id: int,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    friends_page: int = 1,
    enemies_page: int = 1,
    volumes_page: int = 1,
    volumes_letter: str | None = None,
    arcs_page: int = 1,
    arcs_letter: str | None = None,
) -> TeamDetail | None:
    """Team page payload — the team fetched from ComicVine, plus its
    members (characters) paginated as avatar cards and a "General
    Information" sidebar card.

    ``friends_page`` / ``enemies_page`` are the pages of the "Friends" /
    "Enemies" tabs — characters CV allies / opposes the team with, each
    its own pager. ``volumes_page`` / ``volumes_letter`` page and
    alphabet-filter the "Volumes" tab (CV ``volume_credits``);
    ``arcs_page`` / ``arcs_letter`` do the same for the "Story arcs"
    tab (CV ``story_arc_credits``).

    Falls back to a cached row if a live CV fetch fails; returns None
    only when the team is unknown to both CV and the cache.
    """
    try:
        team = await cv_cache.get_team(db, cv_id)
    except ComicVineError:
        team = await db.get(CvTeam, cv_id)
        if team is None:
            raise
    if team is None:
        return None

    raw = team.raw_payload or {}
    publisher_payload = raw.get("publisher") or {}
    publisher_cv_id = (
        int(publisher_payload["id"])
        if publisher_payload.get("id") is not None
        else None
    )
    member_cards, members_page, members_page_count, members_total, _ = (
        await _entity_card_page(
            db,
            raw.get("characters") or [],
            CvCharacter,
            page=page,
            page_size=page_size,
        )
    )
    # "Friends" / "Enemies" tabs — characters, same as the character
    # page's allies / foes. Each gets its own paged window of avatars.
    friend_cards, friends_page, friends_page_count, friends_total, _ = (
        await _entity_card_page(
            db,
            raw.get("character_friends") or [],
            CvCharacter,
            page=friends_page,
            page_size=page_size,
        )
    )
    enemy_cards, enemies_page, enemies_page_count, enemies_total, _ = (
        await _entity_card_page(
            db,
            raw.get("character_enemies") or [],
            CvCharacter,
            page=enemies_page,
            page_size=page_size,
        )
    )
    # "Volumes" tab — credited volumes, same as the creator page (its
    # own pager + alphabet-bar letter filter).
    (volume_rows, volumes_page, volumes_page_count, volumes_total,
     volumes_filtered) = await _volume_credit_page(
        db,
        raw.get("volume_credits") or [],
        page=volumes_page,
        page_size=page_size,
        letter=volumes_letter,
    )
    # "Story arcs" tab — arcs the team is credited on, as avatar cards
    # sorted by name (CV's quoted ``"<book>"`` prefix split off via
    # ``_arc_card_name``), with their own pager + alphabet bar.
    (arc_cards, arcs_page, arcs_page_count, arcs_total,
     arcs_filtered) = await _entity_card_page(
        db,
        raw.get("story_arc_credits") or [],
        CvStoryArc,
        page=arcs_page,
        page_size=page_size,
        sort=True,
        letter=arcs_letter,
        name_transform=_arc_card_name,
    )

    info = _team_info(raw)
    # The "First appearance" row renders a cover thumbnail — pull that
    # issue's cached cover + hydration state from cv_issues (the route
    # walks the issue when it isn't hydrated yet).
    if info.first_appearance is not None:
        fa_row = await db.get(CvIssue, info.first_appearance.cv_id)
        if fa_row is not None:
            info.first_appearance.cover_url = cv_image_url(
                fa_row.raw_payload, "thumb"
            )
            info.first_appearance.is_hydrated = fa_row.fetched_at is not None

    return TeamDetail(
        team=team,
        name=team.name or raw.get("name") or "Unknown team",
        deck=raw.get("deck"),
        description=raw.get("description"),
        cover_url=cv_image_url(raw, "large"),
        banner_url=cv_image_url(raw, "banner"),
        publisher_cv_id=publisher_cv_id,
        members=member_cards,
        members_page=members_page,
        members_page_count=members_page_count,
        members_total=members_total,
        friends=friend_cards,
        friends_page=friends_page,
        friends_page_count=friends_page_count,
        friends_total=friends_total,
        enemies=enemy_cards,
        enemies_page=enemies_page,
        enemies_page_count=enemies_page_count,
        enemies_total=enemies_total,
        volumes=volume_rows,
        volumes_page=volumes_page,
        volumes_page_count=volumes_page_count,
        volumes_total=volumes_total,
        volumes_letter=volumes_letter,
        volumes_filtered_count=volumes_filtered,
        story_arcs=arc_cards,
        story_arcs_page=arcs_page,
        story_arcs_page_count=arcs_page_count,
        story_arcs_total=arcs_total,
        story_arcs_letter=arcs_letter,
        story_arcs_filtered_count=arcs_filtered,
        info=info,
    )
