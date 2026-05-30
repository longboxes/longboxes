"""Tests for the best-effort ComicVine site-page scraper.

The ``parse_*`` tests are pure (no DB, no network) and run anywhere.
The ``scrape_*`` tests use the ``db_session`` fixture and a fake
fetcher injected in place of the real HTTP call.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from app.comicvine.scrape import (
    is_comicvine_url,
    parse_issues_cover_page,
    parse_volume_themes,
    scrape_character_volumes,
    scrape_volume_themes,
)
from app.models import (
    CvCharacter,
    CvCharacterVolume,
    CvVolume,
)


def test_is_comicvine_url():
    assert is_comicvine_url("https://comicvine.gamespot.com/x/4000-1/")
    assert is_comicvine_url("http://comicvine.gamespot.com/y/")
    assert not is_comicvine_url("https://evil.example.com/4000-1/")
    assert not is_comicvine_url("https://comicvine.gamespot.com.evil.com/")
    assert not is_comicvine_url("ftp://comicvine.gamespot.com/x/")
    assert not is_comicvine_url(None)
    assert not is_comicvine_url("")


def _make_fetcher(html):
    """An async fetcher stand-in that returns ``html`` and counts calls."""

    class _Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, url):
            self.calls += 1
            return html

    return _Fetcher()


# ---- parse_issues_cover_page (pure) ------------------------------------
#
# A character's issues-cover page lists volume cards: each volume is a
# 4050- link wrapping a cover image (no text of its own) plus a second
# 4050- link carrying the volume name. Page 1 also carries a
# ``<li class="results">`` element with the total count.


def _cover_page(volumes, *, results_count=None):
    """Build a synthetic issues-cover page. ``volumes`` is a list of
    ``(cv_id, name)`` — each rendered as a textless cover link + a name
    link, mirroring ComicVine's real markup."""
    rc = f'<li class="results">{results_count} results</li>' if results_count is not None else ""
    cards = "".join(
        f'<li class="volume">'
        f'<a href="/v/4050-{vid}/"><img data-src="https://x/{vid}.jpg"></a>'
        f'<a href="/v/4050-{vid}/">{name}</a></li>'
        for vid, name in volumes
    )
    return f'<html><body><ul class="grid">{rc}{cards}</ul></body></html>'


def test_parse_issues_cover_page_full():
    page = parse_issues_cover_page(_cover_page([(100, "Saga"), (200, "X-Men")], results_count=1424))
    assert page.results_count == 1424
    assert [(v.cv_id, v.name) for v in page.volumes] == [
        (100, "Saga"),
        (200, "X-Men"),
    ]
    # The cover comes from the textless cover link; the name from the
    # second link to the same id — merged into one ScrapedVolume.
    assert page.volumes[0].cover_url == "https://x/100.jpg"


def test_parse_issues_cover_page_count_with_commas():
    page = parse_issues_cover_page(_cover_page([(1, "A")], results_count="1,424"))
    assert page.results_count == 1424


def test_parse_issues_cover_page_empty_and_garbage():
    for html in ("", "<html><body><a href=", "not html"):
        page = parse_issues_cover_page(html)
        assert page.results_count is None
        assert page.volumes == []


def test_parse_issues_cover_page_gallery_titles():
    # The issues-cover view is a cover gallery — a card is often just a
    # cover link with no text of its own; the name lives in the link's
    # title attribute and the image's alt.
    html = (
        "<html><body><ul>"
        '<li><a href="/saga/4050-100/" title="Saga">'
        '<img data-src="https://x/100.jpg" alt="Saga"></a></li>'
        '<li><a href="/x-men/4050-200/">'
        '<img data-src="https://x/200.jpg" alt="X-Men"></a></li>'
        "</ul></body></html>"
    )
    page = parse_issues_cover_page(html)
    # Name from the link title for the first card, from the img alt for
    # the second (which has neither text nor a title).
    assert [(v.cv_id, v.name) for v in page.volumes] == [
        (100, "Saga"),
        (200, "X-Men"),
    ]
    assert page.volumes[0].cover_url == "https://x/100.jpg"


# ---- scrape_character_volumes (DB) -------------------------------------


def _make_paged_fetcher(pages):
    """An async fetcher stand-in mapping ``?page=N`` to ``pages[N]``
    (None for a page past the end)."""
    from urllib.parse import parse_qs, urlparse

    class _Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, url):
            self.calls += 1
            query = parse_qs(urlparse(url).query)
            n = int(query.get("page", ["1"])[0])
            return pages.get(n)

    return _Fetcher()


async def _add_character(db, cv_id=1486, *, site_url, **overrides):
    character = CvCharacter(
        cv_id=cv_id,
        name=overrides.pop("name", "Venom"),
        raw_payload={"id": cv_id, "name": "Venom", "site_detail_url": site_url},
        fetched_at=datetime.now(tz=UTC),
        volumes_scraped_at=overrides.pop("volumes_scraped_at", None),
    )
    db.add(character)
    await db.commit()
    return character


