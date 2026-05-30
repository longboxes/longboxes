"""local entities + supplement targets on file_matches

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-21 12:00:00.000000

Phase 11 (11A). Adds ``local_volumes`` / ``local_issues`` — user-authored
library entities for comics with no ComicVine record — and three
polymorphic target columns on ``file_matches`` so a single row can resolve
a file to a CV issue, a local issue, or a supplement attached to a CV
volume. A CHECK constraint enforces that at most one target is set.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# CHECK-constraint value sets, kept as constants so upgrade/downgrade
# can't drift.
_STATUS_OLD = "('auto', 'confirmed', 'pending', 'rejected', 'unmatched')"
_STATUS_NEW = (
    "('auto', 'confirmed', 'pending', 'rejected', 'unmatched', "
    "'local', 'supplement')"
)
_SOURCE_OLD = "('comicinfo_cvid', 'filename', 'manual')"
_SOURCE_NEW = "('comicinfo_cvid', 'filename', 'manual', 'local')"


def upgrade() -> None:
    op.create_table(
        "local_volumes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("publisher_name", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.create_table(
        "local_issues",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "local_volume_id",
            UUID(as_uuid=True),
            sa.ForeignKey("local_volumes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issue_number", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("cover_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_local_issues_local_volume_id", "local_issues", ["local_volume_id"]
    )

    # Polymorphic target columns on file_matches. ``issue_cv_id`` (from
    # migration 0005) is the CV-issue target; these add the local-issue
    # and supplement targets.
    op.add_column(
        "file_matches",
        sa.Column(
            "local_issue_id",
            UUID(as_uuid=True),
            sa.ForeignKey("local_issues.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "file_matches",
        sa.Column(
            "supplement_volume_cv_id",
            sa.BigInteger(),
            sa.ForeignKey("cv_volumes.cv_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "file_matches",
        sa.Column("supplement_type", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_file_matches_local_issue_id", "file_matches", ["local_issue_id"]
    )
    op.create_index(
        "ix_file_matches_supplement_volume_cv_id",
        "file_matches",
        ["supplement_volume_cv_id"],
    )

    # At most one of the three targets may be set on a row.
    op.create_check_constraint(
        "ck_file_matches_single_target",
        "file_matches",
        "(issue_cv_id IS NOT NULL)::int "
        "+ (local_issue_id IS NOT NULL)::int "
        "+ (supplement_volume_cv_id IS NOT NULL)::int <= 1",
    )

    # Widen the status + source CHECKs for the new enum values.
    op.drop_constraint("ck_file_matches_status", "file_matches", type_="check")
    op.create_check_constraint(
        "ck_file_matches_status", "file_matches", f"status IN {_STATUS_NEW}"
    )
    op.drop_constraint("ck_file_matches_source", "file_matches", type_="check")
    op.create_check_constraint(
        "ck_file_matches_source", "file_matches", f"source IN {_SOURCE_NEW}"
    )


def downgrade() -> None:
    op.drop_constraint("ck_file_matches_source", "file_matches", type_="check")
    op.create_check_constraint(
        "ck_file_matches_source", "file_matches", f"source IN {_SOURCE_OLD}"
    )
    op.drop_constraint("ck_file_matches_status", "file_matches", type_="check")
    op.create_check_constraint(
        "ck_file_matches_status", "file_matches", f"status IN {_STATUS_OLD}"
    )
    op.drop_constraint(
        "ck_file_matches_single_target", "file_matches", type_="check"
    )
    op.drop_index(
        "ix_file_matches_supplement_volume_cv_id", table_name="file_matches"
    )
    op.drop_index("ix_file_matches_local_issue_id", table_name="file_matches")
    op.drop_column("file_matches", "supplement_type")
    op.drop_column("file_matches", "supplement_volume_cv_id")
    op.drop_column("file_matches", "local_issue_id")
    op.drop_index("ix_local_issues_local_volume_id", table_name="local_issues")
    op.drop_table("local_issues")
    op.drop_table("local_volumes")
