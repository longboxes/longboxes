"""Filename parser — thin wrapper over ``comicfn2dict``.

comicfn2dict handles the long tail of comic-filename conventions: bracketed
year (`Series (2018) #001.cbz`), space-separated number (`Series 001.cbz`),
hash-prefixed (`Series #1.cbz`), scanlator tags (`Series 001 (Scangroup).cbz`),
and so on. It returns a dict with keys like ``series``, ``issue``, ``year``,
``volume``, ``ext``, ``remainders``.

We normalise its output into a typed ``ParsedFilename`` so the matcher
doesn't deal with dict-key drift between comicfn2dict versions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from comicfn2dict import comicfn2dict


@dataclass(frozen=True)
class ParsedFilename:
    """Best-effort fields extracted from a comic archive's basename.

    ``series`` is the most important — if it's None, the matcher can't search.
    ``issue_number`` is stored as a string to preserve decimals (".5", "1.MU").
    """

    series: str | None
    issue_number: str | None
    year: int | None
    volume_year: int | None
    # Year extracted from a parent directory's ``(YYYY)`` tag — the
    # ``/library/Wonder Woman (2011)/Wonder Woman 023.cbz`` convention
    # popular with Mylar / Komga / Kapowarr layouts. Filenames in
    # those layouts often omit the year because the folder carries
    # it. Without this fallback the matcher had no year signal for
    # those files, so the prefilter sort tiebreaker neutralised and
    # CV's relevance ranking decided the winner — which surfaces
    # popular old volumes (1987 Wonder Woman vol 2, 1939 Batman vol
    # 1, etc.) for any modern issue. See the year fallback chain in
    # ``app/matcher/pipeline.py::_stage_2_through_4``. ``None`` when
    # no parent directory carried a parseable year.
    path_year: int | None
    # Best-effort "everything before the issue marker" series prefix.
    # comicfn2dict truncates the series at a hyphen, so filenames
    # like ``Avengers - No More Bullying #001 (2015).cbr`` come back
    # with ``series="Avengers"`` — losing the subtitle that often
    # disambiguates a one-shot from the mainline volume of the same
    # name. ``long_series`` captures the prefix in full so the matcher
    # can run a parallel CV search. ``None`` when we couldn't safely
    # locate the issue marker (no ``#NNN`` form AND a trailing ``NNN``
    # could collide with a year etc.). Equal to ``series`` for the
    # common case where comicfn2dict already had it right.
    long_series: str | None
    # The raw parser output, retained for debugging / future heuristics.
    raw: dict


def parse_filename(filename: str | Path) -> ParsedFilename:
    """Parse a comic archive filename (or full path) into typed fields.

    Always returns a ``ParsedFilename`` — never raises. If parsing fails
    completely, fields are None and the matcher falls through to its
    "unmatched" path.
    """
    path = Path(filename)
    basename = path.name
    try:
        raw = comicfn2dict(basename)
    except Exception:  # pragma: no cover — comicfn2dict is best-effort by design
        raw = {}

    series = _norm_str(raw.get("series"))
    issue_number = _norm_issue_number(raw.get("issue"))
    year = _norm_int(raw.get("year"))

    return ParsedFilename(
        series=series,
        issue_number=issue_number,
        year=year,
        volume_year=_norm_int(raw.get("volume")),
        # Only walk parents when the basename itself didn't surface a
        # year — the filename's own year wins on the rare occasions
        # both are present (e.g. a one-shot moved into a series
        # folder), and skipping the walk keeps the parser cheap for
        # the common path-less call sites (tests / parsing a bare
        # filename string).
        path_year=_extract_path_year(path) if year is None else None,
        long_series=_extract_long_series(basename, issue_number, year) or series,
        raw=raw,
    )


# Trailing parenthesised / bracketed group — the year, a scanlator
# tag, a format tag. Stripped repeatedly so a filename like
# "Series 001 (2013) (digital-Empire)" reduces to "Series 001",
# putting the bare issue number back at the end of the string.
_TRAILING_GROUP_RE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")


def _extract_long_series(
    basename: str, issue_number: str | None, year: int | None
) -> str | None:
    """Return the series prefix of ``basename`` up to the issue marker.

    Used as a fallback search term when comicfn2dict's ``series``
    field truncates at a hyphen (``"Avengers - No More Bullying"``
    → ``"Avengers"``). We need the full prefix because CV's volume
    names usually include the subtitle, and the matcher's fuzzy
    similarity scoring works better with the long form.

    Algorithm:

      1. Strip the file extension.
      2. Strip the parenthesised year, then strip every trailing
         ``( ... )`` / ``[ ... ]`` group — scanlator / group / format
         tags such as ``(digital-Empire)``. Those sit AFTER the issue
         number, so leaving them on would stop the end-anchored
         bare-number lookup in step 3 from ever firing.
      3. Locate the issue marker. ``#NNN`` (with optional zero
         padding) wins first because the ``#`` makes it unambiguous;
         a bare ``NNN`` now at end-of-string is the fallback.
      4. Everything before the marker, trimmed of separator
         characters, is the long series.

    Returns ``None`` if we couldn't safely locate a marker — caller
    falls back to the comicfn2dict series."""
    if not issue_number:
        return None
    stem = Path(basename).stem
    # Strip the parenthesised year so a year-numeric like 1991 doesn't
    # confuse the bare-number issue-marker lookup further down.
    if year is not None:
        stem = re.sub(rf"\s*\(\s*{year}\s*\)\s*", " ", stem).strip()
    # Strip trailing tag groups — "(digital-Empire)", "[c2c]", a
    # stray "(2013)" — so a bare issue number lands at the end.
    prev = None
    while prev != stem:
        prev = stem
        stem = _TRAILING_GROUP_RE.sub("", stem).strip()

    # ``re.escape`` covers issue numbers like "1.MU" / "1/2" / ".5"
    # that contain regex meta-characters.
    n = re.escape(issue_number)
    patterns = (
        # "#001" — explicit issue marker, position-independent.
        rf"\s*#0*{n}\b",
        # Bare "001" at end-of-string after the strips above.
        rf"\s+0*{n}\s*$",
    )
    for pattern in patterns:
        m = re.search(pattern, stem)
        if m and m.start() > 0:
            candidate = stem[: m.start()].strip()
            # Drop trailing separator characters (``-``, ``:``, ``,``,
            # ``.``) — those sit between the series and the issue
            # marker in common conventions but aren't part of the
            # series itself.
            candidate = candidate.rstrip(" -:,.").strip()
            if candidate:
                return candidate
    return None


# Bracketed-year tag in a directory component, e.g.
# ``Wonder Woman (2011)`` or ``Saga [2012]``. Captures the inner
# digits; the caller validates whether the captured number plausibly
# looks like a publication year.
_PATH_YEAR_RE = re.compile(r"[(\[](\d{4})[)\]]")

# Lower-bound floor for "is this number plausibly a publication year".
# Modern comic publishing starts in the late 1930s; 1930 is the safe
# floor that excludes folder tags like ``(0)`` or ``(1)`` if anyone
# weirdly bracketed a sequence number.
_PATH_YEAR_FLOOR = 1930


def _extract_path_year(path: Path) -> int | None:
    """Walk the file's parent directories and return the first
    plausibly-year value found inside a ``(YYYY)`` / ``[YYYY]`` tag.

    Catches the common library layout where the volume year lives in
    the folder name but not the filename — ``/library/Wonder Woman
    (2011)/Wonder Woman 023.cbz``. Without this, files in that layout
    have no year hint anywhere and the matcher's year-proximity
    scoring falls back to neutral, which lets CV's relevance ranking
    surface the most popular volume of that title (usually a long-
    running old vol) for every modern issue.

    Walks from the immediate parent outward. The closest year-tagged
    folder wins, so a nested layout like ``/library/DC/Wonder Woman
    (2011)/...`` doesn't get fooled by a stray year in a higher
    ancestor. Returns ``None`` if no parent carries a parseable
    year, or if every captured value falls outside the publication-
    year range (filters out ``(0)`` / ``(1)`` sequence-marker
    parentheticals).
    """
    for parent in path.parents:
        if not parent.name:
            continue
        m = _PATH_YEAR_RE.search(parent.name)
        if m is None:
            continue
        try:
            year = int(m.group(1))
        except ValueError:  # pragma: no cover — regex restricts to digits
            continue
        if year >= _PATH_YEAR_FLOOR:
            return year
    return None


def _norm_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _norm_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm_issue_number(value) -> str | None:
    """ComicVine returns issue_number as a string (handles "1", "1.MU", ".5",
    "0", "½", "Annual 1"). comicfn2dict may return numeric types; we coerce
    to string and trim. Drops leading zeros so "001" matches CV's "1"."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # If it parses as a number, strip leading zeros while preserving decimal
    # form (so "001" → "1" but "1.5" stays "1.5" and ".5" stays ".5").
    try:
        as_float = float(s)
    except ValueError:
        return s  # non-numeric like "1.MU" or "Annual 1": pass through
    if as_float == int(as_float):
        return str(int(as_float))
    # Strip a single leading zero from "01.5" → "1.5".
    return s.lstrip("0") if s[:1] == "0" and "." in s else s
