"""Parse MetronInfo.xml — symmetric to ``comicinfo.py``.

MetronInfo is the Metron-Project schema for comic metadata. Stricter
typing than ComicInfo, proper identifier resources, no Web tag abuse.
Spec: https://metron-project.github.io/docs/category/metroninfo

For the matcher we need the same fields ComicInfo provides — series,
volume, number, year, and a ComicVine identifier when present. The
output dataclass is the existing ``ComicInfoExtract`` so the matcher
can consume either schema's hints without caring which source they
came from.

Key MetronInfo field mappings:

  ComicInfoExtract field   ←  MetronInfo source
  ───────────────────────     ───────────────────────────────────────
  series                    <Series><Name>...</Name></Series>
  volume                    <Series><Volume>...</Volume></Series>
                              (numeric volume identifier)
  number                    <Number>...</Number>
  year                      <CoverDate>YYYY-MM-DD</CoverDate>
                              (year extracted)
  cv_issue_id               <IDS><ID source="Comic Vine">...</ID></IDS>
                              (numeric ID stored directly, not in a URL)
  web                       not used — MetronInfo splits identifiers
                              by source explicitly, so we don't need
                              to regex-mine a single string

Defensive parsing — malformed XML, missing fields, and odd encodings
all degrade to ``status=NONE`` rather than raising.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.archives.comicinfo import ComicInfoExtract
from app.models import ComicInfoStatus

# ---- Helpers -------------------------------------------------------------


def _text(root: ET.Element, path: str) -> str | None:
    """Return the stripped text under ``root.find(path)``, or None.

    ``path`` may be a multi-segment XPath ("Series/Name") so we can
    reach the nested <Name> inside <Series> directly."""
    el = root.find(path)
    if el is None or el.text is None:
        return None
    text = el.text.strip()
    return text or None


def _int(root: ET.Element, path: str) -> int | None:
    raw = _text(root, path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _year_from_date(raw: str | None) -> int | None:
    """Extract the year from a ``YYYY-MM-DD`` (or ``YYYY``) date string.

    MetronInfo's ``<CoverDate>`` is supposed to be ISO 8601 but
    scanlator-generated files vary. We accept anything that starts
    with four digits."""
    if not raw:
        return None
    head = raw.strip()[:4]
    try:
        return int(head)
    except ValueError:
        return None


def _cv_id_from_ids(root: ET.Element) -> int | None:
    """Find a Comic Vine identifier in MetronInfo's ``<IDS>`` block.

    Schema: ``<IDS><ID source="Comic Vine">12345</ID>...</IDS>``.
    Multiple ``<ID>`` children with different ``source`` values can
    coexist; we want the Comic Vine one. Case-insensitive match on
    the source attribute — some writers use "comic vine" or
    "comicvine" instead of the canonical "Comic Vine".

    Returns None when no Comic Vine identifier is found or when the
    value isn't a parseable integer."""
    ids_block = root.find("IDS")
    if ids_block is None:
        return None
    for id_el in ids_block.findall("ID"):
        source = (id_el.get("source") or "").strip().lower().replace(" ", "")
        if source != "comicvine":
            continue
        raw = (id_el.text or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


# ---- Main parser --------------------------------------------------------


def parse_metroninfo(xml_bytes: bytes | None) -> ComicInfoExtract:
    """Parse MetronInfo.xml bytes into a ``ComicInfoExtract``.

    Same behaviour contract as ``parse_comicinfo``:
    - ``xml_bytes is None`` → status NONE.
    - Bytes present but malformed XML → status NONE.
    - Parseable, with a Comic Vine ID in ``<IDS>`` → status FULL_WITH_CVID.
    - Parseable, no CV ID → status PARTIAL.
    """
    if xml_bytes is None:
        return ComicInfoExtract(status=ComicInfoStatus.NONE)

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ComicInfoExtract(status=ComicInfoStatus.NONE)

    series = _text(root, "Series/Name")
    volume = _text(root, "Series/Volume")
    number = _text(root, "Number")
    year = _year_from_date(_text(root, "CoverDate"))
    cv_id = _cv_id_from_ids(root)

    status = ComicInfoStatus.FULL_WITH_CVID if cv_id is not None else ComicInfoStatus.PARTIAL

    return ComicInfoExtract(
        status=status,
        series=series,
        volume=volume,
        number=number,
        year=year,
        # MetronInfo doesn't have a ComicInfo-style ``<Web>`` field;
        # callers that care about a clickable URL can derive one
        # from cv_issue_id later (we always know how to build a CV
        # URL from the integer ID).
        web=None,
        cv_issue_id=cv_id,
    )
