"""sha256 helper.

Reads the file in fixed-size chunks rather than loading it whole — comic
archives can be hundreds of MB and we don't want a multi-GB working set just
to compute a digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 1 MB chunks — large enough that syscall overhead is negligible, small
# enough that memory usage stays predictable even when several scanners
# (or scans) happen to run in parallel.
_CHUNK_SIZE = 1 << 20


def sha256_file(path: Path) -> str:
    """Return the lowercase-hex sha256 of the file at ``path``.

    Raises ``OSError`` if the file can't be opened or read. The scanner
    catches this and logs the file as a skip.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()
