"""Tests for the ComicVine-description URL rewriter."""

import pytest

from app.services.cv_description import (
    rewrite_cv_description,
    rewrite_payload_descriptions,
)


# Each row: (raw description with one or more anchors, expected
# rewritten description). All seven internal types + an external
# (location, prefix 4020) type are exercised so the prefix table is
# end-to-end verified.
@pytest.mark.parametrize(
    "raw,expected",
    [
        # ---- Internal types — each prefix maps to its Longboxes route.
        # Volume (4050)
        (
            '<a href="/avengers-academy/4050-33633/">Avengers Academy</a>',
            '<a href="/volume/33633">Avengers Academy</a>',
        ),
        # Issue (4000)
        (
            '<a href="/avengers-1/4000-12345/">#1</a>',
            '<a href="/issue/12345">#1</a>',
        ),
        # Character (4005)
        (
            '<a href="/spider-man/4005-1442/">Spider-Man</a>',
            '<a href="/character/1442">Spider-Man</a>',
        ),
        # Publisher (4010)
        (
            '<a href="/marvel/4010-31/">Marvel</a>',
            '<a href="/publisher/31">Marvel</a>',
        ),
        # Person / creator (4040)
        (
            '<a href="/stan-lee/4040-40439/">Stan Lee</a>',
            '<a href="/creator/40439">Stan Lee</a>',
        ),
        # Story arc (4045)
        (
            '<a href="/civil-war/4045-55829/">Civil War</a>',
            '<a href="/arc/55829">Civil War</a>',
        ),
        # Team (4060)
        (
            '<a href="/avengers/4060-1933/">The Avengers</a>',
            '<a href="/team/1933">The Avengers</a>',
        ),
        # ---- External-typed prefix (locations = 4020) — absolutized to
        # comicvine.gamespot.com instead of routed into Longboxes.
        (
            '<a href="/new-york/4020-100/">New York</a>',
            '<a href="https://comicvine.gamespot.com/new-york/4020-100/">New York</a>',
        ),
        # ---- Untouched: hrefs that don't match the CV-relative shape.
        # Already-absolute URL.
        (
            '<a href="https://example.com/">elsewhere</a>',
            '<a href="https://example.com/">elsewhere</a>',
        ),
        # mailto:
        (
            '<a href="mailto:foo@bar.com">email</a>',
            '<a href="mailto:foo@bar.com">email</a>',
        ),
        # Bare anchor.
        ('<a href="#section">jump</a>', '<a href="#section">jump</a>'),
        # Protocol-relative — leading // not / so we don't match.
        (
            '<a href="//cdn.example.com/x/4050-1/">x</a>',
            '<a href="//cdn.example.com/x/4050-1/">x</a>',
        ),
        # ---- Multi-link single string: every match is rewritten.
        (
            'See <a href="/avengers/4060-1933/">Avengers</a> and '
            '<a href="/x-men/4060-1934/">X-Men</a>.',
            'See <a href="/team/1933">Avengers</a> and <a href="/team/1934">X-Men</a>.',
        ),
        # ---- Case-insensitive HREF and single-quoted attribute.
        (
            "<a HREF='/v/4050-1/'>v</a>",
            "<a HREF='/volume/1'>v</a>",
        ),
        # ---- Idempotent: rewritten output stays unchanged on a second pass.
        # The Longboxes routes have no -<digits> separator, so they don't
        # match the pattern.
        (
            '<a href="/volume/33633">Already rewritten</a>',
            '<a href="/volume/33633">Already rewritten</a>',
        ),
        # ---- Trailing slash optional.
        (
            '<a href="/x/4050-99">no trailing slash</a>',
            '<a href="/volume/99">no trailing slash</a>',
        ),
    ],
)
def test_rewrite_cv_description(raw, expected):
    assert rewrite_cv_description(raw) == expected


def test_rewrite_cv_description_none():
    """``None`` in -> ``None`` out (no crash, no empty string)."""
    assert rewrite_cv_description(None) is None


def test_rewrite_cv_description_empty():
    """Empty string preserved as-is."""
    assert rewrite_cv_description("") == ""


def test_rewrite_cv_description_idempotent():
    """Running the rewriter twice is a no-op on the second pass."""
    raw = '<a href="/x-men/4060-1934/">X-Men</a> and <a href="/new-york/4020-100/">New York</a>'
    once = rewrite_cv_description(raw)
    twice = rewrite_cv_description(once)
    assert once == twice


def test_rewrite_payload_descriptions_mutates_both_fields():
    """Helper applies rewriter to ``description`` and ``deck`` and
    leaves other fields alone."""
    payload = {
        "description": '<a href="/x/4050-1/">v</a>',
        "deck": '<a href="/y/4005-2/">c</a>',
        "name": "noop",
    }
    rewrite_payload_descriptions(payload)
    assert payload["description"] == '<a href="/volume/1">v</a>'
    assert payload["deck"] == '<a href="/character/2">c</a>'
    # Untouched.
    assert payload["name"] == "noop"


def test_rewrite_payload_descriptions_missing_fields_are_skipped():
    """Payload without description/deck doesn't crash and doesn't
    invent fields."""
    payload = {"name": "no descriptions here"}
    rewrite_payload_descriptions(payload)
    assert payload == {"name": "no descriptions here"}


def test_rewrite_payload_descriptions_handles_non_dict():
    """Drop-in safe — ``None`` and non-dict values pass through."""
    assert rewrite_payload_descriptions(None) is None
    assert rewrite_payload_descriptions("not a dict") == "not a dict"
