"""Library browse routes — /library, /volume/{id}, /issue/{id}.

This is the first phase that ships visible UI for end users (Phase 5 in
the design doc). Earlier phases produced data in tables; this one renders it.
"""

from app.library_browse.routes import router

__all__ = ["router"]
