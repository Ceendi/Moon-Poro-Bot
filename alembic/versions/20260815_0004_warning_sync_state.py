"""Track pending Discord synchronization for warnings.

Revision ID: 20260815_0004
Revises: 20260814_0003
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0004"
down_revision = "20260814_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "warnings",
        sa.Column(
            "role_sync_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "warnings",
        sa.Column(
            "audit_sync_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute("UPDATE warnings SET role_sync_pending = true WHERE status = 'ACTIVE'")
    op.alter_column("warnings", "role_sync_pending", server_default=sa.true())
    op.alter_column("warnings", "audit_sync_pending", server_default=sa.true())
    op.create_index(
        "ix_warnings_pending_sync",
        "warnings",
        ["guild_id"],
        unique=False,
        postgresql_where=sa.text("role_sync_pending OR audit_sync_pending"),
    )


def downgrade() -> None:
    op.drop_index("ix_warnings_pending_sync", table_name="warnings")
    op.drop_column("warnings", "audit_sync_pending")
    op.drop_column("warnings", "role_sync_pending")
