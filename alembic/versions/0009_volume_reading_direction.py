"""volume reading direction

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-22 18:00:00.000000

Phase 6 (reader). Adds a ``reading_direction`` column to both volume
tables — ``cv_volumes`` and ``local_volumes`` — so the reader's
left-to-right / right-to-left choice persists per volume (manga reads
RTL, Western comics LTR). The reader toggle writes it back; every issue
in a volume then shares the setting.

NOT NULL with ``server_default 'ltr'`` so existing rows backfill to the
Western default. The server default is kept permanently: ``cv_volumes``
rows are inserted by the ComicVine cache layer's ``pg_insert``, which
sets an explicit column list that does not include ``reading_direction``
— the DB default has to fill it. (The admin cache-clear only NULLs
``fetched_at``; it never deletes ``cv_volumes`` rows, so a stored
direction survives a cache clear and SWR revalidation.)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("cv_volumes", "local_volumes"):
        op.add_column(
            table,
            sa.Column(
                "reading_direction",
                sa.String(),
                nullable=False,
                server_default="ltr",
            ),
        )


def downgrade() -> None:
    for table in ("cv_volumes", "local_volumes"):
        op.drop_column(table, "reading_direction")
