"""Four-stage matcher pipeline per §10 of the design doc.

Stage 1 — ComicInfo CV ID fast path.
    If the file's ComicInfo.xml had a parseable CV ID in its ``<Web>`` field,
    verify the issue exists in the CV cache (fetching via the cache layer if
    needed). Hit → write file_matches with status=auto, confidence=1.000,
    source=comicinfo_cvid. Miss (CV returned 404) → fall through to Stage 2,
    treating the CV ID as an unreliable hint.

Stage 2 — Filename parsing.
    Parse the filename via ``parse_filename``. If no series name comes out,
    we can't search — write unmatched and stop.

Stage 3 — Candidate generation + scoring.
    Search ComicVine for volumes matching the parsed series name (cached).
    For each candidate volume, load its issues from cv_issues (a volume
    fetch via the cache populates stubs if not yet present). Score each
    issue with rapidfuzz series similarity + year proximity + the
    count_of_issues sanity check. Issue-number exact match is a gate.

Stage 4 — Decision.
    Top score ≥ 0.85 → auto, < 0.50 → unmatched, anything in between →
    pending with the top-N candidates serialised into file_matches.candidates
    for the review UI.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from rapidfuzz import fuzz
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.archives import open_archive, parse_comicinfo, parse_metroninfo
from app.archives.base import ArchiveError, UnsupportedArchiveError
from app.archives.comicinfo import ComicInfoExtract
from app.comicvine import ComicVineCache
from app.comicvine.errors import (
    ComicVineError,
    ComicVineNotFoundError,
    ComicVineRateLimitError,
)
from app.matcher.filename import ParsedFilename, parse_filename
from app.models import (
    ComicInfoStatus,
    CvIssue,
    CvVolume,
    File,
    FileLocation,
    FileMatch,
    MatchSource,
    MatchStatus,
)
from app.services.cv_helpers import classify_cv_volume, classify_file_format, safe_int
from app.services.settings import get_archive_backend

logger = logging.getLogger("longboxes.matcher")

# --- Tuning knobs (hardcoded for v1; see design §10 / phase-4 questions) ---

AUTO_THRESHOLD = 0.85
PENDING_THRESHOLD = 0.50
TOP_N_CANDIDATES = 5

# Search returns up to this many volumes per query. CV's per-page
# limit is 100; 50 covers the long tail of popular titles that have
# 30+ same-named volumes (Wonder Woman, Batman, Action Comics,
# Detective Comics, Spider-Man — every major DC / Marvel franchise
# restart counts as a separate volume, plus Annuals, Specials,
# one-shots, and crossover limited series). The pool feeds the
# prefilter — only ``MAX_VOLUMES_TO_FETCH`` are actually fetched as
# full volumes, so a wider pool is mostly free.
SEARCH_RESULT_LIMIT = 50

# Pre-filter search results by series-name fuzzy similarity BEFORE the
# expensive /volume/X/ fetch. Anything below this is dropped on the basis
# of the search-response name alone.
MIN_PREFILTER_SCORE = 0.55

# After pre-filtering, cap full-volume fetches per file. A legitimate
# match almost always sits in the top 5 by name similarity; fetching more
# burns CV rate budget for no signal.
MAX_VOLUMES_TO_FETCH = 5

# Scoring weights (must sum to 1.0 to keep the score in [0, 1]).
WEIGHT_SERIES = 0.65
WEIGHT_YEAR = 0.35

# Multiplier applied to a candidate's score when the file's format
# (single issue vs collected edition) disagrees with the candidate
# volume's classified format. A near-disqualifying penalty — a
# format-mismatched candidate can't reach the PENDING band, let alone
# AUTO. Skipped when the file's format is "unknown" (no page count).
FORMAT_MISMATCH_FACTOR = 0.1

# Year-proximity scoring: integer year delta → multiplier.
_YEAR_PROXIMITY = {0: 1.0, 1: 0.85, 2: 0.45, 3: 0.15}
_YEAR_MISSING_NEUTRAL = 0.5  # neither penalty nor full credit

# Floor for "is this number plausibly a publication year?" — used by
# the ``parsed.volume_year`` fallback to filter out sequence numbers
# (``v1``, ``v2``, ``v50``) that comicfn2dict surfaces in the same
# field as bona-fide years (``v2011``). Modern American comics start
# in the late 1930s; 1930 is a safe floor and well clear of any
# realistic volume-sequence number.
_YEAR_FLOOR = 1930


def _looks_like_year(value: int | None) -> bool:
    """True when ``value`` is plausibly a four-digit publication year.

    Used to gate the ``parsed.volume_year`` fallback so a ``v1`` /
    ``v2`` sequence number doesn't get fed into the year-proximity
    scoring as a "year" of 1 or 2.
    """
    return value is not None and value >= _YEAR_FLOOR


def _prelim_score(series_candidates: list[str], name: str) -> float:
    """Combined strict + forgiving series-name similarity in [0, 1].

    ``token_set_ratio`` (the forgiving metric) returns 1.0 whenever
    one string's tokens are a subset of the other's. For a file
    parsed as ``series="Avengers"`` /
    ``long_series="Avengers - No More Bullying"``, both the canonical
    "Avengers" (1963) volume and the "Avengers - No More Bullying"
    one-shot score 1.0 on either candidate term. That tie puts the
    canonical run ahead of the actual one-shot in CV's relevance
    ordering, and the ``MAX_VOLUMES_TO_FETCH`` cap drops the right
    candidate before it's ever fetched / scored.

    The fix pairs the forgiving signal with a *strict* length-aware
    one: ``token_sort_ratio`` against the longest candidate term.
    ``token_sort_ratio`` is Levenshtein-based, so the short
    "Avengers" volume only scores ~0.48 against the longest term
    "Avengers - No More Bullying" while the full-name volume scores
    1.00. Averaging the two:

      | Volume                        | strict | forgiving | avg  |
      |-------------------------------|--------|-----------|------|
      | "Avengers - No More Bullying" |  1.00  |   1.00    | 1.00 |
      | "Avengers" (1963)             |  0.48  |   1.00    | 0.74 |

    The full match dominates without depending on a parsed year
    (which many filenames don't have). The forgiving half still
    carries the "subset is fine" credit so a volume that misses
    incidental words (``"Vol 1"``, ``"(2015)"`` baked into the
    parsed series) isn't punished out of contention.
    """
    longest = max(series_candidates, key=len)
    strict = fuzz.token_sort_ratio(longest, name) / 100.0
    forgiving = max(
        fuzz.token_set_ratio(t, name) / 100.0 for t in series_candidates
    )
    return (strict + forgiving) / 2.0


def _prelim_sort_key(
    item: tuple[dict, float], parsed_year: int | None
) -> tuple[float, int]:
    """Sort key for the Stage 3 prefilter.

    Primary: prefilter score (descending — so we negate for ``sort``'s
    natural ascending order).
    Secondary: distance between the candidate volume's ``start_year``
    and the file's parsed year (ascending — closer wins).

    The tiebreaker matters for popular titles where every candidate
    matches the series name perfectly: Batman, Wonder Woman, Detective
    Comics. Without it, the MAX_VOLUMES_TO_FETCH cap fills with
    whatever CV returned first (which tends to be the canonical
    long-running original), so the modern run the user actually has
    never gets scored. With it, the candidates closest in time to the
    file's year bubble up into the fetch slice.

    Falls back to neutral (year_dist=0) when either side has no year —
    that preserves stable ordering on CV's relevance ranking, which is
    the original v1 behaviour.
    """
    vol_stub, prelim_score = item
    stub_year = safe_int(vol_stub.get("start_year"))
    if parsed_year is None or stub_year is None:
        year_dist = 0
    else:
        year_dist = abs(parsed_year - stub_year)
    return (-prelim_score, year_dist)


@dataclass
class MatchResult:
    """What the matcher decided. Mostly for tests and the job's return value."""

    status: MatchStatus
    source: MatchSource
    issue_cv_id: int | None
    confidence: float
    candidates: list[dict]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<MatchResult {self.status} source={self.source} "
            f"issue_cv_id={self.issue_cv_id} conf={self.confidence:.3f}>"
        )


