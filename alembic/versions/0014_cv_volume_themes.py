"""cv_volumes themes + theme-scrape marker

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-24 01:00:00.000000

ComicVine's JSON API doesn't expose a volume's "themes" (the genre /
era / publication-status tags shown on the volume's web page), so they
are scraped from that page. This migration adds:

* ``cv_volumes.themes`` — a JSONB array of ``{"id", "name"}`` theme
  dicts. NOT NULL, defaults to ``[]`` so existing rows and
  cache-inserted rows (whose INSERT doesn't name the column) are valid.
* ``cv_volumes.themes_scraped_at`` — stamped when the themes scrape
  last ran. Nullable, no default — NULL means "never scraped", so
  existing rows start eligible.

Both live outside ``raw_payload`` so they survive API revalidation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cv_volumes",
        sa.Column(
            "themes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "cv_volumes",
        sa.Column(
            "themes_scraped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("cv_volumes", "themes_scraped_at")
    op.drop_column("cv_volumes", "themes")
