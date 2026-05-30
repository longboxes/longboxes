"""Tests for the small CV-payload utility functions."""

from types import SimpleNamespace

import pytest

from app.services.cv_helpers import (
    classify_cv_volume,
    classify_volume_format,
    parse_arc_name,
    sort_key_issue_number,
    volume_status_from_themes,
    volume_type_from_themes,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Canonical CV "<book>" <arc> form — what this parser exists for.
        ('"Avengers" Disassembled', ("Avengers", "Disassembled")),
        # Multi-word book + multi-word arc.
        ('"New Avengers" Civil War', ("New Avengers", "Civil War")),
        # Leading/trailing whitespace tolerated.
        ('  "Avengers" Disassembled  ', ("Avengers", "Disassembled")),
        # No quoted prefix → returns the trimmed input unchanged.
        ("Civil War", (None, "Civil War")),
        ("  Civil War  ", (None, "Civil War")),
        # Quote-only input (no following arc name) — conservative: don't
        # split, return as-is. CV shouldn't emit this but we shouldn't
        # crash on it either.
        ('"Avengers"', (None, '"Avengers"')),
        # Quoted text mid-string — not the documented convention, so
        # we leave it alone.
        ('Some "Quoted" Middle', (None, 'Some "Quoted" Middle')),
        # Empty / None inputs.
        ("", (None, "")),
        (None, (None, "")),
    ],
)
def test_parse_arc_name(raw, expected):
    assert parse_arc_name(raw) == expected


@pytest.mark.parametrize(
    "count,name,deck,description,first_issue,expected",
    [
        # The motivating case: Vagabond is an ongoing VIZ series whose
        # CV description mentions collected editions. The issue count
        # wins — keywords must NOT relabel a multi-issue series.
        (
            37,
            "Vagabond",
            None,
            "Collected editions of Takehiko Inoue's manga.",
            None,
            "ongoing",
        ),
        # Plain ongoing — no keywords, high count.
        (60, "Saga", None, None, None, "ongoing"),
        # Limited series: 2..11 issues. A collection keyword in the
        # description does NOT override a multi-issue count.
        (6, "Some Mini", None, "Collects nothing relevant", None, "limited"),
        (2, "Two Parter", None, None, None, "limited"),
        # Lone-issue volume, no keyword → one-shot (the default).
        (1, "A Single Issue Thing", None, None, None, "one_shot"),
        # Lone-issue volume with a collection marker → collection.
        # This is the ONLY situation a keyword changes the answer.
        (1, "Batman Omnibus", None, "Collects Batman #1-50.", None, "collection"),
        (1, "Saga Deluxe Edition", None, None, None, "collection"),
        # Lone-issue volume explicitly flagged a one-shot → one_shot;
        # an explicit one-shot marker also beats a collection marker.
        (1, "Halloween Special", "A one-shot special", None, None, "one_shot"),
        (1, "Weird One", None, "A one-shot that collects nothing", None, "one_shot"),
        # String counts (CV sometimes returns them) are coerced.
        ("1", "Stringy Omnibus", None, "Collects everything", None, "collection"),
        # No count — an un-hydrated stub — is unknown even with a
        # keyword present; absence of a count is never read as a type.
        (None, "Stub Omnibus", None, "Collects everything", None, "unknown"),
        # Degenerate zero count → unknown, not a guess.
        (0, "Empty", None, None, None, "unknown"),
    ],
)
def test_classify_volume_format(count, name, deck, description, first_issue, expected):
    assert (
        classify_volume_format(
            name=name,
            count_of_issues=count,
            deck=deck,
            description=description,
            first_issue_name=first_issue,
        )
        == expected
    )


def _themes(*id_name_pairs):
    """Build a stored themes array from ``(id, name)`` pairs."""
    return [{"id": i, "name": n} for i, n in id_name_pairs]


def test_volume_status_from_themes():
    assert volume_status_from_themes(_themes((61, "Ongoing"))) == "ongoing"
    assert volume_status_from_themes(_themes((52, "Complete"))) == "complete"
    assert volume_status_from_themes(_themes((51, "Cancelled"))) == "cancelled"
    assert volume_status_from_themes(_themes((66, "Unfinished"))) == "unfinished"
    # CV tagging is messy — when both are present the most terminal
    # status wins (a volume tagged Ongoing + Complete reads complete).
    assert volume_status_from_themes(_themes((61, "Ongoing"), (52, "Complete"))) == "complete"
    # No status theme / empty / None.
    assert volume_status_from_themes(_themes((2, "Action"))) is None
    assert volume_status_from_themes([]) is None
    assert volume_status_from_themes(None) is None


def test_volume_type_from_themes():
    assert volume_type_from_themes(_themes((60, "One Shot"))) == "one_shot"
    assert volume_type_from_themes(_themes((58, "Mini-Series"))) == "limited"
    assert volume_type_from_themes(_themes((57, "Maxi-Series"))) == "limited"
    assert volume_type_from_themes(_themes((12, "Collection"))) == "collection"
    assert volume_type_from_themes(_themes((61, "Ongoing"))) == "ongoing"
    # A genre-only theme set yields no type.
    assert volume_type_from_themes(_themes((14, "Crime"))) is None
    assert volume_type_from_themes(None) is None


def test_sort_key_issue_number_natural_order():
    """Issue numbers sort by numeric magnitude, not lexically — so a run
    reads 1, 2, … 10, not 1, 10, 2. Numeric-suffixed values (#1.MU) sort
    just after their base number; fully non-numeric labels go after all
    numerics; None / blank sort last."""
    shuffled = ["10", "2", "1", "0", "1.MU", "Annual 1", None, ""]
    ordered = sorted(shuffled, key=sort_key_issue_number)
    assert ordered == ["0", "1", "1.MU", "2", "10", "Annual 1", None, ""]
    # The key is a tuple, so it's a stable, comparable sort key.
    assert sort_key_issue_number("1")[0] == 0  # numeric bucket
    assert sort_key_issue_number("Annual 1")[0] == 1  # non-numeric bucket
    assert sort_key_issue_number(None)[0] == 2  # nulls-last bucket


def test_classify_cv_volume_prefers_scraped_theme_type():
    # A lone-issue volume is heuristically a one-shot...
    base = dict(name="Thing", count_of_issues=1, raw_payload={})
    heuristic = SimpleNamespace(**base, themes=_themes((14, "Crime")))
    assert classify_cv_volume(heuristic) == "one_shot"
    # ...but a scraped "Ongoing" theme is authoritative and overrides it.
    scraped = SimpleNamespace(**base, themes=_themes((61, "Ongoing")))
    assert classify_cv_volume(scraped) == "ongoing"
    # No themes attribute at all → still falls back to the heuristic.
    assert classify_cv_volume(SimpleNamespace(**base)) == "one_shot"
