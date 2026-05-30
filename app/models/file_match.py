"""``file_matches`` — bridge table linking ``files`` rows to ``cv_issues`` rows.

Per §7 v0.5 of the design doc. One row per files row (1:1 PK on file_id),
with a confidence score and a status enum that drives the review UI.

A file_matches row exists iff the matcher has at least attempted to match
the file. The scanner enqueues a ``match_file`` job for every new file; the
job writes (or replaces) the file_matches row based on the matcher pipeline
result.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MatchStatus(StrEnum):
    """The state of a file's match attempt.

    AUTO / CONFIRMED / PENDING / REJECTED / UNMATCHED are the original
    file-to-CV-issue states. LOCAL and SUPPLEMENT (Phase 11) are
    *resolved* states for non-issue files — like CONFIRMED they leave
    the review queue and the matcher never overwrites them.
    """

    AUTO = "auto"  # matched without human review (confidence ≥ 0.85)
    CONFIRMED = "confirmed"  # human reviewer accepted the match
    PENDING = "pending"  # 0.50-0.85; in the review queue
    REJECTED = "rejected"  # human rejected the candidates; try again later
    UNMATCHED = "unmatched"  # < 0.50; no usable candidates
    LOCAL = "local"  # a user-authored local entry (not in CV)
    SUPPLEMENT = "supplement"  # supplemental content attached to a CV volume


class MatchSource(StrEnum):
    """How the match was produced."""

    COMICINFO_CVID = "comicinfo_cvid"  # Stage 1: <Web> field had a CV ID
    FILENAME = "filename"  # Stage 2-3: parsed + searched
    MANUAL = "manual"  # human picked it from CV search
    LOCAL = "local"  # human authored local metadata


class FileMatch(Base):
    __tablename__ = "file_matches"

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # A file_matches row resolves a file in exactly one of three ways —
    # a CV issue, a local issue, or a supplement to a CV volume. The
    # three target columns below are mutually exclusive: a CHECK
    # constraint (``ck_file_matches_single_target``) enforces that at
    # most one is set. ``issue_cv_id`` is the CV-issue target.
    issue_cv_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cv_issues.cv_id"),
        nullable=True,
    )
    # Phase 11. ``LOCAL`` rows point here — a user-authored issue with
    # no ComicVine record.
    local_issue_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("local_issues.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Phase 11. ``SUPPLEMENT`` rows point here — the file is non-issue
    # content (a cover gallery, etc.) attached to a real CV volume.
    supplement_volume_cv_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cv_volumes.cv_id"),
        nullable=True,
    )
    # The kind of supplement: ``cover_gallery`` is the first wired
    # value; ``sketch`` / ``script`` / etc. are left open. Set only on
    # ``SUPPLEMENT`` rows.
    supplement_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # CV-match heuristic score. NULL for human-resolved rows that had no
    # candidate to score — CONFIRMED-by-pick, LOCAL, SUPPLEMENT.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Top-N alternative candidates serialised as a list of
    #   {"issue_cv_id": int, "volume_cv_id": int, "confidence": float, ...}
    # Used to render the review UI (Phase 7) without re-running the matcher.
    candidates: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    matched_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,  # null for auto/unmatched/pending; set on confirmed/rejected/manual
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<FileMatch file_id={self.file_id} status={self.status} "
            f"issue_cv_id={self.issue_cv_id} conf={self.confidence}>"
        )
