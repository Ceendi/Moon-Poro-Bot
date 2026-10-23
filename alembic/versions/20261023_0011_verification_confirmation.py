"""Queue the Discord confirmation of a completed verification.

Revision ID: 20261023_0011
Revises: 20261017_0010
"""

import sqlalchemy as sa

from alembic import op

revision = "20261023_0011"
down_revision = "20261017_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing sessions stay NULL, so the upgrade sends no late confirmations.
    op.add_column(
        "verification_sessions",
        sa.Column("confirmation_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_verification_sessions_confirmation_due",
        "verification_sessions",
        ["confirmation_due_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_verification_sessions_confirmation_due", "verification_sessions")
    op.drop_column("verification_sessions", "confirmation_due_at")
