"""Small helpers for working with ComicVine ``raw_payload`` JSON.

Kept in a tiny module so templates and services can both import without
pulling in heavier infrastructure.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# CV's image field is a dict of URL variants. Portrait cover variants
# (what we display in 2:3 cards), small → large:
#   thumb_url, small_url, medium_url, super_url, original_url
# NOT portrait covers — wrong aspect for a 2:3 slot:
#   icon_url        — CV's 50x50 SQUARE square_avatar (a tight crop)
#   tiny_url        — a small square-ish variant
#   screen_url, screen_large_url — landscape banner / promo images
# Cover-shaped sizes ("thumb"/"medium"/"large") therefore fall back
# ONLY among the portrait variants: dropping ``icon_url`` into a 2:3
# card renders a zoomed, mis-cropped fragment (the bug this avoids).
# ``object-cover`` would likewise crop a screen_* banner into a
# useless center sliver, so those are excluded too.
_IMAGE_SIZES_BY_PREFERENCE: dict[str, list[str]] = {
    # Portrait variants only — see the note above. An issue whose
    # payload carries none of these resolves to None (a clean empty
    # 2:3 placeholder) rather than a squashed square avatar.
    "thumb": ["thumb_url", "small_url", "medium_url"],
    "medium": ["medium_url", "small_url", "thumb_url"],
    # ``super_url`` is CV's largest portrait cover (~1000-1500px tall).
    # Use this tier for grid cards on retina displays.
    "large": ["super_url", "medium_url", "small_url", "thumb_url"],
    # Landscape banner variants. Used as a hero strip on detail pages
    # (volume / issue). NO fallback to portrait covers — if CV didn't
    # supply a banner, callers should render nothing rather than show a
    # cropped sliver of a portrait image.
    "banner": ["screen_large_url", "screen_url"],
    # Small square avatar — publisher logos, character/team mugshots in
    # sidebars. ``icon_url`` is CV's 50x50 square_avatar variant; the
    # rest are fallbacks that we'd scale down rather than show nothing.
    "icon": ["icon_url", "tiny_url", "thumb_url", "small_url"],
}


def cv_image_url(payload: dict | None, size: str = "medium") -> str | None:
    """Return a usable image URL from a CV ``raw_payload`` dict.

    ``size`` is one of: thumb, medium, large, original. Falls through the
    preference list — most CV records have all variants, but a few only
    have the smallest one and we'd rather show a small image than nothing.
    Returns None if the payload has no usable image data.
    """
    if not isinstance(payload, dict):
        return None
    image = payload.get("image")
    if not isinstance(image, dict):
        return None
    for key in _IMAGE_SIZES_BY_PREFERENCE.get(size, _IMAGE_SIZES_BY_PREFERENCE["medium"]):
        url = image.get(key)
        if url:
            return str(url)
    return None


# Canonical ComicVine site URLs built from a resource cv_id. CV's
# documented resource prefixes (``4050-`` for volume, ``4000-`` for
# issue, etc.) match what the API client already uses to fetch by id,
# so the on-site URL composes the same way. Used by review-page links
# that take the user out to the source-of-truth page for an entity.
_CV_SITE_BASE = "https://comicvine.gamespot.com"


def cv_volume_url(cv_id: int | None) -> str | None:
    """Canonical ComicVine site URL for a volume cv_id."""
    if cv_id is None:
        return None
    return f"{_CV_SITE_BASE}/volume/4050-{cv_id}/"


def cv_issue_url(cv_id: int | None) -> str | None:
    """Canonical ComicVine site URL for an issue cv_id."""
    if cv_id is None:
        return None
    return f"{_CV_SITE_BASE}/issue/4000-{cv_id}/"


def safe_int(v: Any) -> int | None:
    """Coerce a CV-payload value to ``int``, returning None on failure.

    ComicVine's JSON is inconsistent about numeric fields: ``start_year``
    and ``count_of_issues`` arrive as ints in most rows but as strings
    (often padded with whitespace) in older catalogue entries; ``id``
    fields on nested stubs occasionally come back as strings too. This
    helper accepts any of those shapes and yields a clean int or None.

    Used by the matcher pipeline, the CV cache upserts, and the library
    service when reading year-ish values out of stored payloads. Kept in
    cv_helpers (rather than each module) so callers all get the same
    permissive parser — the matcher previously raised on whitespace-padded
    strings that the library layer happily handled, and the discrepancy
    was a latent bug source."""
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def parse_iso_date(raw: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` string into a ``date``.

    None / blank / unparseable inputs yield None. Used on free-text
    form fields (the local-issue cover-date input, primarily) where
    the browser sometimes hands back a stray empty string instead of
    a missing key, and where a typo'd value shouldn't 500 — silently
    dropping the bad value lets the form re-render with the rest of
    the user's edits intact."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def parse_id_csv(ids: str | None) -> list[int]:
    """Parse a ``?ids=1,2,3`` query-string into ``list[int]``.

    Used by the polling endpoints (``/covers``, ``/hydrate``, ...) that
    receive a comma-separated batch of CV ids the client is still
    waiting on. Non-numeric tokens and stray whitespace are silently
    skipped — the alternative is a 400 on every spurious trailing
    comma, which the JS callers can't usefully recover from.

    None / empty input yields ``[]``. Kept here next to ``safe_int``
    rather than at each call site because five route handlers had
    quietly drifted out of sync — one missed the strip(), another
    raised on negative ids — and the duplication was the bug source."""
    if not ids:
        return []
    parsed: list[int] = []
    for token in ids.split(","):
        token = token.strip()
        if token.isdigit():
            parsed.append(int(token))
    return parsed


# CV story-arc names sometimes come prefixed with a quoted reference
# to the parent book or theme, e.g. ``"Avengers" Disassembled`` —
# the quotes are CV's namespacing convention, not a typo. Display-wise
# the prefix is noisy ("Avengers" being repeated dozens of times in
# a single volume's legend isn't useful), but the book itself is
# disambiguating metadata, so we keep it as a sidecar value rather
# than discarding it.
_ARC_NAME_PRIMARY_BOOK_RE = re.compile(r'^"([^"]+)"\s+(.+)$')


def parse_arc_name(raw: str | None) -> tuple[str | None, str]:
    """Split CV's ``"<book>" <arc name>`` prefix from the arc name proper.

    Returns ``(primary_book, clean_name)``. When the input lacks the
    leading quoted prefix, ``primary_book`` is None and ``clean_name``
    is the input trimmed. Empty/None inputs yield ``(None, "")``.

    The parser is deliberately conservative: it only fires on the
    documented ``"<text>" <rest>`` shape (leading quote, balanced
    closing quote, whitespace, then more content). Mid-string quotes
    or quote-only inputs fall through to the no-prefix branch, since
    we can't be sure CV intends them as the book-prefix convention.
    """
    if not raw:
        return None, raw or ""
    trimmed = raw.strip()
    match = _ARC_NAME_PRIMARY_BOOK_RE.match(trimmed)
    if not match:
        return None, trimmed
    return match.group(1).strip(), match.group(2).strip()


# Natural-sort key for issue_number strings. Handles "1", "10", "1.5",
# ".5", "1.MU", "Annual 1", "0", "-1". Numerics sort before strings;
# pure-numeric values sort by numeric magnitude, not by lexical order.
_NUMERIC_PREFIX_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(.*)$")


def sort_key_issue_number(value: Any) -> tuple:
    """Return a sort key for use with ``sorted(... key=sort_key_issue_number)``.

    Issues sort first by numeric prefix (numeric → 0-prefix tuple),
    then by any non-numeric suffix (".MU", ".NOW", " Annual"), then
    fully-non-numeric strings ("Annual 1") sort to the end.
    """
    if value is None:
        return (2, "")  # nulls last
    s = str(value).strip()
    if not s:
        return (2, "")
    m = _NUMERIC_PREFIX_RE.match(s)
    if m:
        num = float(m.group(1))
        suffix = m.group(2)
        return (0, num, suffix.lower())
    return (1, s.lower())


# ---- Format classification ---------------------------------------------
#
# ComicVine has no native volume-type field — a monthly series, a limited
# run, a one-shot and a TPB collection are all just "volumes" with a
# count_of_issues. We waterfall: a high issue count is an ongoing series;
# a handful is a limited series; a lone-issue volume is either a one-shot
# or a collected edition, told apart by keywords in the volume name /
# deck / description / first issue name.
#
# Kept here in cv_helpers (rather than the review service) so both the
# review layer AND the matcher pipeline can import it without a circular
# dependency.

# A volume with at least this many issues reads as an ongoing series
# rather than a limited run.
ONGOING_MIN_ISSUES = 12

# Phrases that mark a collected edition (TPB / HC / omnibus / ...).
# Matched case-insensitively as substrings, so kept specific enough
# not to fire on ordinary series text.
_COLLECTION_KEYWORDS = (
    "collects",
    # "collected edition" also substring-matches the plural
    # "collected editions".
    "collected edition",
    "big edition",
    "trade paperback",
    "tpb",
    "omnibus",
    "compendium",
    "deluxe edition",
    "library edition",
    "epic collection",
    "hardcover",
    "reprints",
)

# Phrases that mark a one-shot. Checked before the collection
# markers — an explicit "one-shot" is unambiguous.
_ONE_SHOT_KEYWORDS = ("one-shot", "one shot", "oneshot")

# Page count above which a file reads as a collected edition rather
# than a single issue. Single issues land ~20-40pp; a generous cutoff
# keeps oversized issues / annuals on the "single issue" side.
COLLECTION_PAGE_THRESHOLD = 64


def classify_volume_format(
    *,
    name: str | None,
    count_of_issues: Any,
    deck: str | None,
    description: str | None,
    first_issue_name: str | None,
) -> str:
    """Classify a CV volume as ``ongoing`` / ``limited`` / ``one_shot``
    / ``collection``, or ``unknown`` when there's too little to tell.

    The issue count decides first, and decisively:

      * many issues (``>= ONGOING_MIN_ISSUES``) → ``ongoing``
      * a handful (2..11) → ``limited``
      * exactly one → a lone-issue volume, either a one-shot or a
        single collected edition; *only here* are the name / deck /
        description / first-issue-name keywords consulted, to tell
        those two apart.
      * no reported count (typically an un-hydrated stub
        ``cv_volumes`` row) → ``unknown``. The absence of a count is
        not read as "one-shot".

    Keywords are deliberately confined to the lone-issue case. An
    ongoing multi-issue series whose CV description merely *mentions*
    its collected editions — e.g. Vagabond, an ongoing VIZ series
    that collects the original tankobon — must keep reading as
    ``ongoing``; letting a "collected edition" substring override a
    37-issue count mislabels it ``collection``.

    Trade-off: ComicVine sometimes catalogs a multi-volume
    collected-editions *line* as a single "volume" whose "issues"
    are the collected books. With keywords no longer overriding the
    count, such a line reads as ``ongoing`` / ``limited`` by its
    issue count — the accepted cost of never mislabelling a genuine
    ongoing series.
    """
    try:
        count = int(count_of_issues)
    except (TypeError, ValueError):
        count = None

    if count is None:
        return "unknown"
    if count >= ONGOING_MIN_ISSUES:
        return "ongoing"
    if count >= 2:
        return "limited"
    if count == 1:
        # A lone-issue volume is either a one-shot or a single
        # collected edition (a TPB / omnibus catalogued on its own).
        # This is the ONLY place keywords are consulted. An explicit
        # one-shot marker is unambiguous, so it's checked first; a
        # collection marker (TPB / omnibus / "collects #1-6" / ...)
        # otherwise flags it ``collection``; with neither, a
        # lone-issue volume defaults to ``one_shot``.
        haystack = " ".join(t for t in (name, deck, description, first_issue_name) if t).lower()
        if any(kw in haystack for kw in _ONE_SHOT_KEYWORDS):
            return "one_shot"
        if any(kw in haystack for kw in _COLLECTION_KEYWORDS):
            return "collection"
        return "one_shot"
    # count <= 0 — a degenerate value; don't guess.
    return "unknown"


def classify_file_format(page_count: int | None) -> str:
    """Guess whether a file is a single ``issue`` or a ``collection``
    from its page count, or ``unknown`` when there's no count.

    A coarse signal — the page count is the cheapest tell for whether
    a file is one issue (~20-40pp) or a collected edition (100pp+).
    The threshold leans toward "single issue" so an oversized one-shot
    isn't mislabelled."""
    if not page_count or page_count <= 0:
        return "unknown"
    if page_count > COLLECTION_PAGE_THRESHOLD:
        return "collection"
    return "issue"


