"""Tests for the RQ queue-status snapshot shown on the admin Health page.

Exercises ``get_queue_stats`` against a fakeredis-backed RQ queue — no
real Redis and no worker, so the counts are deterministic.
"""

from datetime import timedelta

import fakeredis
from rq import Queue
from rq.job import JobStatus

from app.jobs.queue_status import _parse_exc_info, get_job_position, get_queue_stats
from app.jobs.revalidate import revalidate_cv_entity_job, revalidate_job_id

# Any importable dotted path works — RQ stores the name and only resolves
# it at execution time, which these tests never reach.
_JOB = "app.jobs.match_file.match_file_job"


def test_queue_stats_counts_queued_and_scheduled_jobs():
    conn = fakeredis.FakeStrictRedis()
    queue = Queue("default", connection=conn)

    # Three jobs ready to run now.
    for i in range(3):
        queue.enqueue(_JOB, str(i))
    # One job deferred via enqueue_in lands in the ScheduledJobRegistry —
    # the same path the rate-limit re-enqueue uses.
    queue.enqueue_in(timedelta(minutes=10), _JOB, "later")

    stats = get_queue_stats(connection=conn)
    assert stats is not None
    assert stats.queued == 3
    assert stats.scheduled == 1
    assert stats.started == 0
    assert stats.failed == 0
    assert stats.outstanding == 4


def test_queue_stats_empty_queue_is_all_zeros():
    conn = fakeredis.FakeStrictRedis()
    stats = get_queue_stats(connection=conn)
    assert stats is not None
    assert stats.queued == 0
    assert stats.scheduled == 0
    assert stats.started == 0
    assert stats.failed == 0
    assert stats.outstanding == 0


def test_queue_stats_returns_none_when_redis_unreachable():
    """A downed Redis must degrade to None so the Health page still renders
    — the queue widget is not worth 500-ing the whole page over."""
    server = fakeredis.FakeServer()
    server.connected = False  # fakeredis: every command now raises ConnectionError
    conn = fakeredis.FakeStrictRedis(server=server)

    assert get_queue_stats(connection=conn) is None


# ---- get_job_position -------------------------------------------------


def _enqueue_revalidate_directly(queue, entity_type, cv_id, *, at_front=False):
    """Skip the dedupe path in ``enqueue_revalidate`` and put the job
    on the queue with the deterministic id we want to look up."""
    return queue.enqueue(
        revalidate_cv_entity_job,
        entity_type,
        cv_id,
        job_id=revalidate_job_id(entity_type, cv_id),
        at_front=at_front,
    )


def test_get_job_position_reports_queue_index():
    """A queued job's 1-based position equals its index in the LRANGE
    plus 1; depth equals the queue size. The Confirm Volume hydration
    toast reads these to render "in queue, position N of M"."""
    conn = fakeredis.FakeStrictRedis()
    queue = Queue("default", connection=conn)
    # Tail-fill the queue with unrelated jobs so position math is
    # non-trivial. ``volume_issues`` always lands at the head, so
    # explicitly disable that for the seed jobs.
    for n in (1, 2, 3):
        _enqueue_revalidate_directly(queue, "volume", n)
    # Now enqueue the target at the back (default at_front=False).
    _enqueue_revalidate_directly(queue, "volume", 99)

    pos = get_job_position("volume", 99, connection=conn)
    assert pos.state == "queued"
    assert pos.position == 4
    assert pos.depth == 4
    assert pos.retry_after is None


def test_get_job_position_started_state():
    """A running job moves from the queue into ``StartedJobRegistry``;
    its state should report as ``"started"``, not ``"queued"``.

    The helper keys off ``job.get_status()`` rather than registry
    membership (and explicitly NOT off the registry's count), so
    flipping the status + removing from the queue list is enough to
    simulate the started state in fakeredis. RQ 2.x's
    ``StartedJobRegistry.add()`` raises NotImplementedError — the
    registry's wire format is now ``{job_id}:{execution_id}`` and the
    workers populate it via a different code path — so the simpler
    status-flip is the right test fake here."""
    conn = fakeredis.FakeStrictRedis()
    queue = Queue("default", connection=conn)
    job = _enqueue_revalidate_directly(queue, "volume_issues", 50)
    # Simulate the worker pulling the job: drop it from the queue list
    # and flip its persisted status.
    queue.remove(job.id)
    job.set_status(JobStatus.STARTED)

    pos = get_job_position("volume_issues", 50, connection=conn)
    assert pos.state == "started"
    assert pos.position is None
    assert pos.depth is None


