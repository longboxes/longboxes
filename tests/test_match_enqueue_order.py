"""Tests for the match-all enqueue order (Levers 3 + 4 from the
hyperspeed plan).

``enqueue_match_all_unmatched_async`` sorts the per-file enqueue so
the cheap matcher tier (``FULL_WITH_CVID`` — Stage 1 fast path)
drains before the expensive one (``NONE`` — full Stage 2-4 search),
and within each tier files of the same series cluster by their
``file_locations.path`` prefix. We pin the order by capturing the
sequence of ``str(file_id)`` values handed to ``Queue.enqueue`` —
the existing module-level ``Queue`` import gets monkeypatched to a
recorder, so no Redis is required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.jobs.match_file import enqueue_match_all_unmatched_async
from app.models import ComicInfoStatus, File, FileLocation, FileMatch, MatchStatus

pytestmark = pytest.mark.asyncio


class _QueueRecorder:
    """Stand-in for ``rq.Queue`` — records ``enqueue`` calls so the
    test can assert on the order without spinning up Redis. Implements
    only the surface the production code actually uses (constructor
    plus ``enqueue``)."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[str] = []

    def enqueue(self, _job_fn, file_id: str, **_kwargs) -> None:
        self.calls.append(file_id)


@pytest.fixture
def _capture_enqueue(monkeypatch) -> _QueueRecorder:
    """Patch the module-level ``Queue`` so ``enqueue_match_all_unmatched_async``
    hands its file_ids to a recorder instead of a real Redis-backed
    queue. The single recorder instance is returned both via the
    fixture and via every ``Queue(...)`` constructor call inside the
    function under test, so the test reads the captured order off
    one stable object."""
    rec = _QueueRecorder()
    monkeypatch.setattr("app.jobs.match_file.Queue", lambda *a, **kw: rec)

    # ``Redis.from_url`` runs before ``Queue(...)`` in the production
    # function and returns a connection the recorder never touches.
    # The string makes that obvious if a stray test does poke at it.
    monkeypatch.setattr(
        "app.jobs.match_file.Redis.from_url",
        lambda *_a, **_kw: "fake-redis-connection",
    )
    return rec


def _file(
    *,
    sha: str,
    status: ComicInfoStatus,
) -> File:
    return File(
        sha256=sha,
        size_bytes=1024,
        archive_format="cbz",
        page_count=20,
        comicinfo_status=status,
        excluded_from_matching=False,
        first_scanned_at=datetime.now(tz=UTC),
    )


def _location(file_id: uuid.UUID, path: str) -> FileLocation:
    return FileLocation(
        file_id=file_id,
        path=path,
        mtime=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
        missing_since=None,
    )


def _sha(prefix: str) -> str:
    """Pad a one-character prefix into the 64-char sha256 column."""
    return (prefix * 64)[:64]


# ---- Lever 3: tier ordering ---------------------------------------------


async def test_full_with_cvid_files_enqueue_before_partial_and_none(
    db_session, _capture_enqueue
):
    """The cheapest matcher tier drains first. On a real library this
    is what makes the user-visible "library populated" milestone
    happen in hours instead of at the end of the run.

    Each file gets a distinct path so the within-tier secondary sort
    is deterministic and doesn't interfere with the cross-tier
    assertion."""
    full = _file(sha=_sha("a"), status=ComicInfoStatus.FULL_WITH_CVID)
    partial = _file(sha=_sha("b"), status=ComicInfoStatus.PARTIAL)
    none = _file(sha=_sha("c"), status=ComicInfoStatus.NONE)
    db_session.add_all([full, partial, none])
    await db_session.flush()
    db_session.add_all(
        [
            _location(full.id, "/library/A/issue 01.cbz"),
            _location(partial.id, "/library/B/issue 01.cbz"),
            _location(none.id, "/library/C/issue 01.cbz"),
        ]
    )
    await db_session.commit()

    count = await enqueue_match_all_unmatched_async(db_session)
    assert count == 3

    order = _capture_enqueue.calls
    assert order == [str(full.id), str(partial.id), str(none.id)]


async def test_tier_ordering_holds_even_when_filesystem_order_is_inverted(
    db_session, _capture_enqueue
):
    """Same as above but with the cheap-tier file inserted LAST and
    sitting at a path that lexicographically sorts AFTER the
    expensive-tier files. The tier rank takes priority over the
    within-tier path proxy, so the FULL_WITH_CVID file still wins
    despite both signals nominally pushing it last."""
    none_a = _file(sha=_sha("a"), status=ComicInfoStatus.NONE)
    none_b = _file(sha=_sha("b"), status=ComicInfoStatus.NONE)
    db_session.add_all([none_a, none_b])
    await db_session.flush()
    db_session.add_all(
        [
            _location(none_a.id, "/library/A/issue 01.cbz"),
            _location(none_b.id, "/library/B/issue 01.cbz"),
        ]
    )
    await db_session.commit()

    # Inserted last, path sorts last — but cheapest tier.
    full = _file(sha=_sha("c"), status=ComicInfoStatus.FULL_WITH_CVID)
    db_session.add(full)
    await db_session.flush()
    db_session.add(_location(full.id, "/library/Z/issue 01.cbz"))
    await db_session.commit()

    await enqueue_match_all_unmatched_async(db_session)
    assert _capture_enqueue.calls[0] == str(full.id)


