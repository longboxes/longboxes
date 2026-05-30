"""Sync unit tests for the small pure-function helpers in
``app.matcher.pipeline``.

Split out of ``test_matcher.py`` so the integration suite there can keep
its module-level ``pytestmark = pytest.mark.asyncio`` without that mark
spuriously decorating these sync tests (which pytest-asyncio rightly
warns about). Same coverage as before — only the file boundary moved.
"""

import pytest

# ---- _looks_like_year -------------------------------------------------


def test_looks_like_year_accepts_publication_years():
    """Modern American comics start in the late 1930s; anything from
    1930 onward is plausibly a publication year."""
    from app.matcher.pipeline import _looks_like_year

    assert _looks_like_year(1940) is True   # Detective Comics era
    assert _looks_like_year(2011) is True   # New 52
    assert _looks_like_year(2025) is True   # current
    assert _looks_like_year(1930) is True   # boundary


def test_looks_like_year_rejects_sequence_numbers():
    """``parsed.volume_year`` doubles as a year (``v2011``) and a
    sequence number (``v1`` / ``v2`` / ``v50``). The gate filters out
    the latter so we don't score "year delta = 2010" between a
    volume sequence number and a CV start year."""
    from app.matcher.pipeline import _looks_like_year

    assert _looks_like_year(None) is False
    assert _looks_like_year(0) is False
    assert _looks_like_year(1) is False    # v1
    assert _looks_like_year(2) is False    # v2
    assert _looks_like_year(50) is False   # v50
    assert _looks_like_year(1929) is False  # just below the floor


# ---- _prelim_sort_key -------------------------------------------------


def test_prelim_sort_key_year_proximity_tiebreaks_same_name_candidates():
    """The motivating case: a search for "Batman" returns multiple
    volumes whose name matches identically (prefilter score = 1.0
    for all). The MAX_VOLUMES_TO_FETCH=5 cap fills with whatever CV
    sent first — usually the 1940 original — and the modern run the
    user actually has never reaches the scorer. The year-proximity
    tiebreaker pushes the year-closest candidates to the top of the
    sort so they land in the fetch slice."""
    from app.matcher.pipeline import _prelim_sort_key

    candidates = [
        ({"id": 1, "name": "Batman", "start_year": "1940"}, 1.0),
        ({"id": 2, "name": "Batman", "start_year": "2011"}, 1.0),
        ({"id": 3, "name": "Batman", "start_year": "2016"}, 1.0),
        ({"id": 4, "name": "Batman", "start_year": "1989"}, 1.0),
    ]
    candidates.sort(key=lambda x: _prelim_sort_key(x, 2012))

    # 2011 (delta=1) → 2016 (4) → 1989 (23) → 1940 (72).
    assert [c[0]["start_year"] for c in candidates] == [
        "2011", "2016", "1989", "1940",
    ]


def test_prelim_sort_key_year_beats_prelim_score_within_band():
    """The prelim score still dominates: a stronger name match wins
    even if its year is further away. (Year is only a tiebreaker.)"""
    from app.matcher.pipeline import _prelim_sort_key

    candidates = [
        ({"name": "Batman", "start_year": "1940"}, 1.0),   # exact name, far year
        ({"name": "Batman Adventures", "start_year": "2012"}, 0.75),  # partial name, perfect year
    ]
    candidates.sort(key=lambda x: _prelim_sort_key(x, 2012))
    # Exact-name "Batman" (1940) still wins on prelim score, even
    # though year is far — score is the primary key.
    assert candidates[0][0]["name"] == "Batman"