# ---- Public entry point -------------------------------------------------


async def match_file(
    file_id: uuid.UUID,
    db: AsyncSession,
    cv_cache: ComicVineCache,
) -> MatchResult:
    """Run the four-stage pipeline for one file. Writes a file_matches row.

    Re-reads the file's ComicInfo.xml on the fly — we don't persist the
    parsed ComicInfo to the DB (only the coverage status). For most files
    this is a single zipfile open + tiny XML parse, well under a millisecond.
    """
    file_row = await db.get(File, file_id)
    if file_row is None:
        raise ValueError(f"file_id {file_id} not found")

    # Phase 11: a file resolved to a local entry or a supplement is a
    # settled, user-authored decision. The matcher must never overwrite
    # it — a re-scan, a "Match all" pass, or a match job that raced the
    # reviewer's action would otherwise clobber hand-entered metadata
    # (``_persist`` only touches the CV-issue target, so a re-run would
    # also leave the row in a broken half-state). Return the existing
    # row's state untouched; do NOT _persist.
    existing_match = await db.get(FileMatch, file_id)
    if existing_match is not None and existing_match.status in (
        MatchStatus.LOCAL.value,
        MatchStatus.SUPPLEMENT.value,
    ):
        return MatchResult(
            status=MatchStatus(existing_match.status),
            source=MatchSource(existing_match.source),
            issue_cv_id=existing_match.issue_cv_id,
            confidence=(
                float(existing_match.confidence)
                if existing_match.confidence is not None
                else 0.0
            ),
            candidates=existing_match.candidates or [],
        )

    if file_row.excluded_from_matching:
        # The scanner already skips these, but be defensive: if a job is
        # somehow enqueued (e.g. admin-triggered) for an excluded file, we
        # still don't want to write a match row.
        return MatchResult(
            status=MatchStatus.UNMATCHED,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=0.0,
            candidates=[],
        )

    # We need a path to open the archive. Pick the first current location.
    location = await _first_current_location(db, file_id)
    if location is None:
        # File has no current location (all marked missing). Mark unmatched.
        result = MatchResult(
            status=MatchStatus.UNMATCHED,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=0.0,
            candidates=[],
        )
        await _persist(db, file_id, result)
        return result

    # Archive backend setting decides which reader (comicbox vs stdlib)
    # parses the file. Fetched per-call rather than per-process so admin
    # changes take effect on the next match job without a restart.
    archive_backend = await get_archive_backend(db)
    comicinfo = await _load_comicinfo(location.path, archive_backend)

    # Stage 1
    if (
        file_row.comicinfo_status == ComicInfoStatus.FULL_WITH_CVID
        and comicinfo is not None
        and comicinfo.cv_issue_id is not None
    ):
        result = await _stage_1(db, cv_cache, comicinfo)
        if result is not None:
            await _persist(db, file_id, result)
            await _notify_winner(db, cv_cache, result)
            return result
        # Stage 1 hit a CV 404 — fall through.

    # Stages 2 + 3 + 4. The file's format guess (single issue vs
    # collected edition, from its page count) feeds the candidate
    # scoring — a format mismatch is a near-disqualifying penalty.
    file_format = classify_file_format(file_row.page_count)
    parsed = parse_filename(location.path)
    result = await _stage_2_through_4(
        db, cv_cache, parsed, comicinfo, file_format
    )
    await _persist(db, file_id, result)
    await _notify_winner(db, cv_cache, result)
    return result


