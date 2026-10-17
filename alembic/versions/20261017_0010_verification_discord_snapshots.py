"""Store Discord recipient snapshots for verification confirmation.

Revision ID: 20261017_0010
Revises: 20261017_0009
"""

import sqlalchemy as sa

from alembic import op

revision = "20261017_0010"
down_revision = "20261017_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verification_sessions", sa.Column("discord_username", sa.String(100), nullable=True)
    )
    op.add_column(
        "verification_sessions", sa.Column("discord_guild_name", sa.String(100), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("verification_sessions", "discord_guild_name")
    op.drop_column("verification_sessions", "discord_username")
