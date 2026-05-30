"""Archive readers — uniform interface over CBZ / CBR / CB7 / PDF.

Public API:
    ``open_archive(path, backend="comicbox") -> ArchiveReader`` —
        dispatches on backend + extension. ``backend`` is the admin
        setting; ``"stdlib"`` keeps the zipfile/rarfile-only path
        for fallback.
    ``ArchiveReader`` — Protocol implemented by Cbz/Cbr/Comicbox readers.
    ``ArchiveError`` — base exception for readable archive problems.
    ``ComicInfoExtract`` — parsed ComicInfo / MetronInfo fields.
    ``parse_comicinfo(xml_bytes)`` — ComicInfo.xml parser.
    ``parse_metroninfo(xml_bytes)`` — MetronInfo.xml parser. Same
        output dataclass; matcher consumes either.
"""

from app.archives.base import (
    ArchiveError,
    ArchiveReader,
    UnsupportedArchiveError,
    detect_archive_format,
    open_archive,
)
from app.archives.cbr import CbrReader
from app.archives.cbz import CbzReader
from app.archives.comicinfo import ComicInfoExtract, parse_comicinfo
from app.archives.metroninfo import parse_metroninfo

__all__ = [
    "ArchiveError",
    "ArchiveReader",
    "CbrReader",
    "CbzReader",
    "ComicInfoExtract",
    "UnsupportedArchiveError",
    "detect_archive_format",
    "open_archive",
    "parse_comicinfo",
    "parse_metroninfo",
]
