"""Parity tests: stdlib backend vs comicbox backend.

The two backends implement the same ``ArchiveReader`` Protocol; their
behavior should be observationally equivalent on archives both can
read. These tests run synthetic CBZs through both and assert the
results agree on:

  * page count + filename list (same order, same names)
  * ComicInfo.xml presence + raw bytes
  * extracted page bytes for the first page

The tests skip when comicbox isn't installed so the suite stays green
on developer machines that haven't pulled the new dep yet. CI should
have comicbox installed (it's in ``pyproject.toml``), so this still
runs in the canonical environment.

These tests do NOT cover MetronInfo, double-page metadata, or PDF —
those are comicbox-only features by definition, covered separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.archives import open_archive
from tests.fixtures import build_cbz, build_comicinfo_full

# Skip the whole module if comicbox isn't importable. ``importorskip``
# raises ``Skip`` as a side effect when the module is missing, which
# is all we need — we don't reference the returned module object, so
# no assignment.
pytest.importorskip("comicbox")


@pytest.fixture
def both_backends() -> list[str]:
    """Convenience fixture for parameterised iteration in tests that
    construct one archive and read it through both backends."""
    return ["stdlib", "comicbox"]


def _read(path: Path, backend: str):
    """Open ``path`` through the given backend and return a
    ``(pages, comicinfo_bytes, first_page_bytes)`` tuple.

    Helper to keep the per-test assertions terse."""
    reader = open_archive(path, backend=backend)
    pages = reader.list_pages()
    ci = reader.read_comicinfo()
    first = reader.extract_page(pages[0]) if pages else b""
    return pages, ci, first


# ---- Page enumeration ----------------------------------------------------


def test_parity_page_count_and_order(tmp_path: Path, both_backends: list[str]):
    """Both backends report the same number of pages, in the same order."""
    built = build_cbz(tmp_path / "comic.cbz", page_count=5)
    results = {b: _read(built.path, b) for b in both_backends}
    pages_stdlib, _, _ = results["stdlib"]
    pages_comicbox, _, _ = results["comicbox"]
    assert pages_stdlib == pages_comicbox


def test_parity_filters_non_image_entries(tmp_path: Path, both_backends: list[str]):
    """Both backends drop non-image archive entries from ``list_pages()``."""
    built = build_cbz(
        tmp_path / "mixed.cbz",
        page_count=3,
        extra_files={
            "ComicInfo.xml": b"<x/>",
            "cover.txt": b"not a page",
            "readme.md": b"not a page",
            "subdir/note.txt": b"not a page either",
        },
    )
    results = {b: _read(built.path, b) for b in both_backends}
    pages_stdlib, _, _ = results["stdlib"]
    pages_comicbox, _, _ = results["comicbox"]
    assert len(pages_stdlib) == 3
    assert set(pages_stdlib) == set(pages_comicbox)


# ---- ComicInfo.xml -------------------------------------------------------


def test_parity_comicinfo_present(tmp_path: Path, both_backends: list[str]):
    """When ComicInfo.xml exists, both backends return identical bytes."""
    xml = build_comicinfo_full(series="X-Men", number="1", year=1991)
    built = build_cbz(tmp_path / "xmen.cbz", page_count=2, comicinfo=xml)
    results = {b: _read(built.path, b) for b in both_backends}
    _, ci_stdlib, _ = results["stdlib"]
    _, ci_comicbox, _ = results["comicbox"]
    # Raw byte equality — both backends extract the same archive
    # entry. Comicbox parses internally too, but ``read_comicinfo()``
    # specifically returns the raw bytes so the existing
    # ``parse_comicinfo()`` flow keeps working.
    assert ci_stdlib == ci_comicbox
    assert ci_stdlib is not None
    assert b"<Series>X-Men</Series>" in ci_stdlib


def test_parity_comicinfo_absent(tmp_path: Path, both_backends: list[str]):
    """When ComicInfo.xml is missing, both backends return None."""
    built = build_cbz(tmp_path / "no_meta.cbz", page_count=2, comicinfo=None)
    results = {b: _read(built.path, b) for b in both_backends}
    _, ci_stdlib, _ = results["stdlib"]
    _, ci_comicbox, _ = results["comicbox"]
    assert ci_stdlib is None
    assert ci_comicbox is None


# ---- Page extraction -----------------------------------------------------


def test_parity_extract_page_roundtrip(tmp_path: Path, both_backends: list[str]):
    """Page bytes survive the extract path unchanged on both backends."""
    payload = b"DISTINCT-PAGE-CONTENT-1234567890"
    built = build_cbz(tmp_path / "rt.cbz", page_count=1, page_payload=payload)
    results = {b: _read(built.path, b) for b in both_backends}
    _, _, page_stdlib = results["stdlib"]
    _, _, page_comicbox = results["comicbox"]
    assert page_stdlib == payload
    assert page_comicbox == payload
