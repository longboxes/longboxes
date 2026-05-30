"""Tests for the duplicate inspector service.

Covers the contract /admin/duplicates depends on:

* ``_score`` ordering — CBZ over CBR, FULL_WITH_CVID over PARTIAL,
  higher page count, larger cover, larger size, more recent scan as
  tiebreakers in that order.
* ``list_hash_duplicates`` returns only files with >1 current
  location, sorted by location-count descending.
* ``list_issue_duplicates`` returns only issues with >1 resolved
  file, picks a winner by ``_score``.
* ``mark_file_excluded`` flips the flag and survives re-fetch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
from app.services.duplicates import (
    _score,
    count_hash_duplicate_groups,
    count_issue_duplicate_groups,
    get_issue_duplicate_group,
    list_hash_duplicates,
    list_issue_duplicates,
    mark_file_excluded,
)

# Note: ``asyncio_mode = "auto"`` in pyproject.toml takes care of
# routing the async tests below through pytest-asyncio. A module-level
# ``pytestmark = pytest.mark.asyncio`` would also trigger noisy
# warnings on every sync ``_score`` test below.


# ---- _score ordering --------------------------------------------------


def _file(
    *,
    sha: str = "a" * 64,
    fmt: str = "cbz",
    pages: int = 30,
    cw: int | None = 1600,
    ch: int | None = 2400,
    # Interior dimensions default to None so existing tests keep
    # exercising the cover-area fallback path. New tests opt in by
    # passing ``iw`` / ``ih`` explicitly.
    iw: int | None = None,
    ih: int | None = None,
    size: int = 50 * 1024 * 1024,
    ci: ComicInfoStatus = ComicInfoStatus.PARTIAL,
    scanned: datetime | None = None,
) -> File:
    return File(
        sha256=sha,
        archive_format=fmt,
        page_count=pages,
        cover_width=cw,
        cover_height=ch,
        interior_width=iw,
        interior_height=ih,
        size_bytes=size,
        comicinfo_status=ci,
        excluded_from_matching=False,
        first_scanned_at=scanned or datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_score_real_issue_beats_fragment_regardless_of_format():
    """The user-reported failure: a 3-page CBZ (sketch variant /
    placeholder) was winning over a 25-page CBR (the real comic).
    Page-count plausibility is now the top tier — a fragment can't
    outrank a real issue on any lower-tier signal."""
    real_cbr = _file(
        fmt="cbr", pages=25, cw=1920, ch=2951, size=23 * 1024 * 1024,
        ci=ComicInfoStatus.FULL_WITH_CVID,
    )
    sketch_cbz = _file(
        fmt="cbz", pages=3, cw=1400, ch=2120, size=1_400_000,
        ci=ComicInfoStatus.FULL_WITH_CVID,
    )
    assert _score(real_cbr) > _score(sketch_cbz)


def test_score_full_cvid_beats_partial_at_same_plausibility():
    """Once both files clear the plausibility bar, ComicInfo
    coverage is the next decider. A 20-page full-CVID file beats a
    100-page partial — fine-grained page differences don't matter
    yet, ComicInfo does."""
    full = _file(fmt="cbz", pages=20, ci=ComicInfoStatus.FULL_WITH_CVID, cw=1000, ch=1500)
    partial = _file(fmt="cbz", pages=100, ci=ComicInfoStatus.PARTIAL, cw=4000, ch=6000)
    assert _score(full) > _score(partial)


def test_score_cover_area_breaks_tie_after_comicinfo():
    """Two files plausible + ComicInfo-equivalent → bigger cover
    wins. This is the case that picks the 1920x2951 rip over the
    1280x1960 one when both are real comics with CV IDs."""
    big = _file(pages=25, cw=1920, ch=2951, size=10 * 1024 * 1024)
    small = _file(pages=25, cw=1280, ch=1960, size=10 * 1024 * 1024)
    assert _score(big) > _score(small)


def test_score_prefers_interior_resolution_over_cover():
    """When both files have an interior sample, the scorer uses
    interior area for the resolution tier — the whole reason the
    interior column exists. The misleading case the user wanted to
    catch: a re-encode that shrank the cover but left the interior
    art at full resolution should still win. Here the small-cover
    file has a HUGE interior, beating the big-cover file with a
    tiny interior."""
    rich_interior = _file(
        pages=25, cw=800, ch=1200, iw=2400, ih=3600,
        size=10 * 1024 * 1024,
    )
    tiny_interior = _file(
        pages=25, cw=2400, ch=3600, iw=600, ih=900,
        size=10 * 1024 * 1024,
    )
    assert _score(rich_interior) > _score(tiny_interior)


def test_score_falls_back_to_cover_when_interior_missing():
    """A file scanned before the interior column existed (or one
    whose mid-archive page wasn't decodable) still gets a real
    resolution signal: the cover area. So a big-cover legacy file
    beats a small-cover legacy file at the resolution tier even
    though neither has an interior sample."""
    big_cover = _file(pages=25, cw=1920, ch=2951, iw=None, ih=None,
                      size=10 * 1024 * 1024)
    small_cover = _file(pages=25, cw=1280, ch=1960, iw=None, ih=None,
                        size=10 * 1024 * 1024)
    assert _score(big_cover) > _score(small_cover)


def test_score_file_with_interior_compares_against_legacy_cover():
    """Mixed-vintage library: one rescanned file has interior data,
    one legacy file only has cover data. The scorer compares
    interior_area for the new file against cover_area for the old
    one. A 2400x3600 interior beats a 1280x1960 cover — the
    fallback is a *fallback*, not a penalty."""
    new_with_interior = _file(
        pages=25, cw=600, ch=900, iw=2400, ih=3600,
        size=10 * 1024 * 1024,
    )
    legacy_big_cover = _file(
        pages=25, cw=1280, ch=1960, iw=None, ih=None,
        size=10 * 1024 * 1024,
    )
    assert _score(new_with_interior) > _score(legacy_big_cover)


def test_score_size_breaks_tie_after_cover_area():
    big = _file(pages=25, cw=1600, ch=2400, size=80 * 1024 * 1024)
    small = _file(pages=25, cw=1600, ch=2400, size=50 * 1024 * 1024)
    assert _score(big) > _score(small)


def test_score_format_is_a_minor_tiebreaker_not_a_dominant_one():
    """CBZ over CBR is a real preference (GPL-clean opens, no shell
    out to unar) but it only kicks in once plausibility / ComicInfo
    / cover / size all tie. A CBR with a bigger cover beats a CBZ
    with a smaller one."""
    cbr_big_cover = _file(fmt="cbr", pages=25, cw=1920, ch=2951)
    cbz_small_cover = _file(fmt="cbz", pages=25, cw=1280, ch=1960)
    assert _score(cbr_big_cover) > _score(cbz_small_cover)
    # But at identical everything else, CBZ wins.
    cbz_same = _file(fmt="cbz", pages=25, cw=1920, ch=2951)
    cbr_same = _file(fmt="cbr", pages=25, cw=1920, ch=2951)
    assert _score(cbz_same) > _score(cbr_same)


def test_score_raw_page_count_below_format():
    """Once plausibility, ComicInfo, cover, size, and format all
    tie, a 29-pager beats a 28-pager. Just a stable final ordering
    so output isn't arbitrary."""
    more = _file(pages=29, cw=1600, ch=2400)
    fewer = _file(pages=28, cw=1600, ch=2400)
    assert _score(more) > _score(fewer)


def test_score_recency_is_the_final_tiebreaker():
    recent = _file(pages=25, scanned=datetime(2026, 5, 1, tzinfo=UTC))
    old = _file(pages=25, scanned=datetime(2026, 1, 1, tzinfo=UTC))
    assert _score(recent) > _score(old)


def test_score_handles_missing_cover_dimensions_as_zero():
    """A file whose cover never got inspected (null cw/ch) shouldn't
    score above one that has measured dimensions, just because
    ``None * None`` is unhelpful."""
    inspected = _file(pages=25, cw=800, ch=1200)
    uninspected = _file(pages=25, cw=None, ch=None)
    assert _score(inspected) > _score(uninspected)


def test_score_suspect_page_count_beats_fragment():
    """The middle plausibility bucket (5-14 pages) — partial scans,
    back-half splits, two-page previews — still beats a single-digit
    fragment. Both lose to a real issue (>=15)."""
    suspect = _file(pages=10, cw=1600, ch=2400, size=5 * 1024 * 1024)
    fragment = _file(pages=2, cw=4000, ch=6000, size=50 * 1024 * 1024,
                     ci=ComicInfoStatus.FULL_WITH_CVID)
    real = _file(pages=25, cw=1600, ch=2400, size=5 * 1024 * 1024)
    assert _score(real) > _score(suspect) > _score(fragment)


# ---- list_hash_duplicates ---------------------------------------------


def _hash_file(db_session, sha: str) -> File:
    f = File(
        sha256=sha,
        size_bytes=1024 * 1024,
        archive_format="cbz",
        page_count=30,
        comicinfo_status=ComicInfoStatus.PARTIAL,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db_session.add(f)
    return f


def _location(file_id: uuid.UUID, path: str) -> FileLocation:
    return FileLocation(
        file_id=file_id,
        path=path,
        last_seen_at=datetime.now(tz=UTC),
        missing_since=None,
    )


async def test_list_hash_duplicates_only_multi_location_files(db_session):
    """A files row with one current location isn't a hash duplicate.
    Two current locations is. The single-location file must not
    appear in the result."""
    only_one = _hash_file(db_session, sha="b" * 64)
    duplicated = _hash_file(db_session, sha="c" * 64)
    await db_session.flush()
    db_session.add(_location(only_one.id, "/lib/single.cbz"))
    db_session.add(_location(duplicated.id, "/lib/dup-a.cbz"))
    db_session.add(_location(duplicated.id, "/lib/dup-b.cbz"))
    await db_session.commit()

    groups = await list_hash_duplicates(db_session)
    assert len(groups) == 1
    assert groups[0].file_id == duplicated.id
    paths = sorted(loc.path for loc in groups[0].locations)
    assert paths == ["/lib/dup-a.cbz", "/lib/dup-b.cbz"]


async def test_list_hash_duplicates_ignores_missing_locations(db_session):
    """A row marked ``missing_since`` doesn't count toward the
    duplicate threshold — the file isn't on disk there anymore."""
    f = _hash_file(db_session, sha="d" * 64)
    await db_session.flush()
    db_session.add(_location(f.id, "/lib/current.cbz"))
    missing = _location(f.id, "/lib/moved-away.cbz")
    missing.missing_since = datetime.now(tz=UTC)
    db_session.add(missing)
    await db_session.commit()

    groups = await list_hash_duplicates(db_session)
    assert groups == []


async def test_count_hash_duplicate_groups(db_session):
    a = _hash_file(db_session, sha="e" * 64)
    b = _hash_file(db_session, sha="f" * 64)
    await db_session.flush()
    db_session.add(_location(a.id, "/lib/a1.cbz"))
    db_session.add(_location(a.id, "/lib/a2.cbz"))
    db_session.add(_location(b.id, "/lib/b1.cbz"))  # single location
    await db_session.commit()

    assert await count_hash_duplicate_groups(db_session) == 1


# ---- list_issue_duplicates --------------------------------------------


async def _cv_volume(db_session, cv_id: int, name: str = "Vol") -> CvVolume:
    """Add a CvVolume and flush it immediately.

    SQLAlchemy's unit-of-work doesn't always topologically sort
    around the bare ``ForeignKey`` declaration on
    ``cv_issues.volume_cv_id`` (no ``relationship()`` is defined),
    so adding volume + issue + files together and flushing in one
    shot occasionally inserts the issue's row before the volume's.
    Flushing the volume on its own here side-steps that — the
    volume row lands, then any caller can ``add(_cv_issue(...))``
    without tripping the FK.
    """
    v = CvVolume(
        cv_id=cv_id,
        name=name,
        year=2020,
        publisher_cv_id=None,
        count_of_issues=1,
        raw_payload={"id": cv_id, "name": name},
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(v)
    await db_session.flush()
    return v


def _cv_issue(cv_id: int, volume_cv_id: int, number: str = "1") -> CvIssue:
    """Stub-shaped CvIssue — raw_payload has no ``image`` key.

    Models the "mid-hydration" state: matcher has populated the
    row's typed columns (issue_number / name / cover_date) but the
    bulk-issues job hasn't yet supplied image data. The duplicates
    inspector should treat these as deferred. Caller must have
    flushed the parent volume first (use ``_cv_volume``).
    """
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=volume_cv_id,
        issue_number=number,
        name="Issue",
        raw_payload={"id": cv_id, "name": "Issue"},
        fetched_at=datetime.now(tz=UTC),
    )


def _cv_issue_with_cover(
    cv_id: int, volume_cv_id: int, number: str = "1"
) -> CvIssue:
    """Fully-hydrated CvIssue — raw_payload carries an ``image``
    dict with the small/medium variants the duplicates inspector
    asks for. Use this whenever a test wants the group to actually
    appear in the listing."""
    return CvIssue(
        cv_id=cv_id,
        volume_cv_id=volume_cv_id,
        issue_number=number,
        name="Issue",
        raw_payload={
            "id": cv_id,
            "name": "Issue",
            "image": {
                "small_url": f"https://example.com/issue-{cv_id}.jpg",
            },
        },
        fetched_at=datetime.now(tz=UTC),
    )


def _match(
    file_id: uuid.UUID,
    issue_cv_id: int,
    status: MatchStatus = MatchStatus.AUTO,
) -> FileMatch:
    return FileMatch(
        file_id=file_id,
        issue_cv_id=issue_cv_id,
        confidence=None,
        status=status,
        source=MatchSource.FILENAME,
        candidates=None,
    )


async def test_list_issue_duplicates_picks_a_winner_by_score(db_session):
    """Two real-page-count files matching the same issue — the one
    with full ComicInfo + bigger cover should win, regardless of
    archive format. Mirrors the user-reported "3-page CBZ won't
    sneak past a 25-page CBR" scenario at the integration level."""
    await _cv_volume(db_session, 900, name="Test")
    db_session.add(_cv_issue_with_cover(9000, 900, number="42"))
    # Smaller-cover CBZ (the one that USED to win on format alone).
    cbz = File(
        sha256="1" * 64,
        size_bytes=30 * 1024 * 1024,
        archive_format="cbz",
        page_count=24,
        cover_width=1200,
        cover_height=1800,
        comicinfo_status=ComicInfoStatus.PARTIAL,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    # Bigger-cover CBR with full ComicInfo — the higher-quality rip.
    # Under the new ranking it should beat the CBZ.
    cbr = File(
        sha256="2" * 64,
        size_bytes=80 * 1024 * 1024,
        archive_format="cbr",
        page_count=24,
        cover_width=2400,
        cover_height=3600,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db_session.add_all([cbz, cbr])
    await db_session.flush()
    db_session.add(_match(cbz.id, 9000))
    db_session.add(_match(cbr.id, 9000))
    db_session.add(_location(cbz.id, "/lib/cbz.cbz"))
    db_session.add(_location(cbr.id, "/lib/cbr.cbr"))
    await db_session.commit()

    listing = await list_issue_duplicates(db_session)
    assert listing.deferred_count == 0
    assert len(listing.groups) == 1
    g = listing.groups[0]
    assert g.issue_cv_id == 9000
    assert g.volume_name == "Test"
    assert g.issue_number == "42"
    winners = [f for f in g.files if f.is_winner]
    assert len(winners) == 1
    # Under the corrected ranking: ComicInfo coverage (3 > 2) wins
    # at tier 2, before format ever gets a vote.
    assert winners[0].file_id == cbr.id


async def test_list_issue_duplicates_surfaces_cv_cover_url(db_session):
    """The CV issue's canonical cover URL flows onto the group so the
    template can render a thumbnail in the header. Lets the operator
    spot mismatched files visually — if the CV cover is Spider-Man
    but the files show X-Men interiors, something's wrong."""
    await _cv_volume(db_session, 905)
    issue = CvIssue(
        cv_id=9050,
        volume_cv_id=905,
        issue_number="1",
        name="Issue",
        raw_payload={
            "id": 9050,
            "name": "Issue",
            # CV image payloads carry a dict keyed by size — the
            # ``cv_image_url`` helper's "thumb" preference walks
            # thumb_url → small_url → medium_url, so a payload
            # without thumb_url falls through to small_url.
            "image": {
                "small_url": "https://example.com/cover-small.jpg",
                "medium_url": "https://example.com/cover-med.jpg",
            },
        },
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(issue)
    a = _hash_file(db_session, sha="a" * 64)
    b = _hash_file(db_session, sha="b" * 64)
    await db_session.flush()
    db_session.add(_match(a.id, 9050))
    db_session.add(_match(b.id, 9050))
    await db_session.commit()

    listing = await list_issue_duplicates(db_session)
    assert listing.deferred_count == 0
    assert len(listing.groups) == 1
    assert listing.groups[0].cover_url == "https://example.com/cover-small.jpg"


async def test_list_issue_duplicates_suppresses_unhydrated_groups(db_session):
    """A cv_issues row whose raw_payload has no image dict (still
    mid-hydration — bulk-issues job hasn't landed image data for it
    yet) gets EXCLUDED from the listing. The CV cover is the
    reference image for the visual mismatch comparison; without it
    the group is useless to show. The deferred count + the issue's
    volume cv_id flow out so the route can re-enqueue
    ``volume_issues`` hydration."""
    await _cv_volume(db_session, 906)
    # _cv_issue() builds a payload with no ``image`` key — exactly
    # the stub case the matcher writes for issues we haven't fully
    # hydrated yet.
    db_session.add(_cv_issue(9060, 906))
    a = _hash_file(db_session, sha="c" * 64)
    b = _hash_file(db_session, sha="d" * 64)
    await db_session.flush()
    db_session.add(_match(a.id, 9060))
    db_session.add(_match(b.id, 9060))
    await db_session.commit()

    listing = await list_issue_duplicates(db_session)
    assert listing.groups == []
    assert listing.deferred_count == 1
    # The route reads this list to enqueue volume_issues revalidates.
    assert listing.deferred_volume_cv_ids == [906]


async def test_list_issue_duplicates_mixed_hydrated_and_deferred(db_session):
    """A library with both hydrated and unhydrated groups: the
    listing returns the hydrated ones and reports the deferred
    count + volumes separately. Mirrors the real-world case where
    bulk hydration is mid-flight across several volumes."""
    await _cv_volume(db_session, 907)
    await _cv_volume(db_session, 908)
    db_session.add(_cv_issue_with_cover(9070, 907, number="1"))  # hydrated
    db_session.add(_cv_issue(9080, 908))  # stub — no image data
    # sha256 is VARCHAR(64) — keep every fixture sha at exactly 64
    # chars. Single hex digit as the disambiguating prefix.
    files = [
        _hash_file(db_session, sha=prefix * 64)
        for prefix in ("a", "b", "c", "d")
    ]
    await db_session.flush()
    db_session.add(_match(files[0].id, 9070))
    db_session.add(_match(files[1].id, 9070))
    db_session.add(_match(files[2].id, 9080))
    db_session.add(_match(files[3].id, 9080))
    await db_session.commit()

    listing = await list_issue_duplicates(db_session)
    assert [g.issue_cv_id for g in listing.groups] == [9070]
    assert listing.deferred_count == 1
    assert listing.deferred_volume_cv_ids == [908]


async def test_list_issue_duplicates_real_issue_beats_sketch_variant(db_session):
    """End-to-end regression for the user-reported bug: a 3-page
    sketch-variant CBZ shouldn't outrank a 25-page real-comic CBR
    just because CBZ is the preferred format."""
    await _cv_volume(db_session, 910, name="X-Men")
    # Hydrated issue — without image data, the group would get
    # deferred and the test would have no group to assert against.
    db_session.add(_cv_issue_with_cover(9100, 910, number="13"))
    real = File(
        sha256="3" * 64,
        size_bytes=23 * 1024 * 1024,
        archive_format="cbr",
        page_count=25,
        cover_width=1920,
        cover_height=2951,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    sketch = File(
        sha256="4" * 64,
        size_bytes=1_400_000,
        archive_format="cbz",
        page_count=3,
        cover_width=1400,
        cover_height=2120,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db_session.add_all([real, sketch])
    await db_session.flush()
    db_session.add(_match(real.id, 9100))
    db_session.add(_match(sketch.id, 9100))
    await db_session.commit()

    listing = await list_issue_duplicates(db_session)
    assert len(listing.groups) == 1
    winners = [f for f in listing.groups[0].files if f.is_winner]
    assert len(winners) == 1
    assert winners[0].file_id == real.id


async def test_list_issue_duplicates_skips_unresolved_files(db_session):
    """A pending file doesn't count toward the duplicate inventory —
    it's the review queue's problem. So two files where only one is
    AUTO/CONFIRMED isn't a duplicate."""
    await _cv_volume(db_session, 901)
    db_session.add(_cv_issue(9001, 901))
    a = _hash_file(db_session, sha="3" * 64)
    b = _hash_file(db_session, sha="4" * 64)
    await db_session.flush()
    db_session.add(_match(a.id, 9001, status=MatchStatus.AUTO))
    db_session.add(_match(b.id, 9001, status=MatchStatus.PENDING))
    await db_session.commit()

    listing = await list_issue_duplicates(db_session)
    # No group surfaces because only one file is resolved; the
    # "1 resolved file" case is filtered before hydration even
    # gets a vote, so deferred_count stays 0.
    assert listing.groups == []
    assert listing.deferred_count == 0


# ---- get_issue_duplicate_group ----------------------------------------
#
# Single-issue counterpart used by the per-issue Compare page. Shares
# the _build_issue_group helper with list_issue_duplicates so the
# scoring / cover-required behaviour matches; these tests cover the
# edge cases of the single-issue lookup (≤1 file → None, unhydrated
# cover → None, happy path → populated group).


async def test_get_issue_duplicate_group_returns_none_for_single_file(
    db_session,
):
    """One file matched to an issue is not a duplicate group — the
    Compare button shouldn't even render, and the route redirects
    back to the issue page if a stale URL lands here."""
    await _cv_volume(db_session, 920)
    db_session.add(_cv_issue_with_cover(9200, 920))
    f = _hash_file(db_session, sha="a1" * 32)
    await db_session.flush()
    db_session.add(_match(f.id, 9200))
    await db_session.commit()

    group = await get_issue_duplicate_group(db_session, 9200)
    assert group is None


async def test_get_issue_duplicate_group_returns_populated_group(
    db_session,
):
    """The happy path: ≥2 resolved files + hydrated CV cover →
    IssueGroup with files ranked and a winner flagged. Shape matches
    what list_issue_duplicates returns for the same issue."""
    await _cv_volume(db_session, 921, name="Compare Vol")
    db_session.add(_cv_issue_with_cover(9210, 921, number="7"))
    cbz = File(
        sha256="b1" * 32,
        size_bytes=30 * 1024 * 1024,
        archive_format="cbz",
        page_count=24,
        cover_width=1200, cover_height=1800,
        comicinfo_status=ComicInfoStatus.PARTIAL,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    cbr = File(
        sha256="b2" * 32,
        size_bytes=80 * 1024 * 1024,
        archive_format="cbr",
        page_count=24,
        cover_width=2400, cover_height=3600,
        comicinfo_status=ComicInfoStatus.FULL_WITH_CVID,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )
    db_session.add_all([cbz, cbr])
    await db_session.flush()
    db_session.add(_match(cbz.id, 9210))
    db_session.add(_match(cbr.id, 9210))
    db_session.add(_location(cbz.id, "/lib/a.cbz"))
    db_session.add(_location(cbr.id, "/lib/b.cbr"))
    await db_session.commit()

    group = await get_issue_duplicate_group(db_session, 9210)
    assert group is not None
    assert group.issue_cv_id == 9210
    assert group.volume_name == "Compare Vol"
    assert group.issue_number == "7"
    assert len(group.files) == 2
    winners = [f for f in group.files if f.is_winner]
    assert len(winners) == 1
    # Same scoring as the listing — higher ComicInfo coverage + bigger
    # cover wins (the CBR here).
    assert winners[0].file_id == cbr.id
    # CV cover surfaces for the page header.
    assert group.cover_url == "https://example.com/issue-9210.jpg"


async def test_get_issue_duplicate_group_returns_none_when_cover_missing(
    db_session,
):
    """Mid-hydration suppression matches the listing behaviour: an
    issue whose raw_payload has no image dict yields None, not a
    half-rendered group. The Compare page handles this by redirecting
    to the issue page rather than rendering a header with no cover."""
    await _cv_volume(db_session, 922)
    # _cv_issue() builds a stub payload with no image data — the
    # mid-hydration shape.
    db_session.add(_cv_issue(9220, 922))
    a = _hash_file(db_session, sha="c1" * 32)
    b = _hash_file(db_session, sha="c2" * 32)
    await db_session.flush()
    db_session.add(_match(a.id, 9220))
    db_session.add(_match(b.id, 9220))
    await db_session.commit()

    group = await get_issue_duplicate_group(db_session, 9220)
    assert group is None


async def test_get_issue_duplicate_group_ignores_unresolved_files(
    db_session,
):
    """Pending / unmatched / rejected files don't count toward the
    duplicate-group floor — matches list_issue_duplicates. An issue
    with one AUTO file and one PENDING file is not a Compare-worthy
    duplicate; the PENDING file is a review-queue concern, not a
    triage one."""
    await _cv_volume(db_session, 923)
    db_session.add(_cv_issue_with_cover(9230, 923))
    a = _hash_file(db_session, sha="d1" * 32)
    b = _hash_file(db_session, sha="d2" * 32)
    await db_session.flush()
    db_session.add(_match(a.id, 9230, status=MatchStatus.AUTO))
    db_session.add(_match(b.id, 9230, status=MatchStatus.PENDING))
    await db_session.commit()

    group = await get_issue_duplicate_group(db_session, 9230)
    assert group is None


async def test_count_issue_duplicate_groups(db_session):
    await _cv_volume(db_session, 902)
    db_session.add(_cv_issue(9002, 902))
    db_session.add(_cv_issue(9003, 902))
    files = []
    for i in range(3):
        f = _hash_file(db_session, sha=f"{i + 5}{'5' * 63}")
        files.append(f)
    await db_session.flush()
    # 9002 has 2 confirmed → 1 group.
    db_session.add(_match(files[0].id, 9002, status=MatchStatus.AUTO))
    db_session.add(_match(files[1].id, 9002, status=MatchStatus.CONFIRMED))
    # 9003 has 1 confirmed → not a group.
    db_session.add(_match(files[2].id, 9003, status=MatchStatus.AUTO))
    await db_session.commit()

    assert await count_issue_duplicate_groups(db_session) == 1


# ---- mark_file_excluded -----------------------------------------------


async def test_mark_file_excluded_flips_the_flag(db_session):
    f = _hash_file(db_session, sha="9" * 64)
    await db_session.commit()
    assert f.excluded_from_matching is False

    ok = await mark_file_excluded(db_session, f.id)
    assert ok is True
    # Re-fetch to confirm the change committed.
    await db_session.refresh(f)
    assert f.excluded_from_matching is True


async def test_mark_file_excluded_returns_false_for_missing_id(db_session):
    bogus = uuid.uuid4()
    assert await mark_file_excluded(db_session, bogus) is False
