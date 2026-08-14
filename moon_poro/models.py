from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WarningStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class VerificationSessionStatus(StrEnum):
    CREATED = "CREATED"
    AWAITING_RIOT = "AWAITING_RIOT"
    PROCESSING_RIOT = "PROCESSING_RIOT"
    VERIFIED_PENDING_DISCORD = "VERIFIED_PENDING_DISCORD"
    APPLYING_DISCORD = "APPLYING_DISCORD"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class VerificationLink(Base):
    __tablename__ = "verification_links"
    __table_args__ = (
        UniqueConstraint("guild_id", "puuid", name="uq_verification_links_guild_puuid"),
        Index(
            "ix_verification_links_rank_refresh_due",
            "guild_id",
            "rank_next_refresh_at",
            postgresql_where=text("puuid IS NOT NULL"),
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    platform: Mapped[str] = mapped_column(String(10), nullable=False)
    puuid: Mapped[str | None] = mapped_column(String(255))
    verification_method: Mapped[str] = mapped_column(String(32), default="PROFILE_ICON")
    last_known_rank: Mapped[str | None] = mapped_column(String(32))
    rank_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rank_refresh_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rank_next_refresh_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rank_refresh_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VerificationSession(Base):
    __tablename__ = "verification_sessions"
    __table_args__ = (
        Index("ix_verification_sessions_user", "guild_id", "discord_user_id", "created_at"),
        Index(
            "ix_verification_sessions_outbox",
            "status",
            "next_attempt_at",
            "updated_at",
        ),
        UniqueConstraint("start_token_hash", name="uq_verification_sessions_start_token_hash"),
        UniqueConstraint("oauth_state_hash", name="uq_verification_sessions_oauth_state_hash"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    oauth_state_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(10))
    puuid: Mapped[str | None] = mapped_column(String(255))
    riot_game_name: Mapped[str | None] = mapped_column(String(100))
    riot_tag_line: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    completion_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationAccessLog(Base):
    __tablename__ = "verification_access_logs"
    __table_args__ = (Index("ix_verification_access_logs_retention", "guild_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger)
    puuid: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Warning(Base):
    __tablename__ = "warnings"
    __table_args__ = (
        Index(
            "uq_warnings_one_active_per_member",
            "guild_id",
            "discord_user_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_warnings_expiration", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reasons: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WarningStatus.ACTIVE.value
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("warnings.id", ondelete="SET NULL"))

    parent: Mapped[Warning | None] = relationship(remote_side=[id])
    moderators: Mapped[list[WarningModerator]] = relationship(
        back_populates="warning", cascade="all, delete-orphan", lazy="selectin"
    )


class WarningModerator(Base):
    __tablename__ = "warning_moderators"

    warning_id: Mapped[int] = mapped_column(
        ForeignKey("warnings.id", ondelete="CASCADE"), primary_key=True
    )
    moderator_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    warning: Mapped[Warning] = relationship(back_populates="moderators")


class ModerationStat(Base):
    __tablename__ = "moderation_stats"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    moderator_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    month: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    warnings_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reports_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GuildFeature(Base):
    __tablename__ = "guild_features"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feature_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
