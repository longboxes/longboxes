"""cv_issues site-scrape marker

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-23 04:30:00.000000

Adds ``cv_issues.site_scraped_at`` — the marker for the best-effort
ComicVine *site-page* scraper. It is stamped the first time the scraper
runs for an issue, whether or not it managed to fill any gaps, so the
issue page fires that one-shot backfill at most once per issue.

Nullable, no default — NULL means "never attempted". Existing rows
therefore start eligible for a scrape.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cv_issues",
        sa.Column("site_scraped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cv_issues", "site_scraped_at")
