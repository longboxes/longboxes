"""Point-in-time snapshot of the RQ ``default`` queue.

Surfaced on the admin Health page so an operator can watch the match
backlog drain without tailing worker logs. Every count is a cheap Redis
read — ``LLEN`` for the queue itself, ``ZCARD`` for each registry —
against the live queue, so there is no separate bookkeeping to keep in
sync.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from rq.registry import FailedJobRegistry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.jobs.revalidate import revalidate_job_id
from app.models import File, FileLocation

logger = logging.getLogger("longboxes.jobs.queue_status")

# The single queue every job type shares — see ``match_file`` / ``revalidate``.
_QUEUE_NAME = "default"


@dataclass
class QueueStats:
    """RQ ``default``-queue counts at the moment ``get_queue_stats`` ran."""

    queued: int = 0      # ready to run now — the bulk of a match backlog
    scheduled: int = 0   # waiting out a delay: rate-limit re-enqueues
    started: int = 0     # executing right now (<= 1 with a single worker)
    failed: int = 0      # raised and exhausted their retries

    @property
    def outstanding(self) -> int:
        """Work not yet finished — queued + scheduled + still running."""
        return self.queued + self.scheduled + self.started


def get_queue_stats(connection: Redis | None = None) -> QueueStats | None:
    """Snapshot the ``default`` queue.

    Returns ``None`` if Redis can't be reached — the Health page renders
    that as "unavailable" rather than failing the whole page over a
    monitoring widget. ``connection`` is injectable for tests; in
    production it defaults to a fresh client on ``settings.redis_url``.
    """
    try:
        conn = connection or Redis.from_url(settings.redis_url)
        queue = Queue(_QUEUE_NAME, connection=conn)
        return QueueStats(
            queued=queue.count,
            scheduled=queue.scheduled_job_registry.count,
            started=queue.started_job_registry.count,
            failed=queue.failed_job_registry.count,
        )
    except Exception as exc:
        logger.warning("queue stats unavailable: %s", exc)
        return None


JobState = Literal["queued", "started", "scheduled", "done", "missing"]


@dataclass
class JobPosition:
    """Current location of a specific revalidate job.

    Drives the Confirm Volume page's hydration toast so a stuck or
    in-flight bulk hydration tells the user where it sits rather than
    looping silently. All fields are JSON-serialisable so the polling
    endpoint can return this directly.
    """

    state: JobState
    # 1-based queue position; only meaningful for ``state == "queued"``.
    position: int | None = None
    # Total jobs currently in the ``default`` queue (LLEN) — paired with
    # ``position`` so the UI can render "N of M". Same caveat: only
    # meaningful for ``state == "queued"``.
    depth: int | None = None
    # Seconds remaining until a scheduled job becomes due. Only
    # meaningful for ``state == "scheduled"``.
    retry_after: int | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "position": self.position,
            "depth": self.depth,
            "retry_after": self.retry_after,
        }


def get_job_position(
    entity_type: str,
    cv_id: int,
    connection: Redis | None = None,
) -> JobPosition:
    """Resolve the current state of a revalidate job by its deterministic id.

    The Confirm Volume page polls this so it can render a useful "your
    hydration is running" / "in queue position 47 of 250" /
    "rate-limit cooldown — 9 minutes remaining" message instead of an
    indefinite spinner. Every branch is a single Redis call, so this
    is cheap to poll alongside the swap response.

    Failures degrade to ``state="missing"`` — the polling loop already
    handles that gracefully (it stops asking once the swap response
    confirms the cover has populated).
    """
    job_id = revalidate_job_id(entity_type, cv_id)
    try:
        conn = connection or Redis.from_url(settings.redis_url)
        try:
            job = Job.fetch(job_id, connection=conn)
        except NoSuchJobError:
            return JobPosition(state="missing")

        try:
            status = job.get_status()
        except Exception:
            return JobPosition(state="missing")

        # Look up the position on the queue the job actually came in
        # on. Browse-page revalidates land on ``interactive`` under
        # the worker-topology split
        # and ``job.origin`` is the queue name RQ stores on the job
        # itself, so this stays correct as more lanes get added.
        # Falls back to the legacy ``default`` for jobs that pre-date
        # the split or were enqueued by a caller that didn't set a
        # queue.
        origin = getattr(job, "origin", None) or _QUEUE_NAME
        queue = Queue(origin, connection=conn)

        if status == JobStatus.STARTED:
            return JobPosition(state="started")

        if status == JobStatus.QUEUED:
            # ``Queue.job_ids`` reads the full LRANGE in dispatch order
            # (head first). One scan finds the job's slot; ``len`` is
            # implicit in the same list, so we read both for free.
            job_ids = queue.job_ids
            try:
                idx = job_ids.index(job_id)
            except ValueError:
                # Race: the job moved between the status read and the
                # range read. Report it as in flight without a position
                # rather than misclassifying.
                return JobPosition(state="queued", position=None, depth=len(job_ids))
            return JobPosition(
                state="queued", position=idx + 1, depth=len(job_ids)
            )

        if status == JobStatus.SCHEDULED:
            # Scheduled jobs sit in a sorted set keyed on their fire-at
            # timestamp. ZSCORE gives the absolute Unix time; subtract
            # now to get a seconds countdown.
            registry = queue.scheduled_job_registry
            score = conn.zscore(registry.key, job_id)
            if score is None:
                return JobPosition(state="scheduled")
            retry_after = max(0, int(float(score) - time.time()))
            return JobPosition(state="scheduled", retry_after=retry_after)

        # finished / failed / canceled — caller renders as "done"
        # (the polling loop will stop because the swap arrived).
        return JobPosition(state="done")
    except Exception as exc:
        logger.warning("get_job_position failed: %s", exc)
        return JobPosition(state="missing")


# ---- Failed-job inspection + requeue ----------------------------------
#
# The admin Health page surfaces ``FailedJobRegistry.count`` (the "Failed"
# number). When that's non-zero, the operator needs to know which jobs
# died and why — transient (asyncpg event-loop race, OOM-kill, redis
# blip) or sticky (a specific file's archive consistently crashes the
# matcher; a CV payload edge-case in the parser). The listing below
# resolves each failed job to a human-readable record so a glance is
# enough to tell the two apart and decide between "requeue all" and
# "go fix the file on disk."


@dataclass
class FailedJobRecord:
    """One failed job, shaped for the admin listing.

    ``args_summary`` is the cheap-to-render description of what the
    job was working on: a filename + path for ``match_file_job``,
    ``entity_type/cv_id`` for revalidate jobs, the raw tuple for
    anything else. ``exc_class`` + ``exc_message`` come from the
    first / last lines of the traceback the worker stored on the
    job. ``failed_at`` is the timestamp RQ marked it failed; the
    listing sorts by it descending so the most recent failures land
    at the top.
    """

    job_id: str
    function_name: str
    args_summary: str
    file_id: str | None  # set for match_file_job; lets the template link to /review/{id}
    file_path: str | None  # current on-disk path (or "(missing)" / None)
    exc_class: str
    exc_message: str
    failed_at: str | None  # ISO timestamp string for display
    args_raw: tuple = field(default_factory=tuple)


def _parse_exc_info(exc_info: str | None) -> tuple[str, str]:
    """Pull the exception class + message out of an RQ ``exc_info``
    traceback blob.

    Walking back from the tail finds the most useful line:
    SQLAlchemy errors print a trailing ``(Background on this error
    at: https://sqlalche.me/e/20/...)`` hint after the real message,
    plus ``DETAIL:`` / ``[SQL: ...]`` / ``[parameters: {...}]``
    diagnostic lines. The line that actually identifies the
    failure is the ``ExceptionClass: message`` one — typically of
    the form ``module.path.SomeError: explanatory text``. We scan
    backwards for the first line that matches that shape, ignoring
    the diagnostic lines.

    Defaults to ``("Unknown", "")`` when the blob is empty or no
    classifiable line shows up.
    """
    if not exc_info:
        return ("Unknown", "")
    lines = [ln.rstrip() for ln in exc_info.rstrip().splitlines() if ln.strip()]
    # Diagnostic-line prefixes we skip past when walking back. These
    # appear AFTER the real exception line in SQLAlchemy + asyncpg
    # tracebacks; treating them as the message would surface useless
    # noise on the admin page.
    skip_prefixes = (
        "(Background on this error",
        "DETAIL:",
        "HINT:",
        "CONTEXT:",
        "[SQL:",
        "[parameters:",
        "[generated in",
        "[cached since",
    )
    for line in reversed(lines):
        if line.startswith(skip_prefixes):
            continue
        # The exception line shape is ``module.path.Class: message``.
        # ``module.path`` always contains a dot OR is a bare class
        # name; the colon separates from the message. A line without
        # a colon is almost always a traceback frame ("File ..., line
        # ..., in ...") or similar — keep walking.
        if ":" not in line:
            continue
        cls, _, msg = line.partition(":")
        return (cls.strip(), msg.strip())
    return ("Unknown", "")


async def list_failed_jobs(
    db: AsyncSession, connection: Redis | None = None
) -> list[FailedJobRecord]:
    """Resolve every job currently in the failed-job registry to a
    ``FailedJobRecord``.

    Iterates the FailedJobRegistry ids, fetches each ``Job``, parses
    its exception info, and — for ``match_file_job`` failures — looks
    up the file's current path so the admin can spot a broken
    archive at a glance. Returns most-recent-failure-first.
    """
    conn = connection or Redis.from_url(settings.redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    registry = FailedJobRegistry(queue=queue)
    job_ids = list(registry.get_job_ids())

    # Phase 1: fetch all jobs cheaply (no per-job DB call yet).
    raw: list[tuple[Job, str | None, str | None, str, str, str | None]] = []
    file_ids_to_resolve: set[str] = set()
    for jid in job_ids:
        try:
            job = Job.fetch(jid, connection=conn)
        except NoSuchJobError:
            continue
        function_name = (job.func_name or "").rsplit(".", 1)[-1]
        exc_class, exc_message = _parse_exc_info(job.exc_info)
        failed_at = (
            job.ended_at.isoformat() if job.ended_at is not None else None
        )
        file_id: str | None = None
        args_summary = repr(job.args)
        if function_name == "match_file_job" and job.args:
            file_id = str(job.args[0])
            file_ids_to_resolve.add(file_id)
            args_summary = f"match_file {file_id[:8]}…"
        elif function_name == "revalidate_cv_entity_job" and len(job.args) >= 2:
            args_summary = f"revalidate {job.args[0]} {job.args[1]}"
        raw.append((job, file_id, function_name, args_summary, exc_class, exc_message, failed_at))

    # Phase 2: one batch DB lookup for every file_id at once.
    paths: dict[str, str] = {}
    if file_ids_to_resolve:
        import uuid as _uuid
        # ``file_id`` arrives as str-of-UUID; cast back for the query.
        try:
            ids = [_uuid.UUID(fid) for fid in file_ids_to_resolve]
        except (ValueError, TypeError):
            ids = []
        if ids:
            stmt = (
                select(File.id, FileLocation.path)
                .join(FileLocation, FileLocation.file_id == File.id)
                .where(FileLocation.missing_since.is_(None))
                .where(File.id.in_(ids))
            )
            for fid, path in (await db.execute(stmt)).all():
                # First location wins — duplicates are common (same
                # content at multiple paths); any current path is
                # enough to identify the file on disk.
                paths.setdefault(str(fid), path)

    records: list[FailedJobRecord] = []
    for job, file_id, function_name, args_summary, exc_class, exc_message, failed_at in raw:
        records.append(
            FailedJobRecord(
                job_id=job.id,
                function_name=function_name,
                args_summary=args_summary,
                file_id=file_id,
                file_path=paths.get(file_id) if file_id else None,
                exc_class=exc_class,
                exc_message=exc_message,
                failed_at=failed_at,
                args_raw=tuple(job.args or ()),
            )
        )
    # Sort by failed_at desc, falling back to job_id for stable order
    # when ended_at is missing.
    records.sort(key=lambda r: (r.failed_at or "", r.job_id), reverse=True)
    return records


def requeue_failed_job(job_id: str, connection: Redis | None = None) -> bool:
    """Move one failed job back to the dispatchable queue. Returns
    False when the job no longer exists (registry TTL expired or
    someone else cleared it)."""
    conn = connection or Redis.from_url(settings.redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    registry = FailedJobRegistry(queue=queue)
    try:
        registry.requeue(job_id)
        return True
    except (NoSuchJobError, Exception) as e:
        logger.warning("requeue_failed_job(%s) failed: %s", job_id, e)
        return False


def requeue_all_failed_jobs(connection: Redis | None = None) -> int:
    """Requeue every job currently in the failed registry. Returns
    the count requeued (a registry-shrink race may produce a lower
    number than the initial snapshot)."""
    conn = connection or Redis.from_url(settings.redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    registry = FailedJobRegistry(queue=queue)
    requeued = 0
    for jid in list(registry.get_job_ids()):
        try:
            registry.requeue(jid)
            requeued += 1
        except (NoSuchJobError, Exception) as e:
            logger.warning("requeue %s skipped: %s", jid, e)
    return requeued


def delete_failed_job(job_id: str, connection: Redis | None = None) -> bool:
    """Permanently drop one failed job from the registry. The right
    action when the recorded failure references a function that no
    longer exists in the worker (a deleted job class) or an
    irrecoverable file the operator has already handled out-of-band.

    Returns False when the job is already gone (TTL expired or
    cleared by another caller)."""
    conn = connection or Redis.from_url(settings.redis_url)
    try:
        job = Job.fetch(job_id, connection=conn)
    except NoSuchJobError:
        return False
    try:
        # ``delete`` removes the job's Redis hash and pulls its id
        # out of any registries it lives in. Quiet on success.
        job.delete()
        return True
    except Exception as e:
        logger.warning("delete_failed_job(%s) failed: %s", job_id, e)
        return False


def clear_all_failed_jobs(connection: Redis | None = None) -> int:
    """Drop every job currently in the failed registry. Returns the
    count actually deleted (race-tolerant: jobs cleared by another
    caller mid-iteration don't count and don't error)."""
    conn = connection or Redis.from_url(settings.redis_url)
    queue = Queue(_QUEUE_NAME, connection=conn)
    registry = FailedJobRegistry(queue=queue)
    deleted = 0
    for jid in list(registry.get_job_ids()):
        try:
            job = Job.fetch(jid, connection=conn)
            job.delete()
            deleted += 1
        except NoSuchJobError:
            pass
        except Exception as e:
            logger.warning("delete %s skipped: %s", jid, e)
    return deleted


def _job_ids_for_exc_class(
    exc_class: str, connection: Redis
) -> list[str]:
    """Return every failed-job id whose parsed exception class matches.

    Walks the failed registry directly (no DB hit) — we only need the
    ``exc_info`` blob to bucket the job, which is part of the job's
    Redis hash. Lets the per-class bulk handlers below operate without
    asking the caller to pass a list of ids over the wire (the
    template's grouping is server-side; round-tripping the ids would
    just be lossy duplication).
    """
    queue = Queue(_QUEUE_NAME, connection=connection)
    registry = FailedJobRegistry(queue=queue)
    matching: list[str] = []
    for jid in list(registry.get_job_ids()):
        try:
            job = Job.fetch(jid, connection=connection)
        except NoSuchJobError:
            continue
        cls, _msg = _parse_exc_info(job.exc_info)
        if cls == exc_class:
            matching.append(jid)
    return matching


def requeue_failed_jobs_by_class(
    exc_class: str, connection: Redis | None = None
) -> int:
    """Requeue every failed job whose parsed exception class equals
    ``exc_class``. Returns the count requeued.

    The unit of triage on the admin page is the exception class: a
    sticky bug produces 16 rows that share a class and all need the
    same fate. This is the bulk action for "the bug is fixed, retry
    them all"."""
    conn = connection or Redis.from_url(settings.redis_url)
    ids = _job_ids_for_exc_class(exc_class, conn)
    queue = Queue(_QUEUE_NAME, connection=conn)
    registry = FailedJobRegistry(queue=queue)
    requeued = 0
    for jid in ids:
        try:
            registry.requeue(jid)
            requeued += 1
        except (NoSuchJobError, Exception) as e:
            logger.warning("requeue %s skipped: %s", jid, e)
    return requeued


def clear_failed_jobs_by_class(
    exc_class: str, connection: Redis | None = None
) -> int:
    """Drop every failed job whose parsed exception class equals
    ``exc_class``. Returns the count deleted.

    Companion to ``requeue_failed_jobs_by_class`` — the right action
    when a class of failures references a function the worker no
    longer has (deleted scraper, renamed job) or otherwise can't be
    retried."""
    conn = connection or Redis.from_url(settings.redis_url)
    ids = _job_ids_for_exc_class(exc_class, conn)
    deleted = 0
    for jid in ids:
        try:
            job = Job.fetch(jid, connection=conn)
            job.delete()
            deleted += 1
        except NoSuchJobError:
            pass
        except Exception as e:
            logger.warning("delete %s skipped: %s", jid, e)
    return deleted
