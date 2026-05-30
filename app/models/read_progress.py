"""``read_progress`` — per-user reading position within a file (Phase 6).

One row per (user, file): the page the reader was last on, a snapshot of
the archive's page count (so the home page can draw a progress bar
without reopening the file), and ``finished_at`` — set the first time
the last page is reached, and sticky thereafter (re-reading a comic does
not un-finish it).

Reading *direction* is a property of the content and lives on the volume
(see ``app/models/cv.py`` / ``local.py``); reading *progress* is
personal, so it is keyed by user here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReadProgress(Base):
    """A user's reading position in one file."""

    __tablename__ = "read_progress"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 0-based index of the page the reader was last on.
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Snapshot of the archive's page count at save time — lets the home
    # page show progress without reopening the file.
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set the first time the last page is reached; sticky afterwards.
    # NULL means the comic is still in progress.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ReadProgress user={self.user_id} file={self.file_id} "
            f"page={self.page}/{self.page_count}>"
        )
