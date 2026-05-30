"""Cover-image inspection — double-wide cover handling.

A few cover images come out wider than a normal portrait comic cover:

  * A back+front *wraparound*, or a double-page spread saved as page 1,
    is genuinely two pages side by side (~2:1). Only its right half is
    the front cover (Western trade dress runs back-left / front-right).
  * A *landscape* cover — common for digital-first comics — is the
    whole cover; it's simply wide.

Both would otherwise be center-cropped by a 2:3 ``object-cover`` card
into a useless sliver. They need opposite fixes, and a wraparound and a
landscape cover overlap in proportions — so only an unambiguous
near-2:1 (or wider) image is treated as double-wide here. A merely
wider-than-tall cover is left whole for the display layer to letterbox.

This module does two cheap, Pillow-only things — no archive knowledge:

  * ``inspect_cover`` — read the image header and report its pixel
    dimensions plus whether the aspect ratio reads as double-wide.
    The scanner persists this on the ``files`` row.
  * ``crop_to_front`` — when an image is double-wide, decode it, crop
    to the right-half front cover, and re-encode as JPEG. The review
    cover endpoint runs file covers through this before streaming.

ComicVine-hosted covers aren't run through ``crop_to_front`` — CV's
curated main ``image`` is almost always a clean front cover already.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger("longboxes.archives.cover_image")

# Aspect ratio (width / height) above which a cover image is cropped to
# its right-half front cover instead of being served whole.
#
# Wider-than-tall cover images come in two flavours that need opposite
# handling:
#
#   * A landscape cover — common for digital-first comics — IS the whole
#     cover; it just happens to be wide (~1.1-1.8). Cropping it to a
#     half throws away real art. It must be served whole; the cover-
#     display layer letterboxes it into the 2:3 card.
#   * A genuine double-wide image — a back+front wraparound, or a
#     double-page spread used as page 1 — is two pages side by side.
#     Only its right half is the front cover worth showing.
#
# The two overlap in the ~1.3 range and can't be told apart by size
# alone, so the threshold is deliberately set high: only a near-2:1
# (or wider) image is treated as double-wide and right-half-cropped.
# Everything merely wider-than-tall is left whole — the safe choice,
# since the display layer handles the shape.
WRAPAROUND_ASPECT_THRESHOLD = 1.9

# JPEG quality for the re-encoded front-cover crop. Covers are
# photographic / painted art; 88 is visually lossless at cover-card
# sizes and keeps the cropped bytes small.
_CROP_JPEG_QUALITY = 88


@dataclass(frozen=True)
class CoverInspection:
    """Pixel geometry of a cover image + whether it reads as wraparound.

    ``width`` / ``height`` are the dimensions of the *original* image,
    before any front-cover crop. ``is_wraparound`` is derived from the
    aspect ratio — see ``WRAPAROUND_ASPECT_THRESHOLD``.
    """

    width: int
    height: int

    @property
    def aspect_ratio(self) -> float:
        """Width / height. 0.0 for a degenerate zero-height image."""
        return self.width / self.height if self.height > 0 else 0.0

    @property
    def is_wraparound(self) -> bool:
        """True when the image is wide enough to read as a double-wide cover."""
        return self.aspect_ratio > WRAPAROUND_ASPECT_THRESHOLD


def inspect_cover(data: bytes) -> CoverInspection | None:
    """Return a ``CoverInspection`` for raw image ``data``, or None.

    Reads only the image header — Pillow's ``Image.open`` is lazy, so
    ``.size`` doesn't decode pixels — which keeps this cheap enough to
    run for every file during a scan. Returns None when ``data`` isn't
    a decodable image (an empty page, a corrupt entry, a non-image
    first page); the caller leaves the cover columns null.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return CoverInspection(width=int(width), height=int(height))


def crop_to_front(data: bytes) -> bytes | None:
    """Crop a double-wide cover to its front cover, returned as JPEG.

    Returns cropped JPEG bytes when ``data`` is a wraparound (aspect
    ratio over ``WRAPAROUND_ASPECT_THRESHOLD``). Returns None otherwise
    — not a wraparound, or not a decodable image — signalling the
    caller to serve the original bytes unchanged.

    The front cover is the right half: Western wraparound trade dress
    runs back-cover-left, front-cover-right, and the split is the exact
    horizontal midpoint.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if height <= 0 or width / height <= WRAPAROUND_ASPECT_THRESHOLD:
                return None
            # Right half = front cover. ``width // 2`` as the left edge
            # keeps the right ceil(width/2) pixels — fine for an odd
            # width; the spine sits on the midpoint either way.
            front = im.crop((width // 2, 0, width, height))
            # JPEG can't encode alpha / palette modes — normalise first.
            if front.mode not in ("RGB", "L"):
                front = front.convert("RGB")
            buf = io.BytesIO()
            front.save(buf, format="JPEG", quality=_CROP_JPEG_QUALITY)
            return buf.getvalue()
    except Exception as e:
        # A decompression bomb, a truncated image, an unsupported mode
        # — none of it should break cover display. Serve the original.
        logger.warning("crop_to_front failed: %s: %s", type(e).__name__, e)
        return None
