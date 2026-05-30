"""Real image bytes for tests that exercise cover inspection.

The CBZ builder deliberately uses empty page payloads (no Pillow) for
speed. Cover-image tests, by contrast, need genuine decodable images —
this module generates them on demand.
"""

from __future__ import annotations

import io

from PIL import Image


def make_image_bytes(
    width: int,
    height: int,
    *,
    fmt: str = "JPEG",
    color: tuple[int, int, int] = (40, 90, 160),
    left_color: tuple[int, int, int] | None = None,
) -> bytes:
    """Return encoded image bytes of the given pixel size.

    ``color`` fills the whole image. ``left_color``, when given,
    repaints the left half a different colour — used to assert that a
    wraparound crop keeps the right (front-cover) half.
    """
    img = Image.new("RGB", (width, height), color)
    if left_color is not None and width >= 2:
        left = Image.new("RGB", (width // 2, height), left_color)
        img.paste(left, (0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()
