"""Persist verification deletion policy and audit channel identity.

Revision ID: 20260824_0007
Revises: 20260823_0006
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260824_0007"
down_revision = "20260823_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    context = op.get_context()
    bind = op.get_bind()
    # Keep the preflight count and the backfill in one stable snapshot. Without
    # this lock, a concurrent legacy verification could insert an audit message
    # after the count and before the new constraint is installed.
    bind.execute(sa.text("LOCK TABLE verification_links IN ACCESS EXCLUSIVE MODE"))
    existing_audit_messages = int(
        bind.scalar(
            sa.text(
                """
                SELECT COUNT(*)
                FROM verification_links
                WHERE message_id IS NOT NULL
                """
            )
        )
        or 0
    )
    audit_channel_id = context.config.attributes.get("legacy_audit_channel_id")
    if existing_audit_messages:
        try:
            audit_channel_id = int(audit_channel_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "legacy_audit_channel_id is required to migrate existing verification audit "
                "messages"
            ) from error
        if audit_channel_id <= 0:
            raise RuntimeError(
                "legacy_audit_channel_id must be a positive Discord channel ID when audit "
                "messages exist"
            )

    op.add_column(
        "verification_links",
        sa.Column("audit_channel_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "verification_links",
        sa.Column(
            "deletion_remove_rank_region_roles",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    if existing_audit_messages:
        bind.execute(
            sa.text(
                """
                UPDATE verification_links
                SET audit_channel_id = :audit_channel_id
                WHERE message_id IS NOT NULL
                  AND audit_channel_id IS NULL
                """
            ),
            {"audit_channel_id": audit_channel_id},
        )
    op.create_check_constraint(
        "ck_verification_links_audit_message_channel",
        "verification_links",
        "message_id IS NULL OR audit_channel_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_verification_links_audit_message_channel",
        "verification_links",
        type_="check",
    )
    op.drop_column("verification_links", "deletion_remove_rank_region_roles")
    op.drop_column("verification_links", "audit_channel_id")
