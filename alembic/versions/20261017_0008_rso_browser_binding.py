"""Bind RSO callbacks to browsers and persist pending audit publication.

Revision ID: 20261017_0008
Revises: 20260824_0007
"""

import sqlalchemy as sa

from alembic import op

revision = "20261017_0008"
down_revision = "20260824_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing links remain unchanged. Old, unbound OAuth sessions fail closed.
    op.add_column(
        "verification_sessions", sa.Column("browser_binding_hash", sa.String(64), nullable=True)
    )
    op.add_column(
        "verification_sessions",
        sa.Column("audit_pending_channel_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("verification_sessions", "audit_pending_channel_id")
    op.drop_column("verification_sessions", "browser_binding_hash")
