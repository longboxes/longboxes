"""file_errors: per-path failure inventory for the admin inspector

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-27 04:00:00.000000

Three failure modes the scanner / cover endpoint / ComicInfo parser
currently swallow into log lines:

* ``archive_open`` — the archive layer raised on open. The file never
  reaches the ``files`` table, so without this row there's no record
  of it on disk at all.
* ``cover_extraction`` — the cover endpoint hit the placeholder
  fallback. The ``files`` row exists; cover columns stay null.
* ``comicinfo_parse`` — ComicInfo.xml present but unparseable. Today
  this collapses into ``comicinfo_status='none'``, indistinguishable
  from "no XML".

One row per (path, kind) — re-failure UPSERTs and refreshes
``last_seen_at`` + the captured error class/message. The unique index
gives us the upsert target. ``file_id`` is nullable because
``archive_open`` failures have no ``files`` row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_errors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("path", sa.String(), nullable=False),
        # 'archive_open' | 'cover_extraction' | 'comicinfo_parse'.
        # Plain string column rather than a Postgres ENUM type so adding
        # a new kind later doesn't require a CREATE TYPE migration.
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("error_class", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The upsert target — one row per (path, kind). Declared as
        # a UNIQUE CONSTRAINT (not just a unique index) so it mirrors
        # the ORM's ``__table_args__ = (UniqueConstraint(...),)`` —
        # both produce a Postgres-side unique constraint that
        # ``INSERT ... ON CONFLICT (path, kind)`` can target.
        sa.UniqueConstraint("path", "kind", name="uq_file_errors_path_kind"),
    )
    op.create_index("ix_file_errors_kind", "file_errors", ["kind"])
    op.create_index("ix_file_errors_path", "file_errors", ["path"])


def downgrade() -> None:
    op.drop_index("ix_file_errors_path", table_name="file_errors")
    op.drop_index("ix_file_errors_kind", table_name="file_errors")
    op.drop_table("file_errors")
