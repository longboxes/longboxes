"""A no-op job used to verify the queue is wired up end-to-end.

Exercised via the ``just test-job`` recipe in the project's
``justfile``. The recipe enqueues this function on Redis from the
web container; a running worker picks it up and logs ``noop job
ran: ...``. Useful when bringing up a fresh deployment or
debugging a worker that isn't draining the queue.

Production code never calls this — only the diagnostic recipe and
any operator who copies the same invocation by hand. Keeping it
plus the entry in ``app/jobs/__init__.py``'s ``__all__`` is
deliberate: removing either breaks the diagnostic without obvious
warning."""

import logging

logger = logging.getLogger("longboxes.jobs.noop")


def noop(message: str = "hello from a job") -> dict:
    logger.info("noop job ran: %s", message)
    return {"message": message, "status": "completed"}
