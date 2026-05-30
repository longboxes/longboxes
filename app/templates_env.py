"""Jinja2 templates wrapper.

Centralised here so routes don't each construct their own ``Jinja2Templates``
instance and so tests can swap directories if they ever need to.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.cv_helpers import cv_image_url, cv_issue_url, cv_volume_url

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@cache
def static_url(name: str) -> str:
    """Return ``/static/<name>?v=<short-hash>`` so a deployed change to
    an asset invalidates browser caches automatically.

    The hash is the first 8 chars of an MD5 over the file's bytes,
    cached for the lifetime of the process. That makes the cost a
    one-time file read at first reference, and ties cache busting to
    ``web`` container restarts (which is exactly when you want it to
    bite). Missing files fall back to the bare URL — a 404 on the
    asset is a more diagnosable failure than a missing tag.

    Used from templates as ``{{ static_url('auto-refresh.js') }}``.
    """
    path = STATIC_DIR / name
    try:
        # ``usedforsecurity=False`` is purely a hint to FIPS-mode
        # OpenSSL; MD5 is fine for cache-busting (collision-free for
        # a small set of asset filenames).
        digest = hashlib.md5(
            path.read_bytes(), usedforsecurity=False
        ).hexdigest()[:8]
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={digest}"


# Globals available to every template — keeps reusable components like
# ``_publisher_chip.html`` self-contained: they take a CV entity and pull
# the image URL themselves instead of relying on every route to compute
# and pass each variant.
templates.env.globals["cv_image_url"] = cv_image_url
templates.env.globals["cv_volume_url"] = cv_volume_url
templates.env.globals["cv_issue_url"] = cv_issue_url
templates.env.globals["static_url"] = static_url
