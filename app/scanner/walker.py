"""Library walker.

Yields candidate archive paths under a library root. Per §4 of the design
doc, the folder structure carries no semantics — we just enumerate files
with archive extensions and hand them to the scanner.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from app.archives.base import detect_archive_format

# Hidden directories we never descend into. macOS scatters ``.DS_Store``
# and Windows scatters ``Thumbs.db`` and friends; these aren't comics and
# scanning them would generate noise.
_SKIP_DIR_NAMES: frozenset[str] = frozenset({".DS_Store", "@eaDir", "__MACOSX"})


def iter_archive_paths(library_root: Path) -> Iterator[Path]:
    """Yield every archive-shaped file under ``library_root``, recursively.

    ``library_root`` is expected to exist; callers handle the absent-library
    case before invoking this (different log treatment).

    Symlinks are followed for files but not for directories — following
    directory symlinks risks infinite loops in pathological library
    layouts and we don't want to pull in ``os.walk(followlinks=True)``
    semantics without thinking about cycle detection.
    """
    library_root = Path(library_root)
    for dirpath, dirnames, filenames in os.walk(library_root, followlinks=False):
        # In-place mutation prunes the walk per os.walk's contract.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            full = Path(dirpath) / name
            if detect_archive_format(full) is not None:
                yield full
