"""Best-effort scraping of ComicVine *web* pages.

Some facts ComicVine shows on its website aren't in its JSON API at
all. This module scrapes those gaps:

* ``scrape_character_volumes`` — the volume-appearance list off a
  character's paginated ``issues-cover`` page (the API's per-character
  volume data is unreliable).
* ``scrape_volume_themes`` — a volume's "themes" row (genre / era /
  publication-status tags), which the API omits entirely.

This is deliberately separate from ``app.comicvine.client`` (the JSON
API client) and ``app.comicvine.cache`` (the SWR cache layer): it talks
to the *website*, not the API. HTML scraping is brittle by nature, so
every step here fails soft — a parse miss, a non-HTML body, a network
error or a blocked request just leaves the cached data as it was.

Design notes:

* The parsers key off ComicVine's stable URL encodings — ``/4050-<id>/``
  for a volume, ``themes[]=<id>`` for a theme — rather than CSS classes
  or DOM structure. A redesign can rename every class; those URL shapes
  are the same encoding the API client relies on.
* Module-level imports are stdlib only, so the parsers can be
  unit-tested without a database or the network. The heavier imports
  (httpx, the ORM models) are local to the functions that need them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("longboxes.comicvine.scrape")

# ComicVine encodes the resource type in every site URL as a "4NNN-<id>"
# path segment — 4050 is a volume. The leading slash anchors it to a
# real path component (so a stray "4050-" in a query string can't
# false-match).
_VOLUME_HREF_RE = re.compile(r"/4050-(\d+)")

# Only ever fetch the ComicVine site itself. ``site_detail_url`` comes
# from CV's own API payload, but validating the host keeps a poisoned
# or stale payload from pointing the scraper somewhere unexpected.
_ALLOWED_HOST = "comicvine.gamespot.com"

# ComicVine's site sits behind a WAF that rejects requests that look
# automated. A Chrome User-Agent with none of the Accept / Sec-Fetch
# headers a real Chrome sends is itself a tell, so we send the full,
# self-consistent set. This is the limit of what the scraper does — it
# makes the request well-formed; it does not forge a browser session,
# solve a challenge, or spoof a TLS fingerprint. If the WAF still
# answers 403, ComicVine is declining automated site access and the
# scrape simply fails soft.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# A fetcher takes a URL and returns the page HTML, or None on any
# failure. Pulled out as a type so tests can inject a fake.
Fetcher = Callable[[str], Awaitable[str | None]]


def is_comicvine_url(url: str | None) -> bool:
    """True when ``url`` is an http(s) ComicVine site URL — the only
    host the scraper will fetch."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host == _ALLOWED_HOST or host.endswith("." + _ALLOWED_HOST)


async def fetch_cv_page(url: str, *, timeout: float = 12.0) -> str | None:
    """GET a ComicVine site page and return its HTML, or None on any
    failure (network error, non-200, non-HTML body). Never raises."""
    import httpx  # local: keeps module import stdlib-only

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=8.0),
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("CV-page fetch failed for %s: %s", url, exc)
        return None

    if resp.status_code != 200:
        # 403 in particular means ComicVine's WAF is declining automated
        # access — a header tweak won't reliably change that. The scrape
        # just gives up and fails soft. The attempt is still recorded by
        # the caller so it isn't retried.
        logger.warning("CV-page fetch %s returned HTTP %s", url, resp.status_code)
        return None
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        logger.warning(
            "CV-page fetch %s returned non-HTML body (%s)",
            url,
            content_type or "no content-type",
        )
        return None
    return resp.text


# ======================================================================
# Character "issues-cover" page scraper
# ======================================================================
#
# ComicVine's JSON API gives a character a ``volume_credits`` list, but
# it's unreliable — incomplete and often stale. The character's public
# ``issues-cover`` web page, by contrast, lists every volume the
# character appears in, 32 to a page. We scrape that paginated list so
# the character page's appearance tab can render a clean volume grid.
#
# Same philosophy as the issue-page scraper above: key off the stable
# ``/4050-<id>/`` volume-URL encoding rather than CSS classes, and fail
# soft at every step.