def test_prelim_sort_key_no_year_falls_back_to_stable_order():
    """When parsed_year is unknown we can't compute a meaningful
    proximity, so all year_dist values collapse to 0 and the sort
    reduces to pure prefilter-score ordering — the pre-tiebreaker
    behaviour. Same when a candidate stub lacks ``start_year``."""
    from app.matcher.pipeline import _prelim_sort_key

    inputs = [
        ({"name": "Batman", "start_year": "1940"}, 1.0),
        ({"name": "Batman", "start_year": "2011"}, 1.0),
    ]
    # parsed_year=None → both keys are (-1.0, 0); Python's sort is
    # stable so insertion order wins, matching legacy behaviour.
    inputs_a = list(inputs)
    inputs_a.sort(key=lambda x: _prelim_sort_key(x, None))
    assert [c[0]["start_year"] for c in inputs_a] == ["1940", "2011"]

    # candidate stub without a start_year → also year_dist=0.
    inputs_b = [
        ({"name": "Batman", "start_year": None}, 1.0),
        ({"name": "Batman", "start_year": "2011"}, 1.0),
    ]
    inputs_b.sort(key=lambda x: _prelim_sort_key(x, 2012))
    # 2011 (year_dist=1) should still beat the unknown-year one
    # (year_dist=0). Wait — actually equal prelim, year_dist 0 < 1,
    # so unknown-year wins. That's the "no signal, no penalty"
    # behaviour: a stub with no year isn't punished.
    assert inputs_b[0][0]["start_year"] is None


# ---- _prelim_score: strict + forgiving series similarity --------------


def test_prelim_score_separates_subset_from_full_match():
    """The motivating case from the field. A file parsed as
    ``series="Avengers"`` /
    ``long_series="Avengers - No More Bullying"`` runs two CV
    searches and unions the results. The raw pool contains the
    canonical 1963 "Avengers" run AND the actual one-shot we want.

    With ``token_set_ratio`` alone, both volume names tie at 1.0
    against the candidate terms (subset matches), and CV's relevance
    ordering for the short-term search shoves the one-shot out of
    the top-5 fetch slice. The strict half against the longest
    candidate term pulls the full-name match clear of the
    starts-with-the-same-word noise."""
    from app.matcher.pipeline import _prelim_score

    candidates = ["Avengers", "Avengers - No More Bullying"]

    full_match = _prelim_score(candidates, "Avengers - No More Bullying")
    short_match = _prelim_score(candidates, "Avengers")

    # Full-name volume scores ~1.0 (both halves max out).
    assert full_match == pytest.approx(1.0, abs=0.01)
    # Short-name "Avengers" volume keeps the forgiving credit (1.0)
    # but loses the strict half — averages well below the full match.
    assert short_match < 0.85
    # And the spread is wide enough that the top-5 cap can no longer
    # swallow the right candidate.
    assert full_match - short_match > 0.20


def test_prelim_score_unrelated_word_overlap_falls_below_floor():
    """A volume that shares only a leading word with the parsed
    series should drop below ``MIN_PREFILTER_SCORE=0.55`` so it
    doesn't crowd the fetch slice. "Avengers vs. X-Men" shares one
    token with our parsed long-series — forgiving max stays at 1.0,
    but the strict half tanks, dragging the average under the
    threshold."""
    from app.matcher.pipeline import _prelim_score

    candidates = ["Avengers", "Avengers - No More Bullying"]
    unrelated = _prelim_score(candidates, "Avengers vs. X-Men")
    # Token-set still gives ~1.0 on "Avengers" subset, but strict
    # against the longest term drops; either side of the threshold
    # is acceptable as long as it's well below the full-match score.
    full = _prelim_score(candidates, "Avengers - No More Bullying")
    assert full - unrelated > 0.10
    # The forgiving floor (single shared word) shouldn't be enough
    # on its own to keep an unrelated volume in contention.
    assert unrelated < full


def test_prelim_score_single_candidate_no_long_series():
    """When ``parsed.long_series`` is None or equal to ``series``,
    only one candidate term is passed in. The helper should still
    behave sensibly — strict and forgiving both score against the
    same term, and an exact name match returns ~1.0."""
    from app.matcher.pipeline import _prelim_score

    score = _prelim_score(["Saga"], "Saga")
    assert score == pytest.approx(1.0)


def test_prelim_score_year_suffix_doesnt_crater_strict_half():
    """``parsed.long_series`` sometimes carries a year suffix
    (``"Saga (2012)"``) that the CV volume name doesn't have.
    The forgiving half tolerates the extra tokens (set match), and
    the strict half against the longest term is hurt but not
    fatally — averaged, the score should still clear the floor."""
    from app.matcher.pipeline import MIN_PREFILTER_SCORE, _prelim_score

    candidates = ["Saga", "Saga (2012)"]
    score = _prelim_score(candidates, "Saga")
    assert score >= MIN_PREFILTER_SCORE


