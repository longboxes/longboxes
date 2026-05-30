"""ComicVine cache tables — §7 v0.5 of the design doc.

Per §4's "minimal replication" principle, only fields the matcher needs as
indexed lookups get typed columns; everything else lives inside
``raw_payload`` JSONB. Relationships between CV entities are derived from
the JSONB at query time, not materialized as join tables.

All CV IDs are ``bigint`` (CV's resource IDs are integers, but bigint is
forward-safe). Timestamps are timezone-aware.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# ---- Publishers --------------------------------------------------------


class CvPublisher(Base):
    __tablename__ = "cv_publishers"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---- Volumes -----------------------------------------------------------


class CvVolume(Base):
    __tablename__ = "cv_volumes"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    publisher_cv_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cv_publishers.cv_id"),
        nullable=True,
        index=True,
    )
    count_of_issues: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 6 (reader). Per-volume page reading direction — "ltr"
    # (Western, the default) or "rtl" (manga); set from the reader's
    # direction toggle. Deliberately a typed column *outside*
    # ``raw_payload``: it is user-authored, not a ComicVine field, so it
    # must survive cache revalidation. The cache's volume upsert lists
    # an explicit ``set_`` (name/year/payload/...) that does not name
    # this column, and the admin cache-clear only NULLs ``fetched_at``
    # — so a stored direction is never clobbered.
    reading_direction: Mapped[str] = mapped_column(
        String, nullable=False, server_default="ltr", default="ltr"
    )
    # ComicVine "themes" — genre / era / publication-status tags shown
    # on the volume's *web page* but absent from the JSON API. Scraped
    # into a list of ``{"id", "name"}`` dicts. A typed column *outside*
    # ``raw_payload`` (like ``reading_direction``) so it survives cache
    # revalidation — the volume upsert's ``set_`` doesn't name it.
    themes: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    # Stamped when the themes scrape last ran (whatever the outcome),
    # so the volume page fires it at most once. NULL = never scraped.
    themes_scraped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---- Issues ------------------------------------------------------------


class CvIssue(Base):
    """Note: ``raw_payload`` and ``fetched_at`` are nullable.

    A stub row is created when a volume fetch returns its issue list (we get
    id / name / number / cover_date but not the full record). The full record
    is hydrated on first ``/issue/X/`` call; until then, ``raw_payload`` and
    ``fetched_at`` are NULL.
    """

    __tablename__ = "cv_issues"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    volume_cv_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cv_volumes.cv_id"),
        nullable=True,
    )
    # CV returns issue numbers as strings: "1", "1.MU", "0", ".5", "Annual 1", ...
    issue_number: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Best-effort site-page scrape marker. Set (to "now") the first time
    # the ComicVine *website* scraper runs for this issue — whether or
    # not it filled any gaps — so the issue page fires that one-shot
    # backfill at most once. NULL means "never attempted". Not a
    # ComicVine field: it survives API revalidation because the cache's
    # issue upsert ``set_`` (and the bulk-hydrate ``set_``) don't name
    # it — same posture as ``CvVolume.reading_direction``.
    site_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---- "Just (cv_id, name, raw_payload, fetched_at)" entities ------------
#
# These four tables share the same shape; the matcher only ever looks them
# up by cv_id, so no extra typed columns are needed. Relationships (issue ↔
# person/character/team/arc) live inside the raw payloads and are computed
# in Python at query time per §8.


class CvPerson(Base):
    __tablename__ = "cv_persons"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CvCharacter(Base):
    __tablename__ = "cv_characters"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Best-effort marker for the character's volume-appearance scrape.
    # The API's volume grouping for a character is unreliable, so the
    # appearance list is built by scraping the character's ComicVine
    # ``issues-cover`` web page into ``cv_character_volumes``. Stamped
    # ("now") when that scrape completes. NULL means "never scraped" —
    # the character page then enqueues the scrape. Not a ComicVine
    # field: it survives API revalidation because the cache's character
    # upsert ``set_`` doesn't name it — same posture as
    # ``CvIssue.site_scraped_at`` / ``CvVolume.reading_direction``.
    volumes_scraped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CvCharacterVolume(Base):
    """A volume a character appears in — scraped from the character's
    ComicVine ``issues-cover`` web page (the JSON API's volume data for
    a character is unreliable, so we read the site's own volume list).

    One row per (character, volume). ``position`` is the 0-based scrape
    order so the page can preserve ComicVine's ordering. ``name`` /
    ``cover_url`` are captured from the scrape so the character page
    renders without a per-volume API call. The whole set for a
    character is replaced wholesale by each scrape; ``volume_cv_id`` is
    intentionally *not* a foreign key — the volume need not be cached
    in ``cv_volumes`` for a card to render or link out.
    """

    __tablename__ = "cv_character_volumes"

    character_cv_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cv_characters.cv_id"),
        primary_key=True,
    )
    volume_cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    cover_url: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CvStoryArc(Base):
    __tablename__ = "cv_story_arcs"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CvTeam(Base):
    __tablename__ = "cv_teams"

    cv_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---- Search cache ------------------------------------------------------


class CvSearchCache(Base):
    """Keyed by ``request_key`` — a hash of endpoint + params, computed by
    the cache service. Holds the raw search response JSON for short-term
    reuse (default 1-hour TTL per §8).
    """

    __tablename__ = "cv_search_cache"

    request_key: Mapped[str] = mapped_column(String, primary_key=True)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
