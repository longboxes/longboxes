"""Comicbox-backed archive reader.

Wraps the ``comicbox.box.Comicbox`` class behind the existing
``ArchiveReader`` Protocol so the scanner, matcher, and the Phase 6
reader can use the same interface regardless of which backend is
active. The Protocol's three methods map cleanly to comicbox's API:

    list_pages()      → cb.get_page_filenames()
    read_comicinfo()  → ComicInfo.xml bytes via cb.namelist() +
                         direct archive extraction (see _read_archive_entry)
    extract_page(n)   → cb.get_page_by_filename(n)

Comicbox itself is sync; callers that run inside an asyncio loop are
responsible for wrapping these methods in ``asyncio.to_thread`` (or
``run_in_executor``). The Protocol stays sync so the RQ worker — which
is also sync — can call methods directly without ceremony.

Why a wrapper instead of using ``Comicbox`` directly:

  * The existing Protocol is the contract the scanner and matcher
    already speak. Keeping it lets us toggle backends behind a
    setting without touching consumers.
  * Each ``Comicbox(...)`` open does a non-trivial amount of work
    (archive scan + multi-schema metadata parse). Wrapping it lets
    us cache the lifetime correctly — open once, call multiple
    methods, close. For the reader endpoint specifically, the
    Phase 6 work can put an LRU on top of this class.
  * ``Comicbox`` is a context manager; callers wouldn't expect to
    have to ``with`` every archive operation. The wrapper opens
    and closes inline per call to keep the surface familiar.

Failure modes are normalized to ``ArchiveError`` to match the stdlib
backends, so error handling in callers is uniform.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.archives.base import (
    COMICINFO_NAMES,
    METRONINFO_NAMES,
    PAGE_EXTENSIONS,
    ArchiveError,
)

logger = logging.getLogger("longboxes.archives.comicbox_reader")

# Same convention as cbr.py: point rarfile at ``unar`` (GPL-clean) rather
# than the non-free ``unrar``. Set at module import so the comicbox
# backend's RAR reads honor the same tool even when our cbr.py isn't
# imported. Idempotent — setting it again with the same value is fine.
try:
    import rarfile as _rarfile
    _rarfile.UNRAR_TOOL = "unar"
except ImportError:  # pragma: no cover — rarfile is a direct dep
    pass


def _open_comicbox(path: Path):
    """Open a Comicbox handle for ``path``, raising ``ArchiveError``
    on any failure so callers don't have to know about comicbox's
    own exception hierarchy.

    Lazy-imports comicbox so importing this module doesn't pull
    comicbox in when the stdlib backend is active — keeps the
    feature flag genuinely toggle-able and avoids paying the
    import cost on every test run."""
    try:
        from comicbox.box import Comicbox
    except ImportError as e:  # pragma: no cover — missing optional dep
        raise ArchiveError(
            "comicbox is not installed; either install it "
            "(``pip install comicbox``) or switch the archive backend "
            "setting to 'stdlib'."
        ) from e
    try:
        return Comicbox(path)
    except Exception as e:
        raise ArchiveError(f"failed to open archive: {path}") from e


def _safe(label: str, fn):
    """Run ``fn()`` and convert any exception into an ``ArchiveError``.

    Comicbox can throw a variety of exception types depending on
    which underlying library handled the format (zipfile, rarfile,
    pymupdf, py7zr). Funneling them all through ``ArchiveError``
    keeps the scanner's try/except blocks readable."""
    try:
        return fn()
    except ArchiveError:
        raise
    except Exception as e:
        raise ArchiveError(f"{label}: {type(e).__name__}: {e}") from e