async def _notify_winner(
    db: AsyncSession, cv_cache: ComicVineCache, result: MatchResult
) -> None:
    """Tell the cache layer about the matcher's winning volume so the
    cheap one-call bulk hydration gets enqueued.

    Lever 2: ``_upsert_volume`` used
    to enqueue ``volume_issues`` on every first-touch, including for
    Stage 3 candidates the matcher then rejected. Moving the enqueue
    here means only winners spawn hydration. Only AUTO results count
    — PENDING / UNMATCHED have no confirmed winner to hydrate, and
    CONFIRMED comes from the human confirm path, which owns its own
    enqueue.

    **Already-hydrated guard.** The first observed throughput run
    showed ~156 files/hr, dead-on the bulk-gate pacer ceiling: every
    successful match was re-enqueuing ``volume_issues`` for the
    winner's volume, and the deterministic-job-id dedupe's
    "terminal job → fresh enqueue" path was making every file in a
    volume trigger a fresh bulk run. For a 10-file volume that's
    ~10 bulk-gate tokens burned re-hydrating data we already have.
    The fix: before forwarding to the cache, ask the DB whether any
    ``cv_issues`` row for this volume already carries the
    ``_bulk_hydrated: True`` marker that ``hydrate_volume_issues``
    writes. If yes, the bulk job has already run successfully —
    skip the enqueue. The check is one indexed JSONB query per
    successful match (a few ms); the saving is one paced CV call
    per subsequent file of a known volume.

    The volume's cv_id comes off the ``cv_issues`` row that was just
    written or read in either stage. ``db.get`` is a PK lookup that
    will usually be served from the session identity map at zero SQL
    cost. ``notify_match_winner`` itself is also idempotent through
    the deterministic job id, so the guard here is the belt and the
    in-flight dedupe is the suspenders."""
    if result.status != MatchStatus.AUTO or result.issue_cv_id is None:
        return
    issue_row = await db.get(CvIssue, result.issue_cv_id)
    if issue_row is None or issue_row.volume_cv_id is None:
        return
    # ``raw_payload ->> '_bulk_hydrated' = 'true'`` matches the
    # marker ``hydrate_volume_issues`` writes on every row it
    # bulk-fills. A single row with the marker is enough — that
    # confirms the bulk job ran successfully for this volume at
    # least once. Re-running it would only refresh data we'd just
    # write back identically.
    already_bulk_hydrated = await db.scalar(
        select(CvIssue.cv_id)
        .where(CvIssue.volume_cv_id == issue_row.volume_cv_id)
        .where(
            text("cv_issues.raw_payload ->> '_bulk_hydrated' = 'true'")
        )
        .limit(1)
    )
    if already_bulk_hydrated is not None:
        return
    cv_cache.notify_match_winner(issue_row.volume_cv_id)


# ---- Stage 1 ------------------------------------------------------------


