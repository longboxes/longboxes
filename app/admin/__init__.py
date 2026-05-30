"""Admin-facing routes — settings, users, library paths, rescan, health.

Phase 2 ships only the minimum needed to drive the scanner: a placeholder
admin home, the rescan button, and a quick view of currently-configured
library paths. Later phases (5, 8, 9) flesh this out with user management,
TTL tuning, and the full library health report.
"""

from app.admin.routes import router

__all__ = ["router"]
