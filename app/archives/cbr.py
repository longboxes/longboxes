"""CBR reader — a CBR is a RAR archive containing image pages and an
optional ComicInfo.xml at the root.

Reads via the ``rarfile`` Python package, which shells out to ``unar`` for
the actual decompression (we ship ``unar`` in the Docker image; see the
Dockerfile). RAR's licensing forbids embedding a decoder, which is why the
external binary dance is the standard pattern.
"""

from __future__ import annotations

from pathlib import Path

import rarfile

from app.archives.base import (
    COMICINFO_NAMES,
    METRONINFO_NAMES,
    PAGE_EXTENSIONS,
    ArchiveError,
)

# Tell rarfile to use ``unar`` rather than auto-detecting an unrar binary.
# This is the only legally-clean GPL backend on Debian. Setting it once at
# import time is fine — rarfile reads this attribute on each open.
rarfile.UNRAR_TOOL = "unar"


class CbrReader:
    """Read pages and ComicInfo.xml from a CBR."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def list_pages(self) -> list[str]:
        try:
            with rarfile.RarFile(self.path) as rf:
                return [
                    info.filename
                    for info in rf.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in PAGE_EXTENSIONS
                ]
        except (rarfile.Error, OSError) as e:
            raise ArchiveError(f"corrupt or unreadable CBR: {self.path}") from e

    def read_comicinfo(self) -> bytes | None:
        return self._read_root_entry(COMICINFO_NAMES)

    def read_metroninfo(self) -> bytes | None:
        return self._read_root_entry(METRONINFO_NAMES)

    def _read_root_entry(self, names: frozenset[str]) -> bytes | None:
        """Root-only entry lookup by basename. Shared by
        ``read_comicinfo`` and ``read_metroninfo`` — same logic as
        the CBZ reader's helper, just over rarfile instead of
        zipfile."""
        try:
            with rarfile.RarFile(self.path) as rf:
                for name in rf.namelist():
                    if Path(name).name in names and "/" not in name.rstrip("/"):
                        return rf.read(name)
                return None
        except (rarfile.Error, OSError) as e:
            raise ArchiveError(f"corrupt or unreadable CBR: {self.path}") from e

    def extract_page(self, name: str) -> bytes:
        try:
            with rarfile.RarFile(self.path) as rf:
                return rf.read(name)
        except (rarfile.Error, OSError, KeyError) as e:
            raise ArchiveError(f"failed to extract {name!r} from {self.path}") from e