async def _stage_1(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    comicinfo: ComicInfoExtract,
) -> MatchResult | None:
    """Verify the ComicInfo CV ID points at a real issue. None on miss.

    **DB short-circuit (Lever 1 from the hyperspeed plan).** Before
    burning an ``/issue/<id>/`` call on CV's tight issue-gate, look the
    issue up in our local ``cv_issues`` table. If a row exists in *any*
    state — true stub from a volume-walk, ``_bulk_hydrated`` from a
    bulk fill, or fully hydrated — and it carries a populated
    ``volume_cv_id``, that's enough to call this match valid: CV
    already told us once that the issue exists (otherwise no row
    would be there), and we already know its parent volume.

    On a fresh library this saves the lion's share of an initial-run
    bill: ~14k CV-ID-bearing files would each have paid one issue-gate
    call, and the issue gate is the slowest of CV's rate-limited
    resources (~180/hr). After a few volumes are matched, almost
    every subsequent file lands as a cache hit here and pays zero CV
    calls.

    The fallback path (no row, or a row missing ``volume_cv_id``)
    keeps the old behaviour exactly — ``cv_cache.get_issue`` does the
    fetch, and the ``ComicVineNotFoundError`` / network-error
    branches are unchanged. So this is purely a fast path; the slow
    path's error semantics aren't touched."""
    cv_issue_id = comicinfo.cv_issue_id
    assert cv_issue_id is not None

    # Fast path — see the docstring. ``db.get`` is a PK lookup; if
    # something else in the same session has already loaded this row,
    # this is an identity-map hit with no SQL at all.
    cached_issue = await db.get(CvIssue, cv_issue_id)
    if cached_issue is not None and cached_issue.volume_cv_id is not None:
        issue_volume_cv_id: int | None = cached_issue.volume_cv_id
    else:
        # Slow path — never seen this issue (or never resolved its
        # parent volume). Hit CV and let the cache layer upsert the
        # row so the next file in this series gets the fast path.
        try:
            issue = await cv_cache.get_issue(db, cv_issue_id)
        except ComicVineNotFoundError:
            logger.info(
                "Stage 1: CV ID %d from ComicInfo doesn't exist on CV; "
                "falling through to filename parse",
                cv_issue_id,
            )
            return None
        except ComicVineRateLimitError:
            # Rate limited — propagate so the match job pauses and
            # re-enqueues this file rather than marking it unmatched.
            # A re-run will get the real answer once the cool-down
            # passes.
            raise
        except ComicVineError as e:
            # Network error / unknown CV failure. Don't downgrade to
            # Stage 2; surface as unmatched (the review UI lets the
            # user retry).
            logger.warning("Stage 1: CV error for ID %d: %s", cv_issue_id, e)
            return None
        issue_volume_cv_id = issue.volume_cv_id

    # Eagerly hydrate the parent volume. ``_upsert_issue`` only wrote a stub
    # cv_volumes row (FK placeholder, no cover / count_of_issues / nested
    # issue list). Library browse needs the full record to render anything
    # useful, and volumes are low-traffic + high-value, so one extra CV
    # call per *new* volume on match is the right trade — subsequent files
    # in the same volume hit the cache and pay nothing. Failure here is
    # non-fatal: the match is still good; the volume just stays a stub
    # until the library route's safety-net hydration catches it later.
    if issue_volume_cv_id is not None:
        try:
            await cv_cache.get_volume(db, issue_volume_cv_id)
        except ComicVineError as e:
            logger.warning(
                "Stage 1: eager volume hydration failed for %d: %s; "
                "leaving as stub",
                issue_volume_cv_id,
                e,
            )

    return MatchResult(
        status=MatchStatus.AUTO,
        source=MatchSource.COMICINFO_CVID,
        # Use the input cv_issue_id directly — by here we've either
        # confirmed it via the DB short-circuit or via the cache
        # fetch, and the matcher row is keyed on this value
        # regardless of which path we took.
        issue_cv_id=cv_issue_id,
        confidence=1.0,
        candidates=[],
    )


# ---- Stages 2 + 3 + 4 ---------------------------------------------------


