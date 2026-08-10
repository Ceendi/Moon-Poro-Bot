"""Replace the legacy dynamic schema with normalized, auditable tables.

Revision ID: 20260810_0001
Revises:
Create Date: 2026-08-10
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "20260810_0001"
down_revision = None
branch_labels = None
depends_on = None

STAT_COLUMN = re.compile(r"^(?P<kind>[zw])y(?P<year>\d{2})_m(?P<month>\d{2})$")


def upgrade() -> None:
    context = op.get_context()
    guild_id = context.config.attributes.get("guild_id")
    if not guild_id:
        raise RuntimeError("GUILD_ID is required to migrate the legacy single-guild data")

    op.create_table(
        "verification_links",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.String(length=10), nullable=False),
        sa.Column("puuid", sa.String(length=255), nullable=True),
        sa.Column("verification_method", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("guild_id", "discord_user_id"),
        sa.UniqueConstraint("guild_id", "puuid", name="uq_verification_links_guild_puuid"),
    )
    op.create_table(
        "verification_access_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=True),
        sa.Column("puuid", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_verification_access_logs_guild_id", "verification_access_logs", ["guild_id"]
    )
    op.create_index(
        "ix_verification_access_logs_retention",
        "verification_access_logs",
        ["guild_id", "created_at"],
    )
    op.create_table(
        "warnings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("reasons", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["warnings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_warnings_one_active_per_member",
        "warnings",
        ["guild_id", "discord_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index("ix_warnings_expiration", "warnings", ["status", "expires_at"])
    op.create_table(
        "warning_moderators",
        sa.Column("warning_id", sa.BigInteger(), nullable=False),
        sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["warning_id"], ["warnings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("warning_id", "moderator_id"),
    )
    op.create_table(
        "moderation_stats",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column("warnings_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reports_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "moderator_id", "year", "month"),
    )
    op.create_table(
        "guild_features",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("guild_id", "feature_key"),
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "zweryfikowani" in tables:
        bind.execute(
            sa.text(
                """
                INSERT INTO verification_links
                    (guild_id, discord_user_id, message_id, platform, puuid,
                     verification_method, created_at)
                SELECT :guild_id, id, message_id, server,
                       CASE
                           WHEN puuid IS NULL THEN NULL
                           WHEN puuid_position = 1 THEN puuid
                           ELSE NULL
                       END,
                       CASE
                           WHEN puuid IS NULL THEN 'LEGACY_MISSING'
                           WHEN puuid_position = 1 THEN 'PROFILE_ICON'
                           ELSE 'LEGACY_DUPLICATE'
                       END,
                       NOW()
                FROM (
                    SELECT legacy.*,
                           CASE
                               WHEN puuid IS NULL THEN 1
                               ELSE ROW_NUMBER() OVER (PARTITION BY puuid ORDER BY id)
                           END AS puuid_position
                    FROM zweryfikowani AS legacy
                ) AS deduplicated
                """
            ),
            {"guild_id": guild_id},
        )
        op.rename_table("zweryfikowani", "legacy_zweryfikowani")

    if "warn" in tables:
        bind.execute(
            sa.text(
                """
                INSERT INTO warnings
                    (guild_id, discord_user_id, level, reasons, description, starts_at,
                     expires_at, message_id, status, parent_id)
                SELECT :guild_id, id, typ, powod, opis,
                       start AT TIME ZONE 'UTC', koniec AT TIME ZONE 'UTC', message_id,
                       CASE
                           WHEN active IS TRUE AND
                                ROW_NUMBER() OVER (
                                    PARTITION BY id, active
                                    ORDER BY start DESC, koniec DESC, message_id DESC
                                ) = 1
                           THEN 'ACTIVE'
                           ELSE 'SUPERSEDED'
                       END,
                       NULL
                FROM warn
                """
            ),
            {"guild_id": guild_id},
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO warning_moderators (warning_id, moderator_id)
                SELECT DISTINCT new.id, moderator_id
                FROM warnings AS new
                JOIN warn AS old
                  ON new.guild_id = :guild_id
                 AND new.discord_user_id = old.id
                 AND new.level = old.typ
                 AND new.message_id = old.message_id
                 AND new.starts_at = old.start AT TIME ZONE 'UTC'
                CROSS JOIN LATERAL unnest(COALESCE(old.autorzy, ARRAY[]::bigint[])) AS moderator_id
                ON CONFLICT DO NOTHING
                """
            ),
            {"guild_id": guild_id},
        )
        bind.execute(
            sa.text(
                """
                UPDATE warnings AS current
                SET parent_id = (
                    SELECT candidate.id
                    FROM warnings AS candidate
                    WHERE candidate.guild_id = current.guild_id
                      AND candidate.discord_user_id = current.discord_user_id
                      AND candidate.status = 'SUPERSEDED'
                    ORDER BY candidate.expires_at DESC, candidate.id DESC
                    LIMIT 1
                )
                WHERE current.status = 'ACTIVE'
                """
            )
        )
        op.rename_table("warn", "legacy_warn")

    if "mod_stats" in tables:
        columns = [column["name"] for column in inspector.get_columns("mod_stats")]
        periods: dict[tuple[int, int], dict[str, str]] = {}
        for column in columns:
            match = STAT_COLUMN.fullmatch(column)
            if match is None:
                continue
            period = (2000 + int(match["year"]), int(match["month"]))
            periods.setdefault(period, {})[match["kind"]] = column

        for (year, month), kinds in sorted(periods.items()):
            warnings_column = kinds.get("w")
            reports_column = kinds.get("z")
            warnings_expr = f'COALESCE("{warnings_column}", 0)' if warnings_column else "0"
            reports_expr = f'COALESCE("{reports_column}", 0)' if reports_column else "0"
            bind.execute(
                sa.text(
                    f"""
                    INSERT INTO moderation_stats
                        (guild_id, moderator_id, year, month, warnings_count, reports_count)
                    SELECT :guild_id, id, :year, :month, {warnings_expr}, {reports_expr}
                    FROM mod_stats
                    WHERE {warnings_expr} <> 0 OR {reports_expr} <> 0
                    ON CONFLICT (guild_id, moderator_id, year, month) DO UPDATE
                    SET warnings_count = EXCLUDED.warnings_count,
                        reports_count = EXCLUDED.reports_count
                    """
                ),
                {"guild_id": guild_id, "year": year, "month": month},
            )
        op.rename_table("mod_stats", "legacy_mod_stats")

    if "proxy_vc" in tables:
        op.rename_table("proxy_vc", "legacy_proxy_vc")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for legacy, original in (
        ("legacy_proxy_vc", "proxy_vc"),
        ("legacy_mod_stats", "mod_stats"),
        ("legacy_warn", "warn"),
        ("legacy_zweryfikowani", "zweryfikowani"),
    ):
        if legacy in tables and original not in tables:
            op.rename_table(legacy, original)

    op.drop_table("guild_features")
    op.drop_table("moderation_stats")
    op.drop_table("warning_moderators")
    op.drop_index("ix_warnings_expiration", table_name="warnings")
    op.drop_index("uq_warnings_one_active_per_member", table_name="warnings")
    op.drop_table("warnings")
    op.drop_index("ix_verification_access_logs_retention", table_name="verification_access_logs")
    op.drop_index("ix_verification_access_logs_guild_id", table_name="verification_access_logs")
    op.drop_table("verification_access_logs")
    op.drop_table("verification_links")