# ---- ComicVine "themes" → status / type ------------------------------
#
# A volume's CV web page carries a "Themes" row — genre / era /
# publication-status tags. The JSON API omits them, so they are scraped
# (see app.comicvine.scrape). Each theme has a stable integer id; these
# maps pick out the ids carrying a publication status or a series type.
# When a volume is tagged with more than one (CV tagging is user-driven
# and messy), the order tuples break the tie — most terminal / specific
# first, so e.g. a volume tagged both "Complete" and "Ongoing" reads as
# complete.

_THEME_STATUS_BY_ID: dict[int, str] = {
    51: "cancelled",
    52: "complete",
    61: "ongoing",
    66: "unfinished",
}
_STATUS_ORDER: tuple[str, ...] = (
    "cancelled",
    "complete",
    "unfinished",
    "ongoing",
)

# Theme ids → the same series-type buckets ``classify_volume_format``
# produces, so a scraped type is a drop-in for the heuristic one.
_THEME_TYPE_BY_ID: dict[int, str] = {
    60: "one_shot",
    58: "limited",  # Mini-Series
    57: "limited",  # Maxi-Series
    12: "collection",
    61: "ongoing",
}
_TYPE_ORDER: tuple[str, ...] = (
    "one_shot",
    "limited",
    "collection",
    "ongoing",
)