async def _stage_2_through_4(
    db: AsyncSession,
    cv_cache: ComicVineCache,
    parsed: ParsedFilename,
    comicinfo: ComicInfoExtract | None,
    file_format: str = "unknown",
) -> MatchResult:
    """Search → score → decide.

    ``file_format`` is the file's single-issue vs collected-edition
    guess (see ``classify_file_format``); it's threaded into ``_score``
    so a format-mismatched candidate is penalised hard."""
    # Prefer ComicInfo's series/issue_number/year over the parsed-filename
    # values where available — ComicInfo is authoritative when it exists,
    # even when it lacks a usable CV ID. This is the "partial ComicInfo
    # boost" referenced in §9 of the design doc.
    series = (comicinfo.series if comicinfo else None) or parsed.series
    issue_number = (comicinfo.number if comicinfo else None) or parsed.issue_number
    # Year fallback chain, in order of trustworthiness:
    #   1. ComicInfo ``Year`` — explicit metadata, most reliable.
    #   2. ``(YYYY)`` parsed from the filename — the convention.
    #   3. ``(YYYY)`` from a parent directory tag — the folder-level
    #      convention used by Mylar / Komga / Kapowarr layouts
    #      (``/library/Wonder Woman (2011)/Wonder Woman 023.cbz``).
    #      Without this, NONE-tier files in that layout had no year
    #      signal and the prefilter's year-distance tiebreaker
    #      neutralised, letting CV's relevance ranking surface
    #      popular old volumes for every modern issue.
    #   4. ``v{YYYY}`` parsed as ``volume_year`` — gated on
    #      ``_looks_like_year`` so a ``v1`` / ``v2`` sequence number
    #      doesn't get treated as a publication year.
    # The matcher's year-proximity scoring + prefilter tiebreaker both
    # depend on this being populated whenever the filename hints at a
    # year in *any* form — without the volume_year fallback, common
    # titles whose filenames use the ``v{YYYY}`` convention had no
    # year signal and the matcher fell back on series-only scoring.
    year = (
        (comicinfo.year if comicinfo else None)
        or parsed.year
        or parsed.path_year
        or (parsed.volume_year if _looks_like_year(parsed.volume_year) else None)
    )

    if not series:
        # No usable input — nothing to search on.
        return MatchResult(
            status=MatchStatus.UNMATCHED,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=0.0,
            candidates=[],
        )

    # Build the set of series strings to search CV with. comicfn2dict's
    # ``series`` field truncates at hyphens (``"Avengers - No More
    # Bullying"`` → ``"Avengers"``), which floods the search with
    # unrelated hits for the leading word. ``parsed.long_series``
    # captures the prefix in full when one's available; we run BOTH
    # searches and union the results so the right candidate can
    # surface from either path. Scoring later in the pipeline takes
    # ``max()`` across candidates, so a long-series winner isn't
    # penalised for failing the short-series prelim and vice versa.
    #
    # ``parsed.long_series`` is only ever a *longer form of
    # ``parsed.series``* — it's the same parse, just with the hyphen-
    # truncated prefix recovered. So it's only safely added as a
    # candidate when the working ``series`` came from the parsed
    # filename in the first place. When ComicInfo overrode the working
    # series (gibberish-filename + ComicInfo-with-series case), the
    # parsed long-series is unrelated noise and shouldn't anchor the
    # prefilter's strict length-aware metric (see ``_prelim_score``) —
    # doing so tanks an otherwise-perfect ComicInfo match.
    comicinfo_series = (comicinfo.series if comicinfo else None) or None
    using_parsed_series = not comicinfo_series
    series_candidates: list[str] = [series]
    if (
        using_parsed_series
        and parsed.long_series
        and parsed.long_series != series
        and parsed.long_series.lower() != series.lower()
    ):
        series_candidates.append(parsed.long_series)

    # Stage 3a: search CV for matching volumes — one query per
    # candidate series string. Errors on either term skip that
    # term but don't abort the whole match; if every term fails
    # we still return UNMATCHED below via the empty-results path.
    #
    # We use CV's ``/search/?resources=volume`` endpoint (via
    # ``cv_cache.search``) rather than the literal substring filter
    # ``/volumes/?filter=name:`` (``cv_cache.search_volumes``). The
    # two endpoints have *very* different ranking. For a popular
    # title like Wonder Woman the substring filter buries the New 52
    # (2011) vol past position 10 — the matcher's old top-N pool
    # never even considered it, and our prefilter year-distance
    # tiebreaker had no way to rescue a candidate that wasn't in the
    # pool. The same query against ``/search/`` returns the 2011 vol
    # at position 6 because CV's real search engine reasons about
    # relevance rather than alphabetical / popularity-of-the-name
    # order. The manual ``/review/volume-search`` page uses
    # ``/search/`` too, so this aligns the auto-matcher with what
    # the user sees when they search by hand.
    raw_results: list[dict] = []
    seen_volume_ids: set[int] = set()
    for term in series_candidates:
        try:
            envelope = await cv_cache.search(
                db, term, resources="volume", limit=SEARCH_RESULT_LIMIT
            )
        except ComicVineRateLimitError:
            # Propagate so the match job pauses + re-enqueues this file;
            # don't let a rate limit masquerade as "no search results".
            raise
        except ComicVineError as e:
            logger.warning("Stage 3 search failed for series %r: %s", term, e)
            continue
        for stub in (envelope.get("results") or []):
            vid = safe_int(stub.get("id"))
            if vid is None or vid in seen_volume_ids:
                continue
            seen_volume_ids.add(vid)
            raw_results.append(stub)
    if not raw_results:
        return MatchResult(
            status=MatchStatus.UNMATCHED,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=0.0,
            candidates=[],
        )

    # Stage 3a-bis: rank-and-filter candidates BEFORE the expensive
    # /volume/X/ fetch, using only the stub fields the search response
    # already provides (id, name, start_year, count_of_issues). This caps
    # CV calls per file at ``MAX_VOLUMES_TO_FETCH`` regardless of how many
    # matches the search returned. See ``_prelim_score`` — the helper
    # combines a forgiving subset-match credit with a strict
    # length-aware metric against the longest candidate term, so the
    # specific long-form volume separates from the "starts with the
    # same word" volumes that ``token_set_ratio`` alone ties at 1.0.
    prelim: list[tuple[dict, float]] = []
    for vol_stub in raw_results:
        name = vol_stub.get("name") or ""
        prelim_score = _prelim_score(series_candidates, name)
        if prelim_score < MIN_PREFILTER_SCORE:
            continue
        # Cheap count-of-issues gate using only the search-response data.
        count = safe_int(vol_stub.get("count_of_issues"))
        if (
            count is not None
            and count > 0
            and issue_number is not None
            and _looks_like_high_number(issue_number)
        ):
            try:
                n = float(issue_number)
                if n > count * 2:
                    continue
            except (ValueError, TypeError):
                pass
        prelim.append((vol_stub, prelim_score))

    if not prelim:
        logger.info(
            "Stage 3: %d search results, none above pre-filter for series %r",
            len(raw_results),
            series,
        )
        return MatchResult(
            status=MatchStatus.UNMATCHED,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=0.0,
            candidates=[],
        )

    # Sort by pre-filter score and cap. Year-proximity is the
    # tiebreaker — see ``_prelim_sort_key`` for why this matters for
    # popular titles where every candidate has the same name.
    prelim.sort(key=lambda x: _prelim_sort_key(x, year))
    to_fetch = prelim[:MAX_VOLUMES_TO_FETCH]
    logger.info(
        "Stage 3: %d search results → %d after pre-filter → fetching %d",
        len(raw_results),
        len(prelim),
        len(to_fetch),
    )

    # Stage 3b: for each (capped, pre-filtered) candidate volume, get its
    # issues (populating from cache if needed) and score the matching issue.
    scored: list[dict] = []
    for vol_stub, _prelim in to_fetch:
        vol_cv_id = safe_int(vol_stub.get("id"))
        if vol_cv_id is None:
            continue
        # Ensure the volume + its issue list are in the cache. ``get_volume``
        # is a no-op for fresh full rows; for stubs (from previous Stage 1
        # runs) or misses, it makes one /volume/X/ call.
        try:
            volume = await cv_cache.get_volume(db, vol_cv_id)
        except ComicVineRateLimitError:
            # Propagate so the match job pauses + re-enqueues this file
            # rather than scoring it against a partial candidate set.
            raise
        except ComicVineError as e:
            logger.warning(
                "Stage 3: failed to load volume %d: %s; skipping candidate",
                vol_cv_id,
                e,
            )
            continue

        # Find the issue in this volume whose issue_number matches.
        matched_issue = await find_issue_by_number(db, vol_cv_id, issue_number)
        if matched_issue is None:
            continue  # gate: no matching issue_number, skip volume entirely

        score = _score(
            volume=volume,
            issue=matched_issue,
            parsed_series=series,
            parsed_issue_number=issue_number,
            parsed_year=year,
            alt_series=series_candidates,
            file_format=file_format,
        )
        scored.append(
            {
                "issue_cv_id": matched_issue.cv_id,
                "volume_cv_id": vol_cv_id,
                "volume_name": volume.name,
                "volume_year": volume.year,
                "issue_number": matched_issue.issue_number,
                "confidence": round(score, 3),
            }
        )

    # Sort descending; the design "top-N" picks the strongest matches.
    scored.sort(key=lambda c: c["confidence"], reverse=True)
    top = scored[:TOP_N_CANDIDATES]

    # Stage 4: decision.
    if not top:
        return MatchResult(
            status=MatchStatus.UNMATCHED,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=0.0,
            candidates=[],
        )
    best = top[0]
    if best["confidence"] >= AUTO_THRESHOLD:
        return MatchResult(
            status=MatchStatus.AUTO,
            source=MatchSource.FILENAME,
            issue_cv_id=best["issue_cv_id"],
            confidence=best["confidence"],
            candidates=top,
        )
    if best["confidence"] >= PENDING_THRESHOLD:
        return MatchResult(
            status=MatchStatus.PENDING,
            source=MatchSource.FILENAME,
            issue_cv_id=None,
            confidence=best["confidence"],
            candidates=top,
        )
    return MatchResult(
        status=MatchStatus.UNMATCHED,
        source=MatchSource.FILENAME,
        issue_cv_id=None,
        confidence=best["confidence"],
        candidates=top,
    )