class ComicboxReader:
    """Read pages + ComicInfo.xml from any format comicbox supports.

    Each call opens a fresh ``Comicbox`` context, calls one method,
    and closes. The scanner only makes one or two calls per file, so
    persistent handles would just complicate cleanup. The Phase 6
    reader endpoint, which makes many ``extract_page`` calls in a
    row, should layer an LRU over this class — same pattern as the
    stdlib ``CbzReader``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def list_pages(self) -> list[str]:
        """Return page filenames in archive reading order.

        Comicbox's ``get_page_filenames()`` already does the sort
        + image-extension filter, but we keep the same
        ``PAGE_EXTENSIONS`` whitelist as the stdlib reader as a
        defensive backstop — older comicbox versions or odd
        archives could surface non-image files."""
        def _do() -> list[str]:
            with _open_comicbox(self.path) as cb:
                names = cb.get_page_filenames() or []
                return [
                    n for n in names
                    if Path(n).suffix.lower() in PAGE_EXTENSIONS
                ]
        return _safe(f"list_pages {self.path}", _do)

    def read_comicinfo(self) -> bytes | None:
        """Return raw ComicInfo.xml bytes if present, else None.

        Reads through comicbox's ``namelist()`` to find the entry,
        then dispatches to the underlying archive library to extract
        the raw bytes. We deliberately return RAW bytes (not
        comicbox's parsed dict) so the existing
        ``parse_comicinfo()`` consumer keeps working unchanged —
        the stdlib + comicbox backends agree on the contract."""
        return self._read_root_entry(COMICINFO_NAMES, "read_comicinfo")

    def read_metroninfo(self) -> bytes | None:
        """Return raw MetronInfo.xml bytes if present, else None.

        Same shape as ``read_comicinfo`` — returns the bytes of
        the archive entry so the higher-level metadata parser can
        consume them. Symmetric with the stdlib backends'
        ``read_metroninfo``."""
        return self._read_root_entry(METRONINFO_NAMES, "read_metroninfo")

    def _read_root_entry(
        self, names: frozenset[str], label: str
    ) -> bytes | None:
        """Find a root-level archive entry whose basename matches
        one of ``names`` and return its raw bytes.

        Comicbox doesn't expose an arbitrary-entry-read primitive,
        so we use it for the listing (to honor whatever per-format
        rules it has for what counts as an archive entry) and then
        dispatch to the underlying archive library for the actual
        byte extraction. See ``_read_archive_entry`` for the dispatch."""
        def _do() -> bytes | None:
            with _open_comicbox(self.path) as cb:
                target = _find_root_entry(cb.namelist() or [], names)
                if target is None:
                    return None
                return _read_archive_entry(self.path, target)
        return _safe(f"{label} {self.path}", _do)

    def extract_page(self, name: str) -> bytes:
        """Return raw image bytes for the named page entry."""
        def _do() -> bytes:
            with _open_comicbox(self.path) as cb:
                data = cb.get_page_by_filename(name)
                if data is None:
                    raise ArchiveError(
                        f"page {name!r} not found in {self.path}"
                    )
                return data
        return _safe(f"extract_page {self.path} {name!r}", _do)

    # ---- Extra capabilities surfaced beyond the Protocol ---------------
    # The reader endpoint can use these for cover-first navigation,
    # double-page detection, and skip-ads behavior — features the
    # stdlib backend can't offer because ComicInfo's <Pages> block
    # is just bytes to it.

    def page_count(self) -> int:
        """Total page count from comicbox (post sort + filter)."""
        def _do() -> int:
            with _open_comicbox(self.path) as cb:
                return int(cb.get_page_count() or 0)
        return _safe(f"page_count {self.path}", _do)

    def cover_filename(self) -> str | None:
        """Filename of the page comicbox identifies as the cover.

        Comes from ComicInfo ``<Page Type="FrontCover">`` when
        present; falls back to the first page otherwise. Returns
        None on an empty archive."""
        def _do() -> str | None:
            with _open_comicbox(self.path) as cb:
                paths = cb.get_cover_path_list() or []
                return paths[0] if paths else None
        return _safe(f"cover_filename {self.path}", _do)


# ---- Helpers -------------------------------------------------------------


def _find_root_entry(
    namelist: list[str], names: frozenset[str]
) -> str | None:
    """Find the first root-level archive entry whose basename is in
    ``names``. Matches the same case-exact, root-only rule the stdlib
    backends use so the two paths agree on which file counts as "the"
    canonical metadata XML. Subdirectory entries (e.g.
    ``stuff/ComicInfo.xml``) are intentionally skipped — that's a
    publisher quirk, not a canonical placement.

    ``names`` is the set of acceptable basenames — typically
    ``COMICINFO_NAMES`` or ``METRONINFO_NAMES`` from ``base.py``."""
    for name in namelist:
        if Path(name).name in names and "/" not in name.rstrip("/"):
            return name
    return None


def _read_archive_entry(path: Path, entry: str) -> bytes:
    """Extract one entry's raw bytes from any archive comicbox supports.

    We can't go through comicbox here because it doesn't expose a
    "give me the raw bytes of an arbitrary archive entry" primitive
    — its archive-level reads are scoped to pages and metadata.
    So we dispatch on extension and use the same underlying
    libraries comicbox uses, just at one level lower:

      * .cbz / .zip  → ``zipfile``
      * .cbr / .rar  → ``rarfile`` (which shells out to ``unar``)
      * .cb7 / .7z   → ``py7zr`` (lazy-imported)
      * .pdf         → unsupported (PDFs don't have an embedded
                       ComicInfo.xml in the archive-entry sense;
                       comicbox stores it differently)

    Returns the raw bytes. Raises ``ArchiveError`` on extraction
    failure or unsupported format."""
    suffix = path.suffix.lower().lstrip(".")
    try:
        if suffix in ("cbz", "zip"):
            import zipfile
            with zipfile.ZipFile(path) as zf:
                return zf.read(entry)
        if suffix in ("cbr", "rar"):
            import rarfile
            with rarfile.RarFile(path) as rf:
                return rf.read(entry)
        if suffix in ("cb7", "7z"):
            import py7zr
            with py7zr.SevenZipFile(path, mode="r") as sf:
                contents: dict[str, Any] = sf.read(targets=[entry]) or {}
                buf = contents.get(entry)
                if buf is None:
                    raise ArchiveError(
                        f"entry {entry!r} not found in {path}"
                    )
                return buf.read()
        raise ArchiveError(
            f"raw entry read not supported for {suffix!r} archives"
        )
    except ArchiveError:
        raise
    except Exception as e:
        raise ArchiveError(
            f"failed to read {entry!r} from {path}: "
            f"{type(e).__name__}: {e}"
        ) from e
