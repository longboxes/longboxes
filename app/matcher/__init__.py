"""Matcher pipeline — links scanned files to ComicVine issues.

Public API:
    ``match_file(file_id, db, cv_cache, comicinfo) -> MatchResult``
        Async coroutine. Runs the four-stage pipeline from §10 of the
        design doc and writes a ``file_matches`` row.

    ``ParsedFilename``
        Result of ``parse_filename(name)``. The matcher uses it as Stage 2
        input; the review UI (Phase 7) displays it for human context.
"""

from app.matcher.filename import ParsedFilename, parse_filename
from app.matcher.pipeline import MatchResult, match_file

__all__ = [
    "MatchResult",
    "ParsedFilename",
    "match_file",
    "parse_filename",
]
