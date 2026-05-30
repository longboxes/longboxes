"""Parse ComicInfo.xml — best-effort extraction of fields used by the matcher.

ComicInfo.xml is the de-facto metadata sidecar for comic archives, originated
by ComicRack and adopted widely. Spec:
https://anansi-project.github.io/docs/comicinfo/intro

We don't try to be a complete parser — only the fields the matcher needs:
- ``<Web>``    — typically a ComicVine URL. The CV ID is the gold-standard hint.
- ``<Series>``  — series name (matcher fallback)
- ``<Volume>``  — volume year or numeric volume identifier
- ``<Number>``  — issue number ("1", "1.MU", ".5", etc.)
- ``<Year>``    — cover year

The output also includes a ``status`` field encoding ComicInfo coverage per
§7's ``files.comicinfo_status`` column. That status is what the scanner writes
to the DB; the parsed hint fields stay transient and flow into the match
pipeline only.

Parsing is defensive: malformed XML, missing fields, and odd encodings all
degrade gracefully to a ``ComicInfoExtract`` with status NONE rather than
raising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from app.models import ComicInfoStatus

# ComicVine issue URLs look like:
#   https://comicvine.gamespot.com/anything/4000-12345/
# The trailing "4000-12345" is the issue resource type prefix and ID;
# we want the integer ID. Some files store just the bare "4000-12345" too,
# so we accept either form.
_CV_ID_RE = re.compile(r"(?:^|/)4000-(\d+)(?:/|$)")


@dataclass(frozen=True)
class ComicInfoExtract:
    """Result of parsing a ComicInfo.xml payload.

    ``parse_error`` is populated only when bytes were present but
    couldn't be parsed — the scanner uses it to distinguish that
    case from "no ComicInfo.xml in the archive at all" (both
    otherwise land at ``status=NONE``). The bad-XML case is what
    feeds the ``comicinfo_parse`` row on ``/admin/file-errors``.
    """

    status: ComicInfoStatus
    series: str | None = None
    volume: str | None = None
    number: str | None = None
    year: int | None = None
    web: str | None = None
    cv_issue_id: int | None = None
    parse_error: str | None = None

    @property
    def has_cv_id(self) -> bool:
        return self.cv_issue_id is not None


# ---- Helpers -------------------------------------------------------------


def extract_cv_issue_id(web_value: str | None) -> int | None:
    """Return the CV issue ID embedded in a ``<Web>`` value, or None."""
    if not web_value:
        return None
    m = _CV_ID_RE.search(web_value)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:  # pragma: no cover - regex already restricts to digits
        return None


def _text(root: ET.Element, tag: str) -> str | None:
    """Return the stripped text of ``<tag>`` under ``root``, or None.

    ComicInfo elements are flat (no nesting), so a single .find() suffices.
    """
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    text = el.text.strip()
    return text or None


def _int(root: ET.Element, tag: str) -> int | None:
    raw = _text(root, tag)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ---- Main parser --------------------------------------------------------


def parse_comicinfo(xml_bytes: bytes | None) -> ComicInfoExtract:
    """Parse ComicInfo.xml bytes into a ``ComicInfoExtract``.

    Behaviour:
    - ``xml_bytes is None`` (no ComicInfo in the archive) → status NONE.
    - Bytes present but malformed XML → status NONE (we couldn't read it,
      so for matcher purposes there's nothing here).
    - Parseable, with a CV ID in ``<Web>`` → status FULL_WITH_CVID.
    - Parseable, no CV ID → status PARTIAL.
    """
    if xml_bytes is None:
        return ComicInfoExtract(status=ComicInfoStatus.NONE)

    try:
        # ElementTree handles UTF-8 by default; some scanlator files declare
        # other encodings in their XML prolog which ET also honours.
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        # status stays NONE so downstream matcher / classifier logic
        # treats a malformed file the same as "no ComicInfo present"
        # — there's nothing usable in it either way. But we surface
        # the parse error so the scanner can record it under the
        # ``comicinfo_parse`` kind on /admin/file-errors.
        return ComicInfoExtract(
            status=ComicInfoStatus.NONE,
            parse_error=str(e)[:2000],
        )

    web = _text(root, "Web")
    cv_id = extract_cv_issue_id(web)
    series = _text(root, "Series")
    volume = _text(root, "Volume")
    number = _text(root, "Number")
    year = _int(root, "Year")

    status = ComicInfoStatus.FULL_WITH_CVID if cv_id is not None else ComicInfoStatus.PARTIAL

    return ComicInfoExtract(
        status=status,
        series=series,
        volume=volume,
        number=number,
        year=year,
        web=web,
        cv_issue_id=cv_id,
    )
