"""Rewrite ComicVine's relative URLs in cached description / deck HTML.

ComicVine's API returns description text with anchor links pointing
inside ComicVine's own site, like
``<a href="/avengers-academy/4050-33633/">Avengers Academy</a>``.
The first path segment is a display slug (we ignore it); the second
is ``<type-prefix>-<cv_id>``. Type prefixes identify the resource:

    4000 = issue        4005 = character    4010 = publisher
    4040 = person       4045 = story_arc    4050 = volume
    4060 = team

ComicVine has further prefixes for locations / concepts / objects /
videos / origins / etc. — Longboxes doesn't render its own pages
for those, so we keep them as external links to comicvine.gamespot.com
instead of breaking them.

This module rewrites such URLs at cache-write time, so the link
behaviour is correct for the entire app without any template-level
work:

* Known internal types are mapped to Longboxes routes — a 4050 link
  becomes ``/volume/{cv_id}``, a 4005 link becomes
  ``/character/{cv_id}``, etc.
* Unknown / explicitly-external types are absolutized to
  ``https://comicvine.gamespot.com<original-path>`` so the link still
  works.
* href values that don't match the CV pattern (mailto:, anchors,
  already-absolute http(s) URLs) are left untouched.

The rewriter is applied uniformly to ``raw_payload["description"]``
and ``raw_payload["deck"]`` in every ``_upsert_*`` path in
``app/comicvine/cache.py``. It's also exposed via
``app/scripts/backfill_cv_descriptions.py`` for backfilling rows
cached before this feature shipped.

Operates only on the ``href`` attribute. Idempotent: applying the
rewrite to an already-rewritten string is a no-op (the second pass's
patterns no longer match — Longboxes routes have no
``-<digits>`` separator).
"""

from __future__ import annotations

import re

# Map ComicVine resource type prefixes to Longboxes path templates.
# Prefixes not in this dict are treated as external (absolutized to
# comicvine.gamespot.com instead of routed into Longboxes).
#
# Prefix values are documented inline in ``app/comicvine/client.py``
# (around line 96 — same numbers used by the ComicVine client's
# resource-type detection for ComicInfo Web URLs).
_INTERNAL_PATH_BY_TYPE_PREFIX: dict[str, str] = {
    "4000": "/issue/{cv_id}",
    "4005": "/character/{cv_id}",
    "4010": "/publisher/{cv_id}",
    "4040": "/creator/{cv_id}",
    "4045": "/arc/{cv_id}",
    "4050": "/volume/{cv_id}",
    "4060": "/team/{cv_id}",
}

# Used to absolutize external-type and unknown-prefix links so they
# still work when clicked, instead of pointing at a 404 on the
# Longboxes server.
_CV_BASE = "https://comicvine.gamespot.com"

# Match an href="..." (or href='...') attribute whose value is a
# ComicVine-style relative resource path:
#
#    /<slug>/<4-digit-prefix>-<cv_id>/<optional ?query or #fragment>
#
# The leading slash is required, which rules out mailto:, http(s)://,
# bare #anchors, and protocol-relative ``//host/path``. The slug
# accepts any non-slash, non-quote chars. Trailing slash optional;
# query/fragment optional. We use re.IGNORECASE so a sloppy CV payload
# with ``HREF=`` still gets caught.
_HREF_PATTERN = re.compile(
    r"""
    (href \s* = \s*)              # group 1: ``href=`` + any spacing/case
    (["'])                        # group 2: opening quote
    (                             # group 3: the whole URL (we replace this)
      /                           #   leading slash
      [^"'/?\#]+                  #   slug — non-slash, non-quote chars
      /                           #   slash before the prefix-id segment
      (\d{4})                     #   group 4: 4-digit type prefix
      -                           #
      (\d+)                       #   group 5: cv_id (one or more digits)
      /?                          #   optional trailing slash
      (?:[?\#][^"']*)?            #   optional query/fragment
    )
    \2                            # closing quote (must match opener)
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def rewrite_cv_description(html: str | None) -> str | None:
    """Rewrite ComicVine-relative anchor URLs in ``html``.

    Known internal-type links route to their Longboxes detail page.
    Unknown/external-type links are absolutized to
    ``https://comicvine.gamespot.com``. Non-matching href values
    (mailto, anchors, already-absolute URLs) are untouched.

    Returns ``None`` for ``None`` input, ``""`` for empty string,
    and the rewritten string otherwise. Idempotent.
    """
    if html is None:
        return None
    if not html:
        return html

    def _rewrite(match: re.Match[str]) -> str:
        prefix_attr, quote, original_path, type_prefix, cv_id = match.groups()
        target = _INTERNAL_PATH_BY_TYPE_PREFIX.get(type_prefix)
        if target is not None:
            new_href = target.format(cv_id=cv_id)
        else:
            # Unknown / explicitly-external type — keep the original
            # path so anchors / queries survive, just bolt the CV
            # base URL on the front.
            new_href = f"{_CV_BASE}{original_path}"
        return f"{prefix_attr}{quote}{new_href}{quote}"

    return _HREF_PATTERN.sub(_rewrite, html)


def rewrite_payload_descriptions(payload: dict | None) -> dict | None:
    """Apply ``rewrite_cv_description`` to ``payload["description"]``
    and ``payload["deck"]`` if present.

    Mutates and returns ``payload`` for caller convenience. A non-dict
    or ``None`` input is returned untouched so this is safe to drop
    into any upsert path without first checking the payload shape.
    """
    if not isinstance(payload, dict):
        return payload
    if "description" in payload:
        payload["description"] = rewrite_cv_description(payload["description"])
    if "deck" in payload:
        payload["deck"] = rewrite_cv_description(payload["deck"])
    return payload