def _theme_ids(themes: Any) -> set[int]:
    """The integer ids out of a stored themes array — each entry a
    ``{"id", "name"}`` dict. Tolerates junk / missing ids."""
    ids: set[int] = set()
    for theme in themes or []:
        if not isinstance(theme, dict):
            continue
        try:
            ids.add(int(theme["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def volume_status_from_themes(themes: Any) -> str | None:
    """Publication status from a volume's scraped CV themes —
    ``cancelled`` / ``complete`` / ``unfinished`` / ``ongoing``, or
    None when no status theme is present."""
    present = {_THEME_STATUS_BY_ID[i] for i in _theme_ids(themes) if i in _THEME_STATUS_BY_ID}
    return next((s for s in _STATUS_ORDER if s in present), None)


def volume_type_from_themes(themes: Any) -> str | None:
    """Series type from a volume's scraped CV themes — one of the
    ``classify_volume_format`` buckets, or None when no type theme is
    present (the caller falls back to the issue-count heuristic)."""
    present = {_THEME_TYPE_BY_ID[i] for i in _theme_ids(themes) if i in _THEME_TYPE_BY_ID}
    return next((t for t in _TYPE_ORDER if t in present), None)


def classify_cv_volume(volume: Any) -> str:
    """Classify a cached ``cv_volumes`` row's series type.

    A type scraped from the volume's CV "themes" (``volume.themes``) is
    authoritative and wins outright. Otherwise this falls back to
    ``classify_volume_format``, pulling the inputs from the row's
    column data + raw_payload.

    Duck-typed: ``volume`` just needs ``.name``, ``.count_of_issues``,
    ``.raw_payload`` and (optionally) ``.themes`` — works for the
    ``CvVolume`` ORM row without cv_helpers having to import the model."""
    scraped_type = volume_type_from_themes(getattr(volume, "themes", None))
    if scraped_type is not None:
        return scraped_type
    raw = getattr(volume, "raw_payload", None)
    payload = raw if isinstance(raw, dict) else {}
    first_issue = payload.get("first_issue")
    return classify_volume_format(
        name=getattr(volume, "name", None),
        count_of_issues=getattr(volume, "count_of_issues", None),
        deck=payload.get("deck"),
        description=payload.get("description"),
        first_issue_name=(first_issue.get("name") if isinstance(first_issue, dict) else None),
    )
