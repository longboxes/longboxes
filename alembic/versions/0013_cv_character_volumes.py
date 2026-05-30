"""cv_character_volumes + character volume-scrape marker

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-24 00:00:00.000000

The ComicVine JSON API's volume data for a character is unreliable, so
the character page's appearance list is built by scraping the
character's public ``issues-cover`` web page. This migration adds:

* ``cv_character_volumes`` — one row per (character, volume) the scrape
  found, carrying the volume's name + cover + scrape order. The whole
  set for a character is replaced wholesale by each scrape.
* ``cv_characters.volumes_scraped_at`` — stamped when that scrape
  completes. Nullable, no default — NULL means "never scraped", so
  existing rows start eligible.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cv_characters",
        sa.Column(
            "volumes_scraped_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    # The composite primary key (character_cv_id, volume_cv_id) already
    # indexes character_cv_id as its leading column, so a "all volumes
    # for this character" lookup needs no extra index.
    op.create_table(
        "cv_character_volumes",
        sa.Column("character_cv_id", sa.BigInteger(), nullable=False),
        sa.Column("volume_cv_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_cv_id"], ["cv_characters.cv_id"]
        ),
        sa.PrimaryKeyConstraint("character_cv_id", "volume_cv_id"),
    )


def downgrade() -> None:
    op.drop_table("cv_character_volumes")
    op.drop_column("cv_characters", "volumes_scraped_at")