# ---- Scoring ------------------------------------------------------------


def _score(
    *,
    volume: CvVolume,
    issue: CvIssue,
    parsed_series: str,
    parsed_issue_number: str | None,
    parsed_year: int | None,
    alt_series: list[str] | None = None,
    file_format: str | None = None,
) -> float:
    """Compute the 0-1 confidence for a candidate (volume, issue) pair.

    The gate (issue_number exact match) is enforced by the caller.

    ``file_format`` is the file's single-issue vs collected-edition
    guess. When it's known ("issue" / "collection") and disagrees
    with the candidate volume's classified format, the score is
    multiplied by ``FORMAT_MISMATCH_FACTOR`` — a single-issue file
    can't legitimately be a collected edition, and vice versa.

    ``alt_series`` is an optional list of additional series strings
    to consider for the fuzzy-similarity component — used when the
    matcher ran parallel searches (e.g., ``"Avengers"`` AND
    ``"Avengers - No More Bullying"``). The final series score is
    ``max()`` across all candidates so a volume that matches the
    long form perfectly isn't penalised for failing the short form,
    and vice versa. Defaults to ``[parsed_series]`` for backward
    compatibility with callers that don't have a long form.
    """
    # Series similarity: same strict+forgiving combination used in the
    # prefilter (see ``_prelim_score``). ``token_set_ratio`` alone gives
    # 1.0 to subset matches, which means "Avengers" (1963) ties with
    # "Avengers - No More Bullying" when the parsed long-series is the
    # latter — we'd lean entirely on year proximity to break the tie,
    # which fails when the filename has no year. The strict
    # length-aware half against the longest candidate fixes that.
    candidates = alt_series if alt_series else [parsed_series]
    series_score = _prelim_score(candidates, volume.name)

    # Year proximity. The file's year can be either the *volume's*
    # start year (some folks tag every issue of New 52 Batman as
    # ``(2011)``) or the *issue's* publication year (others tag the
    # 2013 cover-date issue as ``(2013)``). Both conventions are
    # common, and a multi-year run can have issues several years
    # past its start — issue 30 of a 2011 ongoing has cover_date
    # 2014, which would score year_score=0.15 against volume.year
    # alone and shove a perfect match into PENDING. So compare
    # ``parsed_year`` to BOTH signals and take whichever is closer.
    # For a wrong-volume candidate the file's year is far from both,
    # so min(delta) stays large and the score still drops out — the
    # min only helps when the file's year genuinely matches either
    # the volume's start or the issue's publication.
    if parsed_year is None:
        year_score = _YEAR_MISSING_NEUTRAL
    else:
        deltas: list[int] = []
        if volume.year is not None:
            deltas.append(abs(parsed_year - volume.year))
        if issue.cover_date is not None:
            deltas.append(abs(parsed_year - issue.cover_date.year))
        if not deltas:
            year_score = _YEAR_MISSING_NEUTRAL
        else:
            year_score = _YEAR_PROXIMITY.get(min(deltas), 0.0)

    score = WEIGHT_SERIES * series_score + WEIGHT_YEAR * year_score

    # count_of_issues sanity: a 1-shot volume (count_of_issues == 1) can't
    # legitimately have an issue #50. Penalize the case where the parsed
    # issue number greatly exceeds the volume's known issue count.
    if (
        volume.count_of_issues is not None
        and parsed_issue_number is not None
        and _looks_like_high_number(parsed_issue_number)
    ):
        try:
            n = int(float(parsed_issue_number))
            if volume.count_of_issues > 0 and n > volume.count_of_issues * 2:
                score *= 0.5
        except (ValueError, TypeError):
            pass

    # Format-mismatch penalty. A single-issue file can't legitimately
    # be a collected edition, and a collected-edition file can't be a
    # single issue / ongoing / limited series. When the file's format
    # is known and disagrees with the candidate volume's classified
    # format, knock the score down hard so the candidate can't reach
    # the PENDING band on its own.
    if file_format in ("issue", "collection"):
        volume_format = classify_cv_volume(volume)
        # Only penalise when the volume's format is actually known —
        # an "unknown" volume (e.g. CV reported no issue count)
        # shouldn't be punished on missing information.
        if volume_format in ("ongoing", "limited", "one_shot", "collection"):
            if (file_format == "collection") != (volume_format == "collection"):
                score *= FORMAT_MISMATCH_FACTOR

    return max(0.0, min(1.0, score))