def test_get_job_position_scheduled_reports_retry_after():
    """A rate-limit re-enqueue lands in ``ScheduledJobRegistry`` with
    a fire-at timestamp; we report the remaining seconds so the toast
    can say "rate-limit cooldown — Xm remaining"."""
    conn = fakeredis.FakeStrictRedis()
    queue = Queue("default", connection=conn)
    # ``enqueue_in`` schedules with a fixed job_id we control.
    queue.enqueue_in(
        timedelta(minutes=8),
        revalidate_cv_entity_job,
        "volume_issues",
        77,
        job_id=revalidate_job_id("volume_issues", 77),
    )

    pos = get_job_position("volume_issues", 77, connection=conn)
    assert pos.state == "scheduled"
    # Allow a tolerance window around 8 minutes for clock drift
    # between enqueue and read.
    assert pos.retry_after is not None
    assert 7 * 60 - 5 <= pos.retry_after <= 8 * 60 + 5


def test_get_job_position_missing_when_no_job():
    """No job under that id at all — caller treats ``"missing"`` the
    same as ``"done"``: stop showing the queue-status sub-line, let
    the swap response (or its absence) drive the rest of the UI."""
    conn = fakeredis.FakeStrictRedis()
    pos = get_job_position("volume_issues", 12345, connection=conn)
    assert pos.state == "missing"
    assert pos.position is None
    assert pos.depth is None
    assert pos.retry_after is None


def test_job_position_to_dict_shape():
    """``to_dict`` produces exactly the keys the polling endpoint sends
    over the wire — the Alpine front-end depends on these field names."""
    conn = fakeredis.FakeStrictRedis()
    pos = get_job_position("volume_issues", 999, connection=conn)
    payload = pos.to_dict()
    assert set(payload.keys()) == {"state", "position", "depth", "retry_after"}


# ---- _parse_exc_info --------------------------------------------------


def test_parse_exc_info_empty_returns_unknown():
    assert _parse_exc_info(None) == ("Unknown", "")
    assert _parse_exc_info("") == ("Unknown", "")


def test_parse_exc_info_picks_last_exception_line():
    """Simple single-frame traceback — the last non-empty line is the
    ``ExceptionClass: message`` we want."""
    blob = (
        "Traceback (most recent call last):\n"
        '  File "/app/jobs/match_file.py", line 42, in match_file_job\n'
        "    do_thing()\n"
        "ValueError: something went wrong\n"
    )
    cls, msg = _parse_exc_info(blob)
    assert cls == "ValueError"
    assert msg == "something went wrong"


def test_parse_exc_info_skips_sqlalchemy_background_hint():
    """SQLAlchemy errors trail a ``(Background on this error at ...)``
    URL hint after the real exception line. The parser must walk past
    it and surface the actual ``IntegrityError`` line instead — that's
    the only line that tells the operator which constraint failed."""
    blob = (
        "Traceback (most recent call last):\n"
        '  File "/app/jobs/revalidate.py", line 80, in revalidate_cv_entity_job\n'
        "    await session.commit()\n"
        "sqlalchemy.exc.IntegrityError: (psycopg.errors.ForeignKeyViolation) "
        'insert or update on table "cv_issues" violates foreign key constraint '
        '"cv_issues_volume_cv_id_fkey"\n'
        'DETAIL:  Key (volume_cv_id)=(18166) is not present in table "cv_volumes".\n'
        "[SQL: INSERT INTO cv_issues (...) VALUES (...)]\n"
        "[parameters: {'volume_cv_id': 18166, ...}]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)\n"
    )
    cls, msg = _parse_exc_info(blob)
    assert cls == "sqlalchemy.exc.IntegrityError"
    assert "ForeignKeyViolation" in msg
    assert "cv_issues_volume_cv_id_fkey" in msg
    # Crucially we did NOT surface the URL hint as the message.
    assert "sqlalche.me" not in msg


def test_parse_exc_info_skips_hint_and_context_lines():
    """``HINT:`` / ``CONTEXT:`` lines from Postgres also follow the
    real exception line — walk past them too."""
    blob = (
        "psycopg.errors.UniqueViolation: duplicate key value violates unique "
        'constraint "files_path_key"\n'
        "DETAIL:  Key (path)=(/library/foo.cbr) already exists.\n"
        "HINT:  Try a different path.\n"
        "CONTEXT:  while inserting row\n"
    )
    cls, msg = _parse_exc_info(blob)
    assert cls == "psycopg.errors.UniqueViolation"
    assert "files_path_key" in msg
