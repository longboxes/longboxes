"""files: cover-image geometry columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-20 12:00:00.000000

Adds cover_width / cover_height / cover_is_wraparound to ``files`` so
the scanner can persist double-wide (wraparound) cover detection. All
three are nullable — null means "cover not yet inspected" (a file
scanned before this feature, or one whose cover page wasn't a
decodable image).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("cover_width", sa.Integer(), nullable=True))
    op.add_column(
        "files", sa.Column("cover_height", sa.Integer(), nullable=True)
    )
    op.add_column(
        "files",
        sa.Column("cover_is_wraparound", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("files", "cover_is_wraparound")
    op.drop_column("files", "cover_height")
    op.drop_column("files", "cover_width")
