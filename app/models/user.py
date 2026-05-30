"""`users` table — application accounts.

Roles (per the design doc):
- ``admin``  — configures CV API key, library paths, scan interval, TTLs;
              manages users; reviews unmatched files; can force refresh.
- ``viewer`` — reads everything; tracks own progress; creates own collections.

Password hashing lives in ``app.auth.passwords`` (argon2id). The model only
stores the resulting hash — it never touches plaintext.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserRole(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Phase 6 (reader). When False the reader stops recording reading
    # progress for this user ("incognito reading") — existing progress
    # and the home reading lists are left untouched. Toggled from the
    # header. Defaults on.
    track_reading_progress: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
        default=True,
    )

    # ---- Convenience -----------------------------------------------------

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username} role={self.role}>"
