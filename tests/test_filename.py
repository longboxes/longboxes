"""Tests for the filename-parser wrapper around comicfn2dict.

These tests deliberately use realistic filenames pulled from common
naming conventions. The point isn't to test comicfn2dict (it has its
own tests) — it's to verify our normalisation: numeric coercion,
leading-zero stripping, decimal preservation, string types throughout.
"""

import pytest

from app.matcher.filename import parse_filename


@pytest.mark.parametrize(
    "name,expected_series,expected_issue,expected_year",
    [
        # Standard bracket-year format.
        ("Saga (2012) #001.cbz", "Saga", "1", 2012),
        ("The Wicked + The Divine (2014) #001.cbz", "The Wicked + The Divine", "1", 2014),
        # Without # prefix.
        ("Saga 001 (2012).cbz", "Saga", "1", 2012),
        # Three-digit issue with leading zeros.
        ("East of West (2013) #042.cbz", "East of West", "42", 2013),
    ],
)
def test_parse_common_conventions(name, expected_series, expected_issue, expected_year):
    parsed = parse_filename(name)
    assert parsed.series == expected_series
    assert parsed.issue_number == expected_issue
    assert parsed.year == expected_year


def test_parse_full_path_uses_basename_only():
    """Full paths should be reduced to basename before parsing."""
    parsed = parse_filename("/library/Image/Saga/Saga (2012) #001.cbz")
    assert parsed.series == "Saga"


def test_issue_number_is_always_string():
    parsed = parse_filename("Saga (2012) #001.cbz")
    assert isinstance(parsed.issue_number, str)


def test_unparseable_filename_returns_none_fields():
    """No raise — just empty fields."""
    parsed = parse_filename("totally-garbage-filename.cbz")
    # series may be set to the garbage; what we care about is "no raise."
    assert parsed.issue_number is None or isinstance(parsed.issue_number, str)


def test_raw_dict_is_preserved():
    parsed = parse_filename("Saga (2012) #001.cbz")
    assert isinstance(parsed.raw, dict)
    assert parsed.raw  # non-empty for a parseable name


# ---- long_series fallback ------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_long",
    [
        # The motivating case: comicfn2dict truncates at the hyphen
        # ("Avengers - No More Bullying" → "Avengers"). The
        # long_series helper recovers the full prefix.
        (
            "Avengers - No More Bullying #001 (2015).cbr",
            "Avengers - No More Bullying",
        ),
        # Same pattern without the explicit ``#`` issue marker — the
        # bare-number-at-end form catches this after the year is
        # stripped from the lookahead context.
        (
            "Avengers - Heroes Welcome 001 (2013).cbz",
            "Avengers - Heroes Welcome",
        ),
        # Bare number followed by a trailing scanlator / group tag.
        # The trailing ``( )`` / ``[ ]`` groups are stripped so the
        # number lands at the end and the bare-number lookup fires —
        # without that the helper used to fall back to comicfn2dict's
        # hyphen-truncated "Marvel".
        (
            "Marvel - Now What 001 (2013) (digital-Empire).cbr",
            "Marvel - Now What",
        ),
        # Same long form even without an explicit year tag.
        ("Avengers - No More Bullying #001.cbz", "Avengers - No More Bullying"),
        # Single-word series with a hyphen inside (X-Men). No subtitle,
        # so the long form is identical to the parsed series.
        ("X-Men #1 (1991).cbz", "X-Men"),
        # No hyphen at all — long form equals parsed series.
        ("Saga (2012) #001.cbz", "Saga"),
    ],
)
def test_long_series_recovers_subtitle(name, expected_long):
    """``long_series`` captures the full series-plus-subtitle prefix
    even when comicfn2dict truncates at the hyphen."""
    parsed = parse_filename(name)
    assert parsed.long_series == expected_long


def test_long_series_falls_back_to_series_when_extraction_fails():
    """When no usable issue marker is in the filename, the helper
    returns None and the dataclass falls back to ``series``. The
    field is never None when ``series`` is populated, so callers
    don't have to do their own fallback."""
    parsed = parse_filename("totally-garbage-filename.cbz")
    # comicfn2dict may or may not produce a series here; either way,
    # long_series shouldn't be more "missing" than series itself.
    if parsed.series is not None:
        assert parsed.long_series is not None


# ---- path_year fallback -------------------------------------------------


def test_path_year_extracts_parent_folder_year():
    """The Mylar / Komga / Kapowarr layout: folder carries the volume
    year, the filename omits it. Without this fallback the matcher's
    year-proximity tiebreaker neutralises and CV's relevance ranking
    surfaces the most popular volume of a series for every modern
    issue (the Wonder Woman 1987 vol 2 case). With it, files in
    year-tagged folders get a usable year signal."""
    parsed = parse_filename("/library/Wonder Woman (2011)/Wonder Woman 023.cbz")
    # Filename itself has no year — the basename parser returns None.
    assert parsed.year is None
    # Folder tag picked up.
    assert parsed.path_year == 2011


def test_path_year_with_square_brackets():
    """Some layouts use ``[YYYY]`` instead of ``(YYYY)``."""
    parsed = parse_filename("/library/Saga [2012]/Saga 001.cbz")
    assert parsed.year is None
    assert parsed.path_year == 2012


def test_path_year_does_not_run_when_filename_has_year():
    """If the basename already carries a year, the path walk is
    skipped — the filename's own year is more specific (it's about
    *this* issue) than a folder tag (which describes the volume).
    Skipping keeps the parser cheap for the common case."""
    parsed = parse_filename("/library/Wonder Woman (2011)/Wonder Woman 023 (2013).cbz")
    assert parsed.year == 2013
    # path_year MAY be left None when the filename year wins — the
    # docstring says it's only populated as a fallback.
    assert parsed.path_year is None


def test_path_year_picks_closest_year_tagged_ancestor():
    """A nested layout like ``DC/Wonder Woman (2011)/...`` shouldn't
    return a stray year from a higher ancestor. The closest
    year-tagged folder to the file wins, and the walk stops at the
    first hit."""
    parsed = parse_filename("/library (2020)/Wonder Woman (2011)/Wonder Woman 023.cbz")
    # The (2011) folder is the file's direct parent — that's the one
    # we want, not the (2020) ancestor that's just where the library
    # happened to be planted.
    assert parsed.path_year == 2011


def test_path_year_ignores_implausible_year_values():
    """``(0)``, ``(1)``, ``(99)`` aren't publication years; they're
    usually some volume- or part-marker convention. The regex caps
    at 4 digits which already excludes those, but the
    ``_PATH_YEAR_FLOOR`` guard catches the corner case of a
    bracketed 4-digit number that's clearly not a year (``(0001)``,
    ``(0042)`` etc.)."""
    parsed = parse_filename("/library/Some Series (0042)/Some Series 001.cbz")
    # 42 is well below the 1930 floor; should be ignored.
    assert parsed.path_year is None


def test_path_year_none_when_no_folder_year_present():
    """A plain layout with no year tags anywhere produces ``None``
    on path_year — matching the v1 behaviour for callers that pass
    a bare filename string (no parents to walk)."""
    parsed = parse_filename("/library/Some Series/Some Series 001.cbz")
    assert parsed.path_year is None


def test_path_year_none_for_bare_filename_string():
    """A bare basename with no path has no parents to walk; path_year
    stays None. The pre-existing tests that pass bare filenames
    (e.g. ``test_parse_common_conventions``) keep their behaviour."""
    parsed = parse_filename("Saga 001.cbz")
    assert parsed.path_year is None
