"""RQ background jobs. Jobs are plain functions discoverable by dotted path."""

from app.jobs.noop import noop

__all__ = ["noop"]