# Page 1 of the issues-cover list carries a ``<li class="results">N
# results</li>`` element with the total volume count. We read it for
# logging / metadata, but pagination actually terminates when a page
# yields no volumes we haven't already seen — robust whether or not
# this brittle, class-keyed element is found.
_RESULTS_COUNT_RE = re.compile(r"([\d,]+)")


@dataclass
class ScrapedVolume:
    """One volume card from a character's issues-cover page."""

    cv_id: int
    name: str
    cover_url: str | None = None


@dataclass
class ScrapedCoverPage:
    """One page of a character's issues-cover list — the total result
    count (None if the count element wasn't found) and the page's
    volume cards in document order."""

    results_count: int | None = None
    volumes: list[ScrapedVolume] | None = None

    def __post_init__(self) -> None:
        if self.volumes is None:
            self.volumes = []


def _pick_img_src(attrs: dict[str, str]) -> str | None:
    """Best image URL from an ``<img>``'s attributes — prefers the
    lazy-load ``data-src`` over a placeholder ``src``; ignores inline
    ``data:`` URIs (the placeholder blur)."""
    for key in ("data-src", "src"):
        val = (attrs.get(key) or "").strip()
        if val and not val.startswith("data:"):
            return val
    for key in ("data-srcset", "srcset"):
        val = (attrs.get(key) or "").strip()
        if val:
            first = val.split(",")[0].strip().split(" ")[0].strip()
            if first and not first.startswith("data:"):
                return first
    return None


