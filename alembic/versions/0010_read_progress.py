"""read progress

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-22 19:00:00.000000

Phase 6 (reader). Adds ``read_progress`` — per-user reading position
within a file: one row per (user, file) holding the last page, a
snapshot of the page count, and ``finished_at`` (set the first time the
last page is reached, sticky thereafter).

The composite (user_id, file_id) primary key doubles as the index for
the per-user lookups the home page does, so no extra index is needed.
Both foreign keys cascade on delete — losing a user or a file should
take its progress rows with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "read_progress",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("page", sa.Integer(), server_default="0", nullable=False),
        sa.Column("page_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("read_progress")
