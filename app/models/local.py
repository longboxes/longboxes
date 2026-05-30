"""``local_volumes`` + ``local_issues`` — user-authored library entities for
comics that have no ComicVine record (Phase 11).

Deliberately a *parallel* of the ``cv_volumes`` / ``cv_issues`` cache, not
rows inside it. The CV cache is disposable by design — TTL'd, revalidated
stale-while-revalidate, wholesale-clearable from ``/admin``. These rows are
the opposite: permanent, user-authored, never revalidated, and they must
survive a cache clear. Keeping them in their own tables makes that
guarantee structural.

Scope is core identification metadata only — name / year / publisher /
issue number / title. No descriptions, creators, characters, or arcs; a
local issue is a metadata island by design (see ``design/phase-11-*.md``).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class LocalVolume(Base):
    """A hand-entered series/volume for a comic not in ComicVine."""

    __tablename__ = "local_volumes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The volume's start year — same role as ``cv_volumes.year``.
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Free text. Local publishers usually aren't ComicVine publishers, so
    # this is intentionally NOT a foreign key to ``cv_publishers`` — it's
    # a plain string the user types.
    publisher_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Phase 11E — a short, hand-entered blurb at the volume level. Local
    # content has no ComicVine wiki text, so this is the only descriptive
    # field a local volume carries; plain text, no HTML.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 6 (reader). Per-volume page reading direction — "ltr"
    # (Western, the default) or "rtl" (manga); set from the reader's
    # direction toggle. Every issue in the volume shares it.
    reading_direction: Mapped[str] = mapped_column(
        String, nullable=False, server_default="ltr", default="ltr"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    issues: Mapped[list["LocalIssue"]] = relationship(
        back_populates="volume",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LocalVolume {self.name!r} ({self.year})>"


class LocalIssue(Base):
    """A hand-entered issue belonging to a ``LocalVolume``."""

    __tablename__ = "local_issues"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    local_volume_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("local_volumes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Same string convention as ``cv_issues.issue_number`` ("1", ".5",
    # "1.MU", "Annual 1") so ``sort_key_issue_number`` orders local and
    # CV issues identically.
    issue_number: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    volume: Mapped[LocalVolume] = relationship(back_populates="issues")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LocalIssue vol={self.local_volume_id} #{self.issue_number}>"
