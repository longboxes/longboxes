"""users: accounts table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        # No server_default for id — the ORM (User.id default=uuid.uuid4)
        # always supplies one, and we avoid taking a hard dependency on
        # pgcrypto / gen_random_uuid() at the DB level.
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_users_username", "users", ["username"])
    # CHECK constraint on role keeps invalid values out at the DB level.
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'viewer')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.drop_table("users")
