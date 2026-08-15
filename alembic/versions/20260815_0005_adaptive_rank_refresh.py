"""Add durable adaptive rank refresh state.

Revision ID: 20260815_0005
Revises: 20260815_0004
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0005"
down_revision = "20260815_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        sa.Column("last_known_division", sa.String(length=4), nullable=True),
        sa.Column("last_known_league_points", sa.Integer(), nullable=True),
        sa.Column("last_known_wins", sa.Integer(), nullable=True),
        sa.Column("last_known_losses", sa.Integer(), nullable=True),
        sa.Column("last_known_inactive", sa.Boolean(), nullable=True),
        sa.Column("rank_last_activity_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rank_schedule_class", sa.String(length=16), nullable=True),
        sa.Column("rank_schedule_reason", sa.String(length=64), nullable=True),
        sa.Column("rank_proposed_interval_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "rank_unranked_confirmations",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("rank_tier_change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rank_counter_reset_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "rank_role_sync_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("rank_role_sync_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rank_role_sync_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rank_role_sync_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rank_manual_refresh_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rank_user_refresh_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_failures", sa.Integer(), nullable=False, server_default="0"),
    ]
    for column in columns:
        op.add_column("verification_links", column)

    op.add_column(
        "verification_sessions",
        sa.Column("verification_link_created_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_verification_links_rank_role_sync_due",
        "verification_links",
        ["guild_id", "rank_role_sync_next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("rank_role_sync_pending"),
    )
    op.create_index(
        "ix_verification_links_deletion_due",
        "verification_links",
        ["guild_id", "deletion_next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("deletion_requested_at IS NOT NULL"),
    )
    op.create_table(
        "verification_audit_cleanups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("verification_session_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_id",
            "message_id",
            name="uq_verification_audit_cleanup_message",
        ),
    )
    op.create_index(
        "ix_verification_audit_cleanup_due",
        "verification_audit_cleanups",
        ["next_attempt_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "verification_marker_cleanups",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("guild_id", "discord_user_id"),
    )
    op.create_index(
        "ix_verification_marker_cleanup_due",
        "verification_marker_cleanups",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_verification_marker_cleanup_due",
        table_name="verification_marker_cleanups",
    )
    op.drop_table("verification_marker_cleanups")
    op.drop_index(
        "ix_verification_audit_cleanup_due",
        table_name="verification_audit_cleanups",
    )
    op.drop_table("verification_audit_cleanups")
    op.drop_column("verification_sessions", "verification_link_created_at")
    op.drop_index(
        "ix_verification_links_deletion_due",
        table_name="verification_links",
    )
    op.drop_index(
        "ix_verification_links_rank_role_sync_due",
        table_name="verification_links",
    )
    for column_name in [
        "deletion_failures",
        "deletion_next_attempt_at",
        "deletion_claimed_at",
        "deletion_requested_at",
        "rank_user_refresh_requested_at",
        "rank_manual_refresh_requested_at",
        "rank_role_sync_failures",
        "rank_role_sync_next_attempt_at",
        "rank_role_sync_claimed_at",
        "rank_role_sync_pending",
        "rank_counter_reset_count",
        "rank_tier_change_count",
        "rank_unranked_confirmations",
        "rank_proposed_interval_seconds",
        "rank_schedule_reason",
        "rank_schedule_class",
        "rank_last_activity_observed_at",
        "last_known_inactive",
        "last_known_losses",
        "last_known_wins",
        "last_known_league_points",
        "last_known_division",
    ]:
        op.drop_column("verification_links", column_name)