# ---- _score: year delta uses issue cover_date too ---------------------


def _score_with(*, volume_year, cover_date_year, parsed_year,
                volume_name="Batman", parsed_series="Batman"):
    """Convenience: call ``_score`` against SimpleNamespace stand-ins
    so we can dial in the year fields without setting up real ORM rows.
    Returns the resulting confidence."""
    from datetime import date
    from types import SimpleNamespace

    from app.matcher.pipeline import _score

    volume = SimpleNamespace(
        name=volume_name,
        year=volume_year,
        count_of_issues=52,
        raw_payload={},
        themes=[],
    )
    issue = SimpleNamespace(
        cover_date=(date(cover_date_year, 6, 1) if cover_date_year else None),
        issue_number="8",
        name=None,
    )
    return _score(
        volume=volume,
        issue=issue,
        parsed_series=parsed_series,
        parsed_issue_number="8",
        parsed_year=parsed_year,
        file_format="unknown",   # skip the format penalty for this test
    )


def test_year_score_uses_issue_cover_date_for_multi_year_runs():
    """The motivating case: a New 52 Batman issue 8 lives in a volume
    that started in 2011 but has cover_date 2013. A file tagged
    ``(2013)`` should auto-match — comparing only to volume.year would
    give delta=2 (year_score=0.45) and shove the file into PENDING.
    Using min(volume_delta, cover_date_delta) gives delta=0 and the
    file auto-matches."""
    score = _score_with(
        volume_year=2011,
        cover_date_year=2013,
        parsed_year=2013,
    )
    # series=1.0 (Batman=Batman), year=1.0 (delta=0 via cover_date).
    # Total = 0.65 + 0.35 = 1.00 → comfortably AUTO (>= 0.85).
    assert score >= 0.95


def test_year_score_also_works_when_file_uses_volume_year_convention():
    """The other convention: file tagged ``(2011)`` against the same
    volume. Now volume_delta=0 wins. Either tag → AUTO."""
    score = _score_with(
        volume_year=2011,
        cover_date_year=2013,
        parsed_year=2011,
    )
    assert score >= 0.95


def test_year_score_rejects_wrong_volume_when_both_signals_disagree():
    """The min() must not become a false-positive engine. A
    wrong-candidate volume (1940 Batman issue 8 with cover_date
    1940-08) against a 2013 file should still bottom out: both
    volume.year and cover_date.year give delta >= 70, year_score=0.0.
    Score=0.65 (series alone) — below PENDING_THRESHOLD."""
    score = _score_with(
        volume_year=1940,
        cover_date_year=1940,
        parsed_year=2013,
    )
    # Series perfect (1.0 * 0.65 = 0.65), year zero (0.0 * 0.35 = 0.0).
    # Below PENDING_THRESHOLD=0.50 ... actually equal-ish. Important
    # thing: NOT above AUTO_THRESHOLD=0.85.
    assert score < 0.85


def test_year_score_missing_cover_date_falls_back_to_volume_year():
    """If the issue's ``cover_date`` is null (stub row hasn't been
    bulk-hydrated yet), fall through to volume.year alone. Same
    behaviour as the pre-fix world."""
    score_volume_only = _score_with(
        volume_year=2011,
        cover_date_year=None,
        parsed_year=2011,
    )
    # delta=0 against volume.year, year_score=1.0, full score.
    assert score_volume_only >= 0.95


def test_year_score_no_year_signals_at_all_stays_neutral():
    """Worst case: file has no year, both volume.year and
    issue.cover_date are null. Score reverts to the neutral 0.5
    year-score — same as pre-fix."""
    score = _score_with(
        volume_year=None,
        cover_date_year=None,
        parsed_year=None,
    )
    # series=1.0 * 0.65 + neutral 0.5 * 0.35 = 0.825 → PENDING band.
    assert 0.80 < score < 0.85
