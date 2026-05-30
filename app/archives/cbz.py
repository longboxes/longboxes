"""CBZ reader — a CBZ is a ZIP file containing image pages and an optional
ComicInfo.xml at the root."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.archives.base import (
    COMICINFO_NAMES,
    METRONINFO_NAMES,
    PAGE_EXTENSIONS,
    ArchiveError,
)


class CbzReader:
    """Read pages and ComicInfo.xml from a CBZ.

    Opens the archive lazily on each call — the scanner only needs one or
    two calls per file (list_pages + read_comicinfo), so persistent handles
    would just complicate cleanup. The reader endpoint, which makes many
    extract_page calls, can wrap an instance with an LRU.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def list_pages(self) -> list[str]:
        try:
            with zipfile.ZipFile(self.path) as zf:
                return [
                    info.filename
                    for info in zf.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in PAGE_EXTENSIONS
                ]
        except zipfile.BadZipFile as e:
            raise ArchiveError(f"corrupt CBZ: {self.path}") from e

    def read_comicinfo(self) -> bytes | None:
        return self._read_root_entry(COMICINFO_NAMES)

    def read_metroninfo(self) -> bytes | None:
        return self._read_root_entry(METRONINFO_NAMES)

    def _read_root_entry(self, names: frozenset[str]) -> bytes | None:
        """Return the bytes of a root-level archive entry whose basename
        matches one of ``names``. Used by both ``read_comicinfo`` and
        ``read_metroninfo`` — same matching rules, just different filename
        set. Root-only (no subdir descent) by design: if a publisher buries
        their metadata in a subfolder, that's a problem with their archive,
        not ours."""
        try:
            with zipfile.ZipFile(self.path) as zf:
                for name in zf.namelist():
                    if Path(name).name in names and "/" not in name.rstrip("/"):
                        return zf.read(name)
                return None
        except zipfile.BadZipFile as e:
            raise ArchiveError(f"corrupt CBZ: {self.path}") from e

    def extract_page(self, name: str) -> bytes:
        try:
            with zipfile.ZipFile(self.path) as zf:
                return zf.read(name)
        except (zipfile.BadZipFile, KeyError) as e:
            raise ArchiveError(f"failed to extract {name!r} from {self.path}") from e
