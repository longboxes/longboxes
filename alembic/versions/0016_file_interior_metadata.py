"""files: interior-page geometry columns

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-28 12:00:00.000000

Adds ``interior_width`` / ``interior_height`` to ``files``. These are
the pixel dimensions of a mid-archive sample page captured by the
scanner — used by the duplicates inspector to compare scan quality
between two files matched to the same CV issue.

Why a separate sample? The existing ``cover_width`` / ``cover_height``
columns describe the first page only. The cover is a fine proxy when
all pages are encoded uniformly, but it can mislead in the awkward
cases the duplicates inspector exists to surface: a re-encode that
shrinks the cover but leaves interior art untouched, a title-card
"cover" that doesn't represent the rest of the archive, or a
wraparound where the captured area inflates without the interior
following suit. Storing a real interior sample as a separate signal
lets the scorer prefer it when present and fall back to cover area
when it isn't (older files, decode failures).

Both are nullable. Null means "interior not yet inspected" — files
scanned before this migration, or files whose mid-archive page wasn't
a decodable image. The scanner backfills opportunistically on any
re-scan of the same path, same as it does for cover columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("interior_width", sa.Integer(), nullable=True))
    op.add_column("files", sa.Column("interior_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("files", "interior_height")
    op.drop_column("files", "interior_width")