class _IssuesCoverParser(HTMLParser):
    """Pulls volume links + the result count out of a character's
    issues-cover page.

    Volume cards are recognised by their ``/4050-<id>/`` href — the
    stable volume-URL encoding — so the parser is indifferent to
    ComicVine's DOM. The issues-cover view is a cover
    *gallery*, so a card is frequently just a cover image wrapped in a
    volume link with no text of its own — the volume name then lives
    in the link's ``title`` or the image's ``alt``. The name is taken
    from the first of: the link text, the link ``title`` /
    ``data-title``, the nested ``<img>``'s ``alt`` / ``title``. The
    cover is the first ``<img>`` nested in any link to that volume."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results_count: int | None = None
        # (cv_id, name, cover url) — one per <a>; merged by id later.
        self.entries: list[tuple[int, str, str | None]] = []
        # State for the <a> currently being read.
        self._cv_id: int | None = None
        self._text: list[str] = []
        self._link_title: str = ""  # the <a>'s own title attribute
        self._cover: str | None = None
        self._img_name: str = ""  # a nested <img>'s alt / title
        # State for a <li class="results"> currently being read.
        self._in_results = False
        self._results_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "li":
            classes = (a.get("class") or "").split()
            if "results" in classes and self.results_count is None:
                self._in_results = True
                self._results_text = []
            return
        if tag == "a":
            vm = _VOLUME_HREF_RE.search(a.get("href") or "")
            if vm:
                self._cv_id = int(vm.group(1))
                self._text = []
                self._link_title = (a.get("title") or a.get("data-title") or "").strip()
                self._cover = None
                self._img_name = ""
            return
        if tag == "img" and self._cv_id is not None:
            if self._cover is None:
                self._cover = _pick_img_src(a)
            if not self._img_name:
                self._img_name = (a.get("alt") or a.get("title") or "").strip()

    def handle_data(self, data: str) -> None:
        if self._cv_id is not None:
            self._text.append(data)
        if self._in_results:
            self._results_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._cv_id is not None:
            text = " ".join("".join(self._text).split())
            name = text or self._link_title or self._img_name
            self.entries.append((self._cv_id, name, self._cover))
            self._cv_id = None
            self._text = []
            self._link_title = ""
            self._cover = None
            self._img_name = ""
        elif tag == "li" and self._in_results:
            text = "".join(self._results_text)
            match = _RESULTS_COUNT_RE.search(text)
            if match:
                try:
                    self.results_count = int(match.group(1).replace(",", ""))
                except ValueError:
                    self.results_count = None
            self._in_results = False
            self._results_text = []


def parse_issues_cover_page(html: str) -> ScrapedCoverPage:
    """Parse one issues-cover page into a ``ScrapedCoverPage``.

    Pure and total: malformed HTML never raises — a parse that finds
    nothing returns an empty page. Volume links are merged by id (CV
    repeats the id on the cover-image link and the name link), keeping
    document order, the first non-empty name, and the first cover."""
    parser = _IssuesCoverParser()
    try:
        parser.feed(html or "")
    except Exception as exc:
        logger.warning("issues-cover parse error: %s", exc)

    merged: dict[int, ScrapedVolume] = {}
    for cv_id, name, cover in parser.entries:
        existing = merged.get(cv_id)
        if existing is None:
            merged[cv_id] = ScrapedVolume(cv_id=cv_id, name=name, cover_url=cover)
            continue
        if not existing.name and name:
            existing.name = name
        if existing.cover_url is None and cover is not None:
            existing.cover_url = cover
    return ScrapedCoverPage(
        results_count=parser.results_count,
        volumes=list(merged.values()),
    )


def _issues_cover_url(site_url: str, page: int) -> str:
    """Build the ``issues-cover`` page URL for a given 1-based page —
    ``<site_detail_url>/issues-cover/?page=<n>``."""
    return f"{site_url.rstrip('/')}/issues-cover/?page={page}"


async def scrape_character_volumes(
    db: Any,
    character_cv_id: int,
    *,
    site_url: str | None = None,
    fetcher: Fetcher | None = None,
    max_pages: int = 300,
) -> dict:
    """Scrape a character's full volume-appearance list off its
    ComicVine ``issues-cover`` web page and store it in
    ``cv_character_volumes``.

    Steps:

    1. Resolve the page URL from the passed ``site_url`` or the cached
       character's ``site_detail_url``.
    2. Walk ``issues-cover/?page=N`` until a page yields no volume we
       haven't already seen (the natural end-of-list signal — robust
       even when CV serves an out-of-range page by repeating page 1),
       or ``max_pages`` is hit. Volumes are de-duped by id across the
       whole walk, document order preserved.
    3. Replace the character's ``cv_character_volumes`` rows wholesale
       and stamp ``CvCharacter.volumes_scraped_at``.

    A page-1 fetch failure aborts *without* stamping or touching the
    stored rows, so a transient blip is retried on the next visit.
    ``fetcher`` is injectable for tests. Returns a status dict.
    """
    from datetime import UTC, datetime

    from sqlalchemy import delete as sa_delete

    from app.models import CvCharacter, CvCharacterVolume

    fetch = fetcher or fetch_cv_page
    now = datetime.now(tz=UTC)

    character = await db.get(CvCharacter, character_cv_id)
    if character is None:
        return {"status": "skipped", "reason": "unknown_character"}

    url_base = site_url or (character.raw_payload or {}).get("site_detail_url")
    if not is_comicvine_url(url_base):
        return {"status": "skipped", "reason": "no_site_url"}

    seen: dict[int, ScrapedVolume] = {}
    results_count: int | None = None
    pages_fetched = 0
    for page in range(1, max_pages + 1):
        html = await fetch(_issues_cover_url(url_base, page))
        if html is None:
            if page == 1:
                # Couldn't reach the site at all. Stamp the character so
                # the page stops showing its "building" state (and stops
                # re-enqueueing the scrape) — but leave any existing
                # rows untouched. A future revalidation re-attempts.
                character.volumes_scraped_at = now
                await db.commit()
                return {"status": "fetch_failed", "page": 1}
            break  # mid-walk blip — keep what we have, stop here
        pages_fetched += 1
        parsed = parse_issues_cover_page(html)
        if results_count is None:
            results_count = parsed.results_count
        new = 0
        for vol in parsed.volumes or []:
            if vol.cv_id not in seen:
                seen[vol.cv_id] = vol
                new += 1
        if new == 0:
            break  # page added nothing — end of the list

    # Replace this character's volume rows wholesale.
    await db.execute(
        sa_delete(CvCharacterVolume).where(CvCharacterVolume.character_cv_id == character_cv_id)
    )
    db.add_all(
        [
            CvCharacterVolume(
                character_cv_id=character_cv_id,
                volume_cv_id=vol.cv_id,
                name=vol.name or "(untitled)",
                cover_url=vol.cover_url,
                position=idx,
            )
            for idx, vol in enumerate(seen.values())
        ]
    )
    character.volumes_scraped_at = now
    await db.commit()
    return {
        "status": "ok",
        "volumes": len(seen),
        "pages": pages_fetched,
        "results_count": results_count,
    }


# ======================================================================
# Volume "themes" scraper
# ======================================================================
#
# A volume's ComicVine web page carries a "Themes" row — genre / era /
# publication-status tags (Action, Modern Age, Ongoing, Complete, ...).
# The JSON API omits them entirely, so we scrape that one page. The
# themes are recognised by their ``/volumes/?themes[]=<id>`` link href
# — a stable query-string encoding, the same kind of structural hook
# the issue / issues-cover parsers key off. The page also embeds an
# edit ``<select>`` listing *every* possible theme; that's <option>s,
# not <a>s, so it's naturally excluded.
_THEME_HREF_RE = re.compile(r"themes\[\]=(\d+)")


@dataclass
class ScrapedTheme:
    """One theme tag from a volume page — a stable CV theme id + label."""

    theme_id: int
    name: str


class _VolumeThemesParser(HTMLParser):
    """Pulls a volume's theme tags out of its ComicVine page.

    Collects every ``<a>`` whose href carries a ``themes[]=<id>`` query
    param (the volume's actual themes), with the link text as the label.
    Indifferent to ComicVine's DOM — keyed purely off the href."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # (theme_id, link text) — one per <a>; de-duped by id later.
        self.entries: list[tuple[int, str]] = []
        self._theme_id: int | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        a = {k: (v or "") for k, v in attrs}
        match = _THEME_HREF_RE.search(a.get("href") or "")
        if match:
            self._theme_id = int(match.group(1))
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._theme_id is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._theme_id is not None:
            name = " ".join("".join(self._text).split())
            self.entries.append((self._theme_id, name))
            self._theme_id = None
            self._text = []


def parse_volume_themes(html: str) -> list[ScrapedTheme]:
    """Parse a volume page's HTML into its list of themes.

    Pure and total: malformed HTML never raises. Themes are de-duped by
    id, keeping document order and the first non-empty label."""
    parser = _VolumeThemesParser()
    try:
        parser.feed(html or "")
    except Exception as exc:
        logger.warning("volume-themes parse error: %s", exc)

    merged: dict[int, ScrapedTheme] = {}
    for theme_id, name in parser.entries:
        existing = merged.get(theme_id)
        if existing is None:
            merged[theme_id] = ScrapedTheme(theme_id=theme_id, name=name)
        elif not existing.name and name:
            existing.name = name
    return list(merged.values())


async def scrape_volume_themes(
    db: Any,
    volume_cv_id: int,
    *,
    site_url: str | None = None,
    fetcher: Fetcher | None = None,
) -> dict:
    """Scrape a volume's themes off its ComicVine web page and store
    them on ``cv_volumes.themes``.

    One page fetch. ``themes_scraped_at`` is stamped whatever the
    outcome — a dead or blocked page is never re-fetched — so the
    volume page fires this at most once. ``fetcher`` is injectable for
    tests. Returns a status dict.
    """
    from datetime import UTC, datetime

    from app.models import CvVolume

    fetch = fetcher or fetch_cv_page
    now = datetime.now(tz=UTC)

    volume = await db.get(CvVolume, volume_cv_id)
    if volume is None:
        return {"status": "skipped", "reason": "unknown_volume"}

    url = site_url or (volume.raw_payload or {}).get("site_detail_url")
    if not is_comicvine_url(url):
        return {"status": "skipped", "reason": "no_site_url"}

    html = await fetch(url)
    volume.themes_scraped_at = now  # stamp the attempt regardless
    if html is None:
        await db.commit()
        return {"status": "fetch_failed", "url": url}

    themes = parse_volume_themes(html)
    volume.themes = [{"id": t.theme_id, "name": t.name} for t in themes]
    await db.commit()
    return {"status": "ok", "themes": len(themes), "url": url}
