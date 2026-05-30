"""Tests for ComicInfo.xml parsing."""

import pytest

from app.archives.comicinfo import extract_cv_issue_id, parse_comicinfo
from app.models import ComicInfoStatus

# ---- extract_cv_issue_id -----------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://comicvine.gamespot.com/foo/4000-12345/", 12345),
        ("https://comicvine.gamespot.com/foo/4000-1/", 1),
        ("4000-99", 99),
        ("/some/prefix/4000-7/", 7),
        ("https://example.com/no-cv-id-here/", None),
        ("", None),
        (None, None),
        ("4000-abc", None),  # not numeric
    ],
)
def test_extract_cv_issue_id(url, expected):
    assert extract_cv_issue_id(url) == expected


# ---- parse_comicinfo ---------------------------------------------------


def test_parse_none_returns_status_none():
    result = parse_comicinfo(None)
    assert result.status is ComicInfoStatus.NONE
    assert result.series is None


def test_parse_malformed_xml_returns_status_none():
    result = parse_comicinfo(b"<not really xml")
    assert result.status is ComicInfoStatus.NONE


def test_parse_partial_no_web_field():
    xml = b"""<?xml version="1.0"?>
<ComicInfo>
  <Series>Saga</Series>
  <Number>1</Number>
  <Year>2012</Year>
</ComicInfo>"""
    result = parse_comicinfo(xml)
    assert result.status is ComicInfoStatus.PARTIAL
    assert result.series == "Saga"
    assert result.number == "1"
    assert result.year == 2012
    assert result.cv_issue_id is None
    assert not result.has_cv_id


def test_parse_partial_web_without_cv_id():
    xml = b"""<?xml version="1.0"?>
<ComicInfo>
  <Series>Saga</Series>
  <Web>https://example.com/some-other-site</Web>
</ComicInfo>"""
    result = parse_comicinfo(xml)
    assert result.status is ComicInfoStatus.PARTIAL
    assert result.web == "https://example.com/some-other-site"
    assert result.cv_issue_id is None


def test_parse_full_with_cvid():
    xml = b"""<?xml version="1.0"?>
<ComicInfo>
  <Series>Saga</Series>
  <Number>1</Number>
  <Year>2012</Year>
  <Web>https://comicvine.gamespot.com/saga-1/4000-345678/</Web>
</ComicInfo>"""
    result = parse_comicinfo(xml)
    assert result.status is ComicInfoStatus.FULL_WITH_CVID
    assert result.cv_issue_id == 345678
    assert result.has_cv_id
    assert result.series == "Saga"


def test_parse_handles_blank_fields():
    xml = b"""<?xml version="1.0"?>
<ComicInfo>
  <Series>   </Series>
  <Number></Number>
</ComicInfo>"""
    result = parse_comicinfo(xml)
    assert result.series is None
    assert result.number is None


def test_parse_invalid_year_falls_back_to_none():
    xml = b"""<?xml version="1.0"?>
<ComicInfo>
  <Year>not-a-year</Year>
</ComicInfo>"""
    result = parse_comicinfo(xml)
    assert result.year is None
