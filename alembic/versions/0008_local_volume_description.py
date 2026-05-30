"""local volume description

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-22 12:00:00.000000

Phase 11E. Adds a free-text ``description`` column to ``local_volumes``
— a short, hand-entered blurb at the volume level. Local content has no
ComicVine wiki text to draw on, so this is the only descriptive field a
local volume carries. Nullable; existing rows get NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "local_volumes",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("local_volumes", "description")
