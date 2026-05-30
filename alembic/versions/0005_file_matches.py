"""file_matches: bridge between files and cv_issues

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-16 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_matches",
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "issue_cv_id",
            sa.BigInteger(),
            sa.ForeignKey("cv_issues.cv_id"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("candidates", JSONB(), nullable=True),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "matched_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_file_matches_status",
        "file_matches",
        "status IN ('auto', 'confirmed', 'pending', 'rejected', 'unmatched')",
    )
    op.create_check_constraint(
        "ck_file_matches_source",
        "file_matches",
        "source IN ('comicinfo_cvid', 'filename', 'manual')",
    )
    op.create_check_constraint(
        "ck_file_matches_confidence_range",
        "file_matches",
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    )
    op.create_index(
        "ix_file_matches_issue_cv_id", "file_matches", ["issue_cv_id"]
    )
    op.create_index("ix_file_matches_status", "file_matches", ["status"])


def downgrade() -> None:
    op.drop_index("ix_file_matches_status", table_name="file_matches")
    op.drop_index("ix_file_matches_issue_cv_id", table_name="file_matches")
    op.drop_constraint(
        "ck_file_matches_confidence_range", "file_matches", type_="check"
    )
    op.drop_constraint("ck_file_matches_source", "file_matches", type_="check")
    op.drop_constraint("ck_file_matches_status", "file_matches", type_="check")
    op.drop_table("file_matches")
