"""Archive Protocol + dispatcher + shared helpers.

The Protocol intentionally exposes only what the scanner and reader actually
need: list pages, read optional ComicInfo.xml, extract a single page on
demand. Per §9, this is enough to support CBZ/CBR/CB7 with no per-format
branching in the scanner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.models import ArchiveFormat

# Image extensions we count as "pages" (per §9 page-count step).
PAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
)

# ComicInfo.xml is always at the root of the archive, exact case. Some
# scanlator archives use lowercase or alternate spellings; we recognise the
# canonical variants only — bogus ones are treated as "no ComicInfo," which
# downgrades the file's status to ``partial`` or ``none`` but does not fail.
COMICINFO_NAMES: frozenset[str] = frozenset({"ComicInfo.xml"})

# MetronInfo.xml is the newer Metron-Project schema with stricter typing
# and proper identifier resources. Lives at the same root location as
# ComicInfo.xml; an archive can carry one, the other, or both. The
# matcher prefers ComicInfo when present (its CV-ID field is the gold-
# standard hint) and falls back to MetronInfo otherwise.
METRONINFO_NAMES: frozenset[str] = frozenset({"MetronInfo.xml"})


class ArchiveError(Exception):
    """Raised when an archive is unreadable, corrupt, or password-protected.

    The scanner catches this, logs, and moves on — a single bad file
    shouldn't poison a library scan.
    """


class UnsupportedArchiveError(ArchiveError):
    """Raised when the file extension isn't one we know how to read."""


@runtime_checkable
class ArchiveReader(Protocol):
    """Minimal interface every concrete reader implements."""

    path: Path

    def list_pages(self) -> list[str]:
        """Return image-page filenames inside the archive, in archive order.

        Filtered to ``PAGE_EXTENSIONS``. Order matters for the reader; CBZ
        readers should preserve the zip entry order, which by convention
        matches the page order publishers/scanlators set.
        """

    def read_comicinfo(self) -> bytes | None:
        """Return raw ComicInfo.xml bytes if present, else None."""

    def read_metroninfo(self) -> bytes | None:
        """Return raw MetronInfo.xml bytes if present, else None.

        Symmetric to ``read_comicinfo``. The matcher uses MetronInfo
        as a fallback when ComicInfo is absent; either schema can
        supply series / number / year / CV-ID hints."""

    def extract_page(self, name: str) -> bytes:
        """Return raw image bytes for the named page entry."""


def detect_archive_format(path: Path | str) -> ArchiveFormat | None:
    """Map a file extension to its archive format. Returns None for unknowns.

    We intentionally key off extension rather than content sniffing — the
    scanner sees thousands of files per pass and content sniffing each one
    would mean an open()+read() that the fast path is supposed to avoid.
    """
    suffix = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "cbz": ArchiveFormat.CBZ,
        "zip": ArchiveFormat.CBZ,  # legacy/manual users sometimes use .zip
        "cbr": ArchiveFormat.CBR,
        "rar": ArchiveFormat.CBR,
        "cb7": ArchiveFormat.CB7,
        "7z": ArchiveFormat.CB7,
        "pdf": ArchiveFormat.PDF,
    }
    return mapping.get(suffix)


def open_archive(
    path: Path | str,
    *,
    backend: str = "comicbox",
) -> ArchiveReader:
    """Dispatch on archive backend (comicbox vs stdlib) and file extension.

    ``backend`` is the ``archive_backend`` admin setting fetched by the
    caller. Two values are honored:

      * ``"comicbox"`` (default) — one reader (``ComicboxReader``)
        handles every format comicbox supports (CBZ, CBR, CBT, PDF).
        Proper page sort order, MetronInfo extraction, double-page
        metadata, and cover-page detection come along for free.
      * ``"stdlib"`` — the original ``zipfile``/``rarfile`` readers
        in ``cbz.py`` / ``cbr.py``. Kept as a fallback path so admins
        can flip back via /admin if comicbox surprises us on a
        specific archive. CB7 + PDF remain unsupported on this path.

    Unknown ``backend`` values fall through to the comicbox path so a
    bogus setting can't break the app — settings.py's ``get_archive_
    backend()`` accessor already coerces to the default, but the
    extra guard here keeps direct callers (tests) honest.

    Raises ``UnsupportedArchiveError`` for extensions we don't handle.
    With the stdlib backend, CB7 and PDF stay unsupported; with the
    comicbox backend, they route through ``ComicboxReader`` which has
    its own per-format support story (PDF is gated on the
    ``comicbox[pdf]`` extra install).
    """
    # Late imports keep the dispatcher cheap and avoid circular imports
    # if a reader ever wants to reuse helpers from this module.
    fmt = detect_archive_format(path)
    if fmt is None:
        raise UnsupportedArchiveError(f"unknown archive extension: {path!r}")

    if backend == "stdlib":
        from app.archives.cbr import CbrReader
        from app.archives.cbz import CbzReader

        if fmt is ArchiveFormat.CBZ:
            return CbzReader(Path(path))
        if fmt is ArchiveFormat.CBR:
            return CbrReader(Path(path))
        if fmt in (ArchiveFormat.CB7, ArchiveFormat.PDF):
            raise UnsupportedArchiveError(
                f"{fmt.value} archives are recognised but not yet supported "
                f"on the stdlib backend (switch to 'comicbox' to enable)"
            )
        raise UnsupportedArchiveError(f"unknown archive extension: {path!r}")

    # Default: comicbox backend. One reader handles every supported
    # format; CB7 / PDF stop raising UnsupportedArchiveError here.
    from app.archives.comicbox_reader import ComicboxReader

    return ComicboxReader(Path(path))


def resolve_cover_page_name(reader: ArchiveReader, *, pages: list[str] | None = None) -> str | None:
    """Return the archive entry name of ``reader``'s cover page.

    Prefers a comicbox-backed reader's ``cover_filename()`` — which
    honours ComicInfo ``<Page Type="FrontCover">`` — and falls back to
    the first page in archive order. Returns None for an empty archive.

    ``pages`` lets a caller that has already listed the archive's
    pages pass them in to avoid a redundant ``list_pages()`` call (one
    extra archive open on the comicbox backend). When omitted, the
    fallback path lists pages itself.

    Shared by the scanner (cover inspection during a scan) and the
    review cover endpoint so the two agree on which page is "the"
    cover.
    """
    cover_fn = getattr(reader, "cover_filename", None)
    if callable(cover_fn):
        try:
            name = cover_fn()
        except ArchiveError:
            name = None
        if name:
            return name
    if pages is None:
        pages = reader.list_pages()
    return pages[0] if pages else None
