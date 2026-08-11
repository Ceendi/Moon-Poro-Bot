"""Add short-lived Riot Sign On sessions and a durable Discord outbox.

Revision ID: 20260810_0002
Revises: 20260810_0001
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260810_0002"
down_revision = "20260810_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "verification_links", "message_id", existing_type=sa.BigInteger(), nullable=True
    )
    op.create_table(
        "verification_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("start_token_hash", sa.String(length=64), nullable=False),
        sa.Column("oauth_state_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("platform", sa.String(length=10), nullable=True),
        sa.Column("puuid", sa.String(length=255), nullable=True),
        sa.Column("riot_game_name", sa.String(length=100), nullable=True),
        sa.Column("riot_tag_line", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("completion_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("oauth_state_hash", name="uq_verification_sessions_oauth_state_hash"),
        sa.UniqueConstraint("start_token_hash", name="uq_verification_sessions_start_token_hash"),
    )
    op.create_index(
        "ix_verification_sessions_user",
        "verification_sessions",
        ["guild_id", "discord_user_id", "created_at"],
    )
    op.create_index(
        "ix_verification_sessions_outbox",
        "verification_sessions",
        ["status", "next_attempt_at", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_verification_sessions_outbox", table_name="verification_sessions")
    op.drop_index("ix_verification_sessions_user", table_name="verification_sessions")
    op.drop_table("verification_sessions")
    op.execute("UPDATE verification_links SET message_id = 0 WHERE message_id IS NULL")
    op.alter_column(
        "verification_links", "message_id", existing_type=sa.BigInteger(), nullable=False
    )