async def test_scrape_character_volumes_paginates(db_session):
    # Three volumes spread across two result pages, then an empty page
    # signals the end of the list.
    await _add_character(
        db_session,
        site_url="https://comicvine.gamespot.com/venom/4005-1486/",
    )
    fetcher = _make_paged_fetcher(
        {
            1: _cover_page([(10, "Alpha"), (20, "Beta")], results_count=3),
            2: _cover_page([(30, "Gamma")]),
            3: _cover_page([]),  # past the end -> 0 new -> stop
        }
    )
    result = await scrape_character_volumes(db_session, 1486, fetcher=fetcher)
    assert result["status"] == "ok"
    assert result["volumes"] == 3

    rows = (
        (
            await db_session.execute(
                select(CvCharacterVolume).where(CvCharacterVolume.character_cv_id == 1486)
            )
        )
        .scalars()
        .all()
    )
    assert {r.volume_cv_id for r in rows} == {10, 20, 30}
    by_id = {r.volume_cv_id: r for r in rows}
    assert by_id[10].name == "Alpha"
    assert by_id[10].cover_url == "https://x/10.jpg"
    # The character is stamped so the page stops showing "building".
    character = await db_session.get(CvCharacter, 1486)
    assert character.volumes_scraped_at is not None


async def test_scrape_character_volumes_replaces_wholesale(db_session):
    # A re-scrape replaces the stored rows entirely.
    await _add_character(
        db_session,
        site_url="https://comicvine.gamespot.com/venom/4005-1486/",
    )
    db_session.add(
        CvCharacterVolume(character_cv_id=1486, volume_cv_id=999, name="Stale", position=0)
    )
    await db_session.commit()

    fetcher = _make_paged_fetcher({1: _cover_page([(10, "Fresh")])})
    await scrape_character_volumes(db_session, 1486, fetcher=fetcher)

    rows = (
        (
            await db_session.execute(
                select(CvCharacterVolume).where(CvCharacterVolume.character_cv_id == 1486)
            )
        )
        .scalars()
        .all()
    )
    assert {r.volume_cv_id for r in rows} == {10}  # 999 is gone


async def test_scrape_character_volumes_no_site_url(db_session):
    character = CvCharacter(
        cv_id=77,
        name="No Site",
        raw_payload={"id": 77, "name": "No Site"},
        fetched_at=datetime.now(tz=UTC),
    )
    db_session.add(character)
    await db_session.commit()
    result = await scrape_character_volumes(db_session, 77, fetcher=_make_paged_fetcher({}))
    assert result["status"] == "skipped"
    assert result["reason"] == "no_site_url"


# ---- parse_volume_themes (pure) ----------------------------------------
#
# A volume page's "Themes" row: the volume's actual themes are <a> links
# carrying a ``themes[]=<id>`` query param. The edit panel below repeats
# the *whole* theme vocabulary as <select><option>s — those must be
# ignored (they're <option>s, not <a>s).

_VOLUME_THEMES_HTML = """
<tr>
  <th>Themes</th>
  <td>
    <div id="wiki-4050-2127-themes" data-field="themes"
         class="bar wiki-item-display">
      <a href="/volumes/?themes[]=2">Action</a>
      <a href="/volumes/?themes[]=11">Bronze Age (1970 - 1985)</a>
      <a href="/volumes/?themes[]=52">Complete</a>
      <a href="/volumes/?themes[]=61">Ongoing</a>
    </div>
    <div class="wiki-item-edit">
      <select id="volume_themes" name="volume[themes][]" multiple="multiple">
        <option value="2">Action</option>
        <option value="51">Cancelled</option>
        <option value="52">Complete</option>
        <option value="61">Ongoing</option>
      </select>
    </div>
  </td>
</tr>
"""


def test_parse_volume_themes():
    themes = parse_volume_themes(_VOLUME_THEMES_HTML)
    # Only the <a> theme links in the display div — not the full
    # <select> vocabulary in the edit panel.
    assert [(t.theme_id, t.name) for t in themes] == [
        (2, "Action"),
        (11, "Bronze Age (1970 - 1985)"),
        (52, "Complete"),
        (61, "Ongoing"),
    ]


def test_parse_volume_themes_empty_and_garbage():
    for html in ("", "<a href=", "no themes here"):
        assert parse_volume_themes(html) == []


# ---- scrape_volume_themes (DB) -----------------------------------------


async def test_scrape_volume_themes(db_session):
    db_session.add(
        CvVolume(
            cv_id=2127,
            name="Some Volume",
            raw_payload={
                "id": 2127,
                "site_detail_url": "https://comicvine.gamespot.com/v/4050-2127/",
            },
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()

    result = await scrape_volume_themes(
        db_session, 2127, fetcher=_make_fetcher(_VOLUME_THEMES_HTML)
    )
    assert result["status"] == "ok"
    assert result["themes"] == 4

    volume = await db_session.get(CvVolume, 2127)
    assert {t["id"] for t in volume.themes} == {2, 11, 52, 61}
    assert {t["name"] for t in volume.themes} == {
        "Action",
        "Bronze Age (1970 - 1985)",
        "Complete",
        "Ongoing",
    }
    # Stamped so the volume page fires the scrape at most once.
    assert volume.themes_scraped_at is not None


async def test_scrape_volume_themes_no_site_url(db_session):
    db_session.add(
        CvVolume(
            cv_id=88,
            name="No Site",
            raw_payload={"id": 88},
            fetched_at=datetime.now(tz=UTC),
        )
    )
    await db_session.commit()
    result = await scrape_volume_themes(db_session, 88, fetcher=_make_fetcher(_VOLUME_THEMES_HTML))
    assert result["status"] == "skipped"
    assert result["reason"] == "no_site_url"
