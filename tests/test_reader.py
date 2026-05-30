"""Tests for the reader's page extraction (Phase 6).

Covers the pure archive-extraction helpers behind the page-serving
endpoint — building a CBZ and pulling individual pages back out, the
same path ``GET /read/{file_id}/page/{n}`` takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.archives.base import ArchiveError
from app.reader.routes import _page_count, _read_page
from tests.fixtures import build_cbz, make_image_bytes


@pytest.mark.parametrize("backend", ["comicbox", "stdlib"])
def test_read_page_extracts_each_page(tmp_path: Path, backend: str):
    cbz = tmp_path / "comic.cbz"
    build_cbz(cbz, page_count=3, page_payload=make_image_bytes(800, 1200))

    assert _page_count(str(cbz), backend) == 3
    for index in range(3):
        data, content_type = _read_page(str(cbz), backend, index)
        assert data  # non-empty image bytes
        assert content_type.startswith("image/")


@pytest.mark.parametrize("backend", ["comicbox", "stdlib"])
def test_read_page_out_of_range_raises(tmp_path: Path, backend: str):
    cbz = tmp_path / "comic.cbz"
    build_cbz(cbz, page_count=2, page_payload=make_image_bytes(800, 1200))

    # Past the last page, and before the first — both out of range, so
    # the endpoint can map either to a 404.
    with pytest.raises(ArchiveError):
        _read_page(str(cbz), backend, 2)
    with pytest.raises(ArchiveError):
        _read_page(str(cbz), backend, -1)
