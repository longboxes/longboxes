"""files + file_locations: content/location split per §7 v0.5

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-15 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("archive_format", sa.String(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "comicinfo_status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column(
            "excluded_from_matching",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("first_scanned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_files_sha256", "files", ["sha256"])
    op.create_check_constraint(
        "ck_files_comicinfo_status",
        "files",
        "comicinfo_status IN ('none', 'partial', 'full_with_cvid')",
    )
    op.create_check_constraint(
        "ck_files_archive_format",
        "files",
        "archive_format IS NULL OR archive_format IN ('cbz', 'cbr', 'cb7', 'pdf')",
    )

    op.create_table(
        "file_locations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_file_locations_path", "file_locations", ["path"])
    # "list every location of this content" lookup
    op.create_index("ix_file_locations_file_id", "file_locations", ["file_id"])
    # Phase 2 reconciliation: "find locations not visited this scan"
    op.create_index("ix_file_locations_last_seen_at", "file_locations", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_file_locations_last_seen_at", table_name="file_locations")
    op.drop_index("ix_file_locations_file_id", table_name="file_locations")
    op.drop_constraint("uq_file_locations_path", "file_locations", type_="unique")
    op.drop_table("file_locations")
    op.drop_constraint("ck_files_archive_format", "files", type_="check")
    op.drop_constraint("ck_files_comicinfo_status", "files", type_="check")
    op.drop_constraint("uq_files_sha256", "files", type_="unique")
    op.drop_table("files")
