"""Persist a staggered schedule for Riot rank refreshes.

Revision ID: 20260814_0003
Revises: 20260810_0002
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260814_0003"
down_revision = "20260810_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verification_links",
        sa.Column("last_known_rank", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "verification_links",
        sa.Column("rank_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "verification_links",
        sa.Column("rank_refresh_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "verification_links",
        sa.Column(
            "rank_next_refresh_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.add_column(
        "verification_links",
        sa.Column(
            "rank_refresh_failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    op.execute(
        """
        WITH scheduled AS (
            SELECT guild_id,
                   discord_user_id,
                   (ROW_NUMBER() OVER (ORDER BY guild_id, discord_user_id) - 1)::double precision
                       / GREATEST(COUNT(*) OVER (), 1)::double precision AS day_fraction
            FROM verification_links
            WHERE puuid IS NOT NULL
        )
        UPDATE verification_links AS links
        SET rank_next_refresh_at = NOW() + scheduled.day_fraction * INTERVAL '24 hours'
        FROM scheduled
        WHERE links.guild_id = scheduled.guild_id
          AND links.discord_user_id = scheduled.discord_user_id
        """
    )
    op.alter_column(
        "verification_links",
        "rank_next_refresh_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.create_index(
        "ix_verification_links_rank_refresh_due",
        "verification_links",
        ["guild_id", "rank_next_refresh_at"],
        unique=False,
        postgresql_where=sa.text("puuid IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_verification_links_rank_refresh_due",
        table_name="verification_links",
    )
    op.drop_column("verification_links", "rank_refresh_failures")
    op.drop_column("verification_links", "rank_next_refresh_at")
    op.drop_column("verification_links", "rank_refresh_claimed_at")
    op.drop_column("verification_links", "rank_last_checked_at")
    op.drop_column("verification_links", "last_known_rank")
