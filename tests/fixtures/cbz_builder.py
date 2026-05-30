"""Synthetic CBZ builder for tests.

A CBZ is just a ZIP containing image files and an optional ComicInfo.xml.
The scanner doesn't decode page images — it only filters archive entries
by extension — so empty-but-correctly-named files are sufficient stand-ins
for real pages. This keeps fixtures fast (no Pillow, no I/O beyond the zip
itself) and lets us assert on exact byte counts when needed.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuiltCbz:
    """Metadata about a CBZ we created for a test."""

    path: Path
    page_count: int
    has_comicinfo: bool


def build_cbz(
    target: Path,
    *,
    page_count: int = 3,
    page_payload: bytes = b"",  # change to inject distinct content
    comicinfo: str | None = None,
    page_prefix: str = "page",
    extra_files: dict[str, bytes] | None = None,
) -> BuiltCbz:
    """Create a CBZ at ``target`` and return descriptor.

    Parents are created as needed. The file is overwritten if present.

    ``page_payload`` is what gets written to each "page." Default is empty
    bytes; pass distinct content (e.g. b"content-A" vs b"content-B") when
    the test needs the underlying sha256 to differ between two CBZs that
    are otherwise structurally identical.

    ``extra_files`` lets a test stuff additional non-image entries into the
    archive (cover.txt, README, etc.) to exercise the page-extension filter.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(page_count):
            zf.writestr(f"{page_prefix}{i + 1:03d}.jpg", page_payload)
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)
        if extra_files:
            for name, content in extra_files.items():
                zf.writestr(name, content)
    return BuiltCbz(
        path=target,
        page_count=page_count,
        has_comicinfo=comicinfo is not None,
    )


def build_comicinfo_full(
    *,
    series: str = "Test Series",
    number: str = "1",
    year: int = 2023,
    volume: str = "2023",
    cv_issue_id: int = 12345,
) -> str:
    """ComicInfo.xml with a full CV issue URL in <Web> — yields ``full_with_cvid``."""
    web = f"https://comicvine.gamespot.com/test-series/4000-{cv_issue_id}/"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Series>{series}</Series>
  <Number>{number}</Number>
  <Year>{year}</Year>
  <Volume>{volume}</Volume>
  <Web>{web}</Web>
</ComicInfo>"""


def build_comicinfo_partial(
    *,
    series: str = "Test Series",
    number: str = "1",
    year: int = 2023,
    volume: str = "2023",
) -> str:
    """ComicInfo.xml with no <Web> field — yields ``partial`` status."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Series>{series}</Series>
  <Number>{number}</Number>
  <Year>{year}</Year>
  <Volume>{volume}</Volume>
</ComicInfo>"""
