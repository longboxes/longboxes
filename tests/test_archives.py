"""Tests for the archive readers and the dispatcher."""

from pathlib import Path

import pytest

from app.archives import UnsupportedArchiveError, open_archive
from app.archives.base import detect_archive_format
from app.models import ArchiveFormat
from tests.fixtures import build_cbz, build_comicinfo_full

# ---- detect_archive_format ---------------------------------------------


def test_detect_format_known_extensions():
    assert detect_archive_format("foo.cbz") is ArchiveFormat.CBZ
    assert detect_archive_format("foo.CBZ") is ArchiveFormat.CBZ  # case-insensitive
    assert detect_archive_format("foo.zip") is ArchiveFormat.CBZ
    assert detect_archive_format("foo.cbr") is ArchiveFormat.CBR
    assert detect_archive_format("foo.rar") is ArchiveFormat.CBR
    assert detect_archive_format("foo.cb7") is ArchiveFormat.CB7
    assert detect_archive_format("foo.7z") is ArchiveFormat.CB7
    assert detect_archive_format("foo.pdf") is ArchiveFormat.PDF


def test_detect_format_unknown_returns_none():
    assert detect_archive_format("foo.txt") is None
    assert detect_archive_format("noext") is None
    assert detect_archive_format("foo.cbz.bak") is None  # double extension


# ---- open_archive dispatch ----------------------------------------------


def test_open_archive_cbz(tmp_path: Path):
    built = build_cbz(tmp_path / "comic.cbz", page_count=2)
    reader = open_archive(built.path, backend="stdlib")
    pages = reader.list_pages()
    assert len(pages) == 2
    assert all(p.endswith(".jpg") for p in pages)


def test_open_archive_unsupported_extension(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("hello")
    with pytest.raises(UnsupportedArchiveError):
        open_archive(target, backend="stdlib")


def test_open_archive_pdf_stdlib_unsupported(tmp_path: Path):
    """The stdlib backend still refuses PDFs (no reader for that
    format on this path). The comicbox backend handles PDFs when
    the ``comicbox[pdf]`` extra is installed — covered separately
    in the parity test suite."""
    target = tmp_path / "fake.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(UnsupportedArchiveError):
        open_archive(target, backend="stdlib")


# ---- CBZ reader behaviour ----------------------------------------------


def test_cbz_list_pages_filters_by_extension(tmp_path: Path):
    built = build_cbz(
        tmp_path / "mixed.cbz",
        page_count=3,
        extra_files={
            "ComicInfo.xml": b"<x/>",  # XML
            "cover.txt": b"not a page",
            "readme.md": b"not a page",
        },
    )
    reader = open_archive(built.path, backend="stdlib")
    pages = reader.list_pages()
    assert len(pages) == 3


def test_cbz_list_pages_preserves_order(tmp_path: Path):
    built = build_cbz(tmp_path / "ordered.cbz", page_count=5)
    reader = open_archive(built.path, backend="stdlib")
    pages = reader.list_pages()
    assert pages == [f"page{i:03d}.jpg" for i in range(1, 6)]


def test_cbz_read_comicinfo_present(tmp_path: Path):
    xml = build_comicinfo_full(series="X-Men", number="1", year=1991)
    built = build_cbz(tmp_path / "xmen.cbz", page_count=2, comicinfo=xml)
    reader = open_archive(built.path, backend="stdlib")
    raw = reader.read_comicinfo()
    assert raw is not None
    assert b"<Series>X-Men</Series>" in raw


def test_cbz_read_comicinfo_absent(tmp_path: Path):
    built = build_cbz(tmp_path / "no_meta.cbz", page_count=2, comicinfo=None)
    reader = open_archive(built.path, backend="stdlib")
    assert reader.read_comicinfo() is None


def test_cbz_extract_page_roundtrip(tmp_path: Path):
    built = build_cbz(tmp_path / "rt.cbz", page_count=1, page_payload=b"PAGE-CONTENT")
    reader = open_archive(built.path, backend="stdlib")
    pages = reader.list_pages()
    assert reader.extract_page(pages[0]) == b"PAGE-CONTENT"
