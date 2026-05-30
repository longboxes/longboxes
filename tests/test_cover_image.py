"""Tests for double-wide / wraparound cover detection + cropping.

Two layers:
  * ``inspect_cover`` / ``crop_to_front`` — pure Pillow logic.
  * ``_extract_cover_cached`` — the review cover endpoint's extractor,
    end-to-end through an archive reader, confirming a wraparound is
    cropped to the front cover before it's served.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.archives.cover_image import (
    WRAPAROUND_ASPECT_THRESHOLD,
    CoverInspection,
    crop_to_front,
    inspect_cover,
)
from tests.fixtures import build_cbz, make_image_bytes

# ---- inspect_cover ------------------------------------------------------


def test_inspect_portrait_cover_is_not_wraparound():
    # ratio 0.67 — a standard single comic cover.
    insp = inspect_cover(make_image_bytes(400, 600))
    assert insp is not None
    assert (insp.width, insp.height) == (400, 600)
    assert insp.is_wraparound is False


def test_inspect_double_wide_cover_is_wraparound():
    # ratio 2.0 — wide enough to read as a genuine double-wide image.
    insp = inspect_cover(make_image_bytes(1200, 600))
    assert insp is not None
    assert (insp.width, insp.height) == (1200, 600)
    assert insp.is_wraparound is True


def test_inspect_square_cover_is_not_wraparound():
    # ratio 1.0 — well under the near-2:1 double-wide threshold; a
    # square oddity is left whole rather than cropped.
    insp = inspect_cover(make_image_bytes(600, 600))
    assert insp is not None
    assert insp.is_wraparound is False


def test_inspect_landscape_cover_is_not_wraparound():
    # ratio 1.5 — a wide landscape cover (common for digital comics).
    # Below the near-2:1 threshold, so it's NOT treated as double-wide:
    # it's the whole cover and must be served whole.
    insp = inspect_cover(make_image_bytes(1500, 1000))
    assert insp is not None
    assert insp.is_wraparound is False


def test_inspect_non_image_bytes_returns_none():
    assert inspect_cover(b"") is None
    assert inspect_cover(b"not an image at all") is None


def test_inspection_aspect_ratio_handles_zero_height():
    degenerate = CoverInspection(width=100, height=0)
    assert degenerate.aspect_ratio == 0.0
    assert degenerate.is_wraparound is False


# ---- crop_to_front ------------------------------------------------------


def test_crop_returns_none_for_normal_portrait_cover():
    # Not a wraparound → None signals "serve the original untouched".
    assert crop_to_front(make_image_bytes(400, 600)) is None


def test_crop_leaves_landscape_cover_whole():
    # A wide landscape cover that isn't near-2:1 is the whole cover —
    # crop_to_front returns None so the original is served intact.
    assert crop_to_front(make_image_bytes(1500, 1000)) is None


def test_crop_returns_none_for_non_image():
    assert crop_to_front(b"garbage bytes") is None


def test_crop_double_wide_keeps_the_front_half():
    # Left half red (back cover), right half blue (front cover).
    red, blue = (200, 30, 30), (30, 30, 200)
    data = make_image_bytes(1200, 600, color=blue, left_color=red)
    cropped = crop_to_front(data)
    assert cropped is not None
    with Image.open(io.BytesIO(cropped)) as im:
        assert im.width == 1200 - 1200 // 2  # right half
        assert im.height == 600
        r, g, b = im.convert("RGB").getpixel((im.width // 2, im.height // 2))
    # The kept half is the front cover — blue dominates.
    assert b > r and b > g


def test_crop_result_is_no_longer_a_wraparound():
    cropped = crop_to_front(make_image_bytes(1200, 600))
    assert cropped is not None
    reinspected = inspect_cover(cropped)
    assert reinspected is not None
    assert reinspected.is_wraparound is False


def test_threshold_boundary():
    h = 1000
    just_under = make_image_bytes(int(h * (WRAPAROUND_ASPECT_THRESHOLD - 0.05)), h)
    just_over = make_image_bytes(int(h * (WRAPAROUND_ASPECT_THRESHOLD + 0.05)), h)
    assert inspect_cover(just_under).is_wraparound is False
    assert inspect_cover(just_over).is_wraparound is True
    assert crop_to_front(just_under) is None
    assert crop_to_front(just_over) is not None


# ---- _extract_cover_cached (cover endpoint extractor) -------------------


@pytest.mark.parametrize("backend", ["comicbox", "stdlib"])
def test_extract_cover_crops_wraparound(tmp_path: Path, backend: str):
    """A CBZ whose cover page is a double-wide image comes back from
    the endpoint extractor cropped to a portrait front cover, with the
    original (pre-crop) geometry reported in the inspection."""
    from app.review.routes import _extract_cover_cached

    wide = make_image_bytes(1950, 1000)  # ratio 1.95 — near-2:1 double-wide
    cbz = tmp_path / "wrap.cbz"
    build_cbz(cbz, page_count=2, page_payload=wide)

    data, content_type, inspection = _extract_cover_cached(str(cbz), backend)

    assert content_type == "image/jpeg"
    # Inspection carries the ORIGINAL geometry, pre-crop.
    assert inspection is not None
    assert (inspection.width, inspection.height) == (1950, 1000)
    assert inspection.is_wraparound is True
    # The served bytes are the cropped front cover — now portrait.
    with Image.open(io.BytesIO(data)) as im:
        assert im.width < im.height


@pytest.mark.parametrize("backend", ["comicbox", "stdlib"])
def test_extract_cover_passes_through_normal_cover(tmp_path: Path, backend: str):
    """A normal portrait cover is served unchanged — no crop."""
    from app.review.routes import _extract_cover_cached

    portrait = make_image_bytes(800, 1200)
    cbz = tmp_path / "normal.cbz"
    build_cbz(cbz, page_count=2, page_payload=portrait)

    data, _content_type, inspection = _extract_cover_cached(str(cbz), backend)

    assert data == portrait  # untouched
    assert inspection is not None
    assert inspection.is_wraparound is False
