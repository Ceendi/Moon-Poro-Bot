"""Route durable verification completion by authentication method.

Revision ID: 20261017_0009
Revises: 20261017_0008
"""

import sqlalchemy as sa

from alembic import op

revision = "20261017_0009"
down_revision = "20261017_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verification_sessions",
        sa.Column("verification_method", sa.String(32), nullable=False, server_default="RSO"),
    )


def downgrade() -> None:
    op.drop_column("verification_sessions", "verification_method")
