"""Cache the Riot ID on durable verification links.

Revision ID: 20260823_0006
Revises: 20260815_0005
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260823_0006"
down_revision = "20260815_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verification_links",
        sa.Column("riot_game_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "verification_links",
        sa.Column("riot_tag_line", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("verification_links", "riot_tag_line")
    op.drop_column("verification_links", "riot_game_name")