def _looks_like_high_number(s: str) -> bool:
    """True if s parses as a numeric > 1. Skips ".5" / "1.MU" style values."""
    try:
        return float(s) > 1
    except (ValueError, TypeError):
        return False


# ---- DB helpers ---------------------------------------------------------


async def _first_current_location(
    db: AsyncSession, file_id: uuid.UUID
) -> FileLocation | None:
    stmt = (
        select(FileLocation)
        .where(
            FileLocation.file_id == file_id,
            FileLocation.missing_since.is_(None),
        )
        .order_by(FileLocation.last_seen_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_issue_by_number(
    db: AsyncSession, volume_cv_id: int, issue_number: str | None
) -> CvIssue | None:
    """Find a cv_issues row matching (volume_cv_id, issue_number) loosely.

    "Loosely" means we normalise both sides before comparing: strip leading
    zeros so "001" matches "1", lowercase for "1.MU" vs "1.mu" tolerance.

    Public so the review-bulk-confirm path can reuse the same
    matching logic the matcher uses internally — keeps the
    "volume + number → issue" mapping single-sourced.
    """
    if issue_number is None:
        return None
    target = _normalize_issue_number(issue_number)
    stmt = select(CvIssue).where(CvIssue.volume_cv_id == volume_cv_id)
    candidates = (await db.execute(stmt)).scalars().all()
    for c in candidates:
        if _normalize_issue_number(c.issue_number) == target:
            return c
    return None


def _normalize_issue_number(value: str | None) -> str:
    """Lowercase + strip leading zeros from the integer portion."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    if not s:
        return ""
    # Strip leading zeros only from the integer portion ("001" → "1",
    # "01.5" → "1.5", but ".5" stays ".5").
    if "." in s:
        head, tail = s.split(".", 1)
        head = head.lstrip("0") or ("0" if tail else "")
        s = f"{head}.{tail}"
    else:
        try:
            int(s)
        except ValueError:
            return s  # non-numeric ("annual 1") — leave as-is
        s = s.lstrip("0") or "0"
    return s


async def _load_comicinfo(
    path: str, archive_backend: str
) -> ComicInfoExtract | None:
    """Open the archive and parse its metadata. None on any failure.

    ``archive_backend`` is the caller-resolved admin setting; passed
    in so the matcher doesn't have to take a db handle just to look
    up the backend on every call.

    Reads both ComicInfo.xml AND MetronInfo.xml from the archive in
    a single thread hop, then picks the best result:

      * If ComicInfo gives us a CV ID (status FULL_WITH_CVID), use it
        — that's the gold-standard hint and ComicInfo's CV-in-Web
        convention is what the matcher's been tuned for.
      * Else if MetronInfo gives us a CV ID, use MetronInfo — its
        identifier resources are well-typed, no Web-tag regex
        guessing.
      * Else if either gives us PARTIAL hints, prefer ComicInfo for
        compatibility with existing behavior. (If only MetronInfo
        is present, use it.)
      * Else None.

    Archive reads are off the event loop — comicbox + rarfile can
    shell out / parse heavy formats, and the matcher runs across a
    list of files where blocking on each would compound."""
    def _read() -> tuple[bytes | None, bytes | None]:
        reader = open_archive(path, backend=archive_backend)
        return reader.read_comicinfo(), reader.read_metroninfo()
    try:
        comicinfo_bytes, metroninfo_bytes = await asyncio.to_thread(_read)
    except (ArchiveError, UnsupportedArchiveError, OSError) as e:
        logger.warning("Matcher couldn't read metadata from %s: %s", path, e)
        return None

    comicinfo = parse_comicinfo(comicinfo_bytes)
    metroninfo = parse_metroninfo(metroninfo_bytes)

    # Pick the best source per the priority rules above. ``status``
    # is the deciding signal — FULL_WITH_CVID > PARTIAL > NONE — with
    # ComicInfo winning ties.
    if comicinfo.cv_issue_id is not None:
        return comicinfo
    if metroninfo.cv_issue_id is not None:
        return metroninfo
    if comicinfo.status != ComicInfoStatus.NONE:
        return comicinfo
    if metroninfo.status != ComicInfoStatus.NONE:
        return metroninfo
    return None


# ---- Persistence --------------------------------------------------------


async def _persist(
    db: AsyncSession, file_id: uuid.UUID, result: MatchResult
) -> None:
    """Upsert the file_matches row. Caller doesn't need to commit; we do."""
    now = datetime.now(tz=UTC)
    confidence = (
        Decimal(str(result.confidence)).quantize(Decimal("0.001"))
        if result.confidence is not None
        else None
    )
    stmt = (
        pg_insert(FileMatch)
        .values(
            file_id=file_id,
            issue_cv_id=result.issue_cv_id,
            confidence=confidence,
            status=result.status,
            source=result.source,
            candidates=result.candidates or None,
            matched_at=now,
            matched_by=None,
        )
        .on_conflict_do_update(
            index_elements=[FileMatch.file_id],
            set_={
                "issue_cv_id": result.issue_cv_id,
                "confidence": confidence,
                "status": result.status,
                "source": result.source,
                "candidates": result.candidates or None,
                "matched_at": now,
                # Don't clobber matched_by on auto re-runs — preserve who
                # confirmed/rejected. (Auto/unmatched runs always overwrite
                # with NULL otherwise, which is also fine.)
                "matched_by": None,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