# ---- Lever 4: within-tier series clustering ----------------------------


async def test_same_series_files_cluster_within_tier(
    db_session, _capture_enqueue
):
    """Files in the same series share a parent directory in almost
    every real library layout — Mylar / Komga / Kapowarr / plain
    folders all do this. Sorting by the lex-first path means the
    matcher fetches each parent volume once and the cache stays hot
    for the rest of the series rather than thrashing across
    interleaved candidates."""
    # Two series, two issues each, all in the same NONE tier so any
    # ordering we see is the path proxy's doing.
    saga_1 = _file(sha=_sha("a"), status=ComicInfoStatus.NONE)
    saga_2 = _file(sha=_sha("b"), status=ComicInfoStatus.NONE)
    paper_1 = _file(sha=_sha("c"), status=ComicInfoStatus.NONE)
    paper_2 = _file(sha=_sha("d"), status=ComicInfoStatus.NONE)
    db_session.add_all([saga_1, saga_2, paper_1, paper_2])
    await db_session.flush()
    # Insertion order deliberately interleaves the two series, so a
    # sort by File.id alone would also interleave the output.
    db_session.add_all(
        [
            _location(saga_1.id, "/library/Saga (2012)/Saga 001.cbz"),
            _location(paper_1.id, "/library/Paper Girls/Paper Girls 001.cbz"),
            _location(saga_2.id, "/library/Saga (2012)/Saga 002.cbz"),
            _location(paper_2.id, "/library/Paper Girls/Paper Girls 002.cbz"),
        ]
    )
    await db_session.commit()

    await enqueue_match_all_unmatched_async(db_session)

    # Expected order: Paper Girls 001, Paper Girls 002, Saga 001,
    # Saga 002 — "P" sorts before "S", and within each series the
    # issue-number ordering of the path proxy holds.
    assert _capture_enqueue.calls == [
        str(paper_1.id),
        str(paper_2.id),
        str(saga_1.id),
        str(saga_2.id),
    ]


# ---- Existing inclusion/exclusion semantics --------------------------


async def test_already_matched_files_are_not_re_enqueued(
    db_session, _capture_enqueue
):
    """Pin the existing inclusion rule: a file with an AUTO match is
    settled and the match-all enqueue must skip it. UNMATCHED and
    PENDING are transient — those still come through."""
    settled = _file(sha=_sha("a"), status=ComicInfoStatus.FULL_WITH_CVID)
    pending = _file(sha=_sha("b"), status=ComicInfoStatus.NONE)
    unmatched = _file(sha=_sha("c"), status=ComicInfoStatus.NONE)
    db_session.add_all([settled, pending, unmatched])
    await db_session.flush()
    db_session.add_all(
        [
            FileMatch(
                file_id=settled.id,
                status=MatchStatus.AUTO,
                source="comicinfo_cvid",
            ),
            FileMatch(
                file_id=pending.id,
                status=MatchStatus.PENDING,
                source="filename",
            ),
            FileMatch(
                file_id=unmatched.id,
                status=MatchStatus.UNMATCHED,
                source="filename",
            ),
        ]
    )
    db_session.add_all(
        [
            _location(settled.id, "/library/Saga/issue 01.cbz"),
            _location(pending.id, "/library/Unknown/file 01.cbz"),
            _location(unmatched.id, "/library/Unknown/file 02.cbz"),
        ]
    )
    await db_session.commit()

    count = await enqueue_match_all_unmatched_async(db_session)
    assert count == 2
    assert set(_capture_enqueue.calls) == {str(pending.id), str(unmatched.id)}


async def test_excluded_files_are_not_enqueued(
    db_session, _capture_enqueue
):
    """``excluded_from_matching=True`` files are out of the matcher's
    world entirely — they shouldn't surface in the match-all enqueue
    regardless of their match status."""
    skip = _file(sha=_sha("a"), status=ComicInfoStatus.NONE)
    skip.excluded_from_matching = True
    include = _file(sha=_sha("b"), status=ComicInfoStatus.NONE)
    db_session.add_all([skip, include])
    await db_session.flush()
    db_session.add_all(
        [
            _location(skip.id, "/library/excluded.cbz"),
            _location(include.id, "/library/included.cbz"),
        ]
    )
    await db_session.commit()

    count = await enqueue_match_all_unmatched_async(db_session)
    assert count == 1
    assert _capture_enqueue.calls == [str(include.id)]


async def test_file_with_no_locations_still_enqueues_at_tier_end(
    db_session, _capture_enqueue
):
    """The path proxy is a correlated subquery — when the file has
    no current locations the subquery returns NULL. In Postgres the
    default ASC sort puts NULLs last, so the file lands at the end
    of its tier rather than disappearing or crashing the sort. The
    File.id tiebreaker keeps the result deterministic."""
    located = _file(sha=_sha("a"), status=ComicInfoStatus.NONE)
    orphan = _file(sha=_sha("b"), status=ComicInfoStatus.NONE)
    db_session.add_all([located, orphan])
    await db_session.flush()
    db_session.add(_location(located.id, "/library/path.cbz"))
    await db_session.commit()

    await enqueue_match_all_unmatched_async(db_session)
    assert _capture_enqueue.calls == [str(located.id), str(orphan.id)]
