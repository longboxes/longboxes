"""user reading-progress tracking toggle

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-22 20:00:00.000000

Phase 6 (reader). Adds ``users.track_reading_progress`` — the per-user
"incognito reading" switch behind the header toggle. When false the
reader stops recording progress; existing progress is left as-is.

NOT NULL with ``server_default true`` so existing accounts keep
tracking on. The default is kept permanently — new users are created by
the ORM, but a DB-level default keeps any direct insert safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "track_reading_progress",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "track_reading_progress")
