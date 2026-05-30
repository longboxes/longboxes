"""`files` and `file_locations` — the content/location split from §7 of the
design doc.

``files`` is the content identity table: one row per unique sha256, holding
properties intrinsic to the *contents* of the archive (page count, archive
format, ComicInfo coverage, the user's "this is not a comic" exclusion flag).

``file_locations`` is the path table: one row per distinct path on disk,
many-to-one with ``files``. Duplicates (same content at multiple paths)
produce multiple location rows for one file row. Clean moves update the path
of the existing location row in place (see scanner §9 Phase 2).

This split is what lets the scanner natively distinguish the
move / duplicate / replace cases. See §4 of the design doc.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ComicInfoStatus(StrEnum):
    """How much ComicInfo coverage a file has.

    ``none``           — no ComicInfo.xml inside the archive
    ``partial``        — ComicInfo present but no parseable CV ID URL in <Web>
    ``full_with_cvid`` — ComicInfo present with a parseable CV ID URL
    """

    NONE = "none"
    PARTIAL = "partial"
    FULL_WITH_CVID = "full_with_cvid"


class ArchiveFormat(StrEnum):
    CBZ = "cbz"
    CBR = "cbr"
    CB7 = "cb7"
    PDF = "pdf"


class File(Base):
    """Content identity. One row per unique sha256."""

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archive_format: Mapped[str | None] = mapped_column(String, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cover-image geometry, captured once when the file is scanned.
    # ``cover_is_wraparound`` is True when the cover page's aspect
    # ratio reads as a double-wide / wraparound image — see
    # ``app.archives.cover_image``. All three are nullable: null means
    # "cover not yet inspected" — a file scanned before cover
    # inspection existed, or one whose cover page wasn't a decodable
    # image. The cover endpoint lazy-backfills the long tail.
    cover_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_is_wraparound: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    # Interior-page geometry. Sampled from a mid-archive page at scan
    # time (see ``app.scanner.scanner._inspect_interior``) so the
    # duplicates scorer can prefer a real interior signal over the
    # cover area. Null means "interior not yet inspected" — files
    # scanned before this column existed, or a sample page that
    # wasn't a decodable image. Backfilled opportunistically on
    # re-scan, same pattern as the cover columns.
    interior_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interior_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comicinfo_status: Mapped[str] = mapped_column(
        String, nullable=False, default=ComicInfoStatus.NONE
    )
    excluded_from_matching: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    first_scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    locations: Mapped[list["FileLocation"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<File sha256={self.sha256[:12]} format={self.archive_format}>"


class FileLocation(Base):
    """Where the content lives on disk. Many-to-one with ``files``."""

    __tablename__ = "file_locations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    missing_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    file: Mapped[File] = relationship(back_populates="locations")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FileLocation path={self.path!r} missing={self.missing_since is not None}>"


class FileErrorKind(StrEnum):
    """The three failure modes the file-errors inspector surfaces.

    ``archive_open`` — opening the .cbr / .cbz raised. The
    ``files`` table has no record of the file (the scanner skips it
    after logging), so ``path`` is the only handle we have on it.

    ``cover_extraction`` — the cover endpoint hit its
    ``ArchiveError`` / ``UnsupportedArchiveError`` fallback and
    served the placeholder SVG. The ``files`` row exists; covers
    columns may stay null forever.

    ``comicinfo_parse`` — ComicInfo.xml was present in the
    archive but couldn't be parsed (malformed XML). Without this
    row the bad-XML case is indistinguishable from "no XML" — both
    land at ``comicinfo_status = NONE``.
    """

    ARCHIVE_OPEN = "archive_open"
    COVER_EXTRACTION = "cover_extraction"
    COMICINFO_PARSE = "comicinfo_parse"


class FileError(Base):
    """One row per (path, kind) failure currently recorded.

    A successful operation on the same (path, kind) deletes the row —
    so the inspector page only shows currently-broken files, not a
    history log. ``last_seen_at`` is bumped on every fresh failure so
    the page can show "started failing N hours ago, last seen 5
    minutes ago" if we ever want that. ``file_id`` is nullable
    because the ``ARCHIVE_OPEN`` kind happens before the file gets
    inserted into ``files``.
    """

    __tablename__ = "file_errors"
    # The ``on_conflict_do_update`` upsert in
    # ``record_error`` targets (path, kind). The Alembic migration
    # declares this as a unique index; the ORM also needs to know
    # about it so the test fixture's ``Base.metadata.create_all``
    # builds the constraint when it rebuilds the schema from
    # metadata. Without this declaration, INSERT ... ON CONFLICT
    # (path, kind) fails in tests with "no unique or exclusion
    # constraint matching the ON CONFLICT specification".
    __table_args__ = (
        UniqueConstraint("path", "kind", name="uq_file_errors_path_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    path: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    error_class: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FileError kind={self.kind} path={self.path!r}>"
