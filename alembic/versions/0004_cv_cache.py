"""cv_*: ComicVine cache tables per §7 v0.5

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _simple_entity(name: str) -> None:
    """Create one of the uniform (cv_id, name, raw_payload, fetched_at) tables."""
    op.create_table(
        name,
        sa.Column("cv_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("raw_payload", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    # Publishers — referenced by volumes, so create first.
    _simple_entity("cv_publishers")

    # Volumes
    op.create_table(
        "cv_volumes",
        sa.Column("cv_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column(
            "publisher_cv_id",
            sa.BigInteger(),
            sa.ForeignKey("cv_publishers.cv_id"),
            nullable=True,
        ),
        sa.Column("count_of_issues", sa.Integer(), nullable=True),
        sa.Column("raw_payload", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cv_volumes_name", "cv_volumes", ["name"])
    op.create_index("ix_cv_volumes_year", "cv_volumes", ["year"])
    op.create_index(
        "ix_cv_volumes_publisher_cv_id", "cv_volumes", ["publisher_cv_id"]
    )

    # Issues (raw_payload / fetched_at nullable: stub rows allowed)
    op.create_table(
        "cv_issues",
        sa.Column("cv_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "volume_cv_id",
            sa.BigInteger(),
            sa.ForeignKey("cv_volumes.cv_id"),
            nullable=True,
        ),
        sa.Column("issue_number", sa.String(), nullable=True),
        sa.Column("cover_date", sa.Date(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("raw_payload", JSONB(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_cv_issues_volume_cv_id_issue_number",
        "cv_issues",
        ["volume_cv_id", "issue_number"],
    )
    op.create_index("ix_cv_issues_cover_date", "cv_issues", ["cover_date"])

    # Uniform entity tables.
    for name in ("cv_persons", "cv_characters", "cv_story_arcs", "cv_teams"):
        _simple_entity(name)

    # Search response cache.
    op.create_table(
        "cv_search_cache",
        sa.Column("request_key", sa.String(), primary_key=True, nullable=False),
        sa.Column("response_json", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cv_search_cache")
    for name in ("cv_teams", "cv_story_arcs", "cv_characters", "cv_persons"):
        op.drop_table(name)
    op.drop_index("ix_cv_issues_cover_date", table_name="cv_issues")
    op.drop_index(
        "ix_cv_issues_volume_cv_id_issue_number", table_name="cv_issues"
    )
    op.drop_table("cv_issues")
    op.drop_index("ix_cv_volumes_publisher_cv_id", table_name="cv_volumes")
    op.drop_index("ix_cv_volumes_year", table_name="cv_volumes")
    op.drop_index("ix_cv_volumes_name", table_name="cv_volumes")
    op.drop_table("cv_volumes")
    op.drop_table("cv_publishers")
