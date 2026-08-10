from __future__ import annotations

from functools import cached_property

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_RANKS = [
    "Unranked",
    "Iron",
    "Bronze",
    "Silver",
    "Gold",
    "Platinum",
    "Emerald",
    "Diamond",
    "Master",
    "GrandMaster",
    "Challenger",
]


class Settings(BaseSettings):
    """Validated deployment configuration.

    Secrets and installation-specific Discord IDs come from the environment. Role
    names have portable defaults and can be overridden; production deployments
    should populate ``ROLE_IDS`` so renamed roles remain stable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    discord_token: SecretStr
    riot_api_token: SecretStr

    postgres_user: str
    postgres_password: SecretStr
    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    database_pool_size: int = Field(default=3, ge=1, le=10)
    database_max_overflow: int = Field(default=2, ge=0, le=10)

    guild_id: int = Field(gt=0)
    warn_channel_id: int | None = None
    zweryfikowani_channel_id: int | None = None
    szukanie_gry_channel_id: int | None = None
    komendy_botowe_channel_id: int | None = None
    mod_alert_channel_id: int | None = None

    verification_enabled: bool = True
    roles_enabled: bool = True
    warnings_enabled: bool = True
    mod_stats_enabled: bool = True
    member_logs_enabled: bool = True
    account_age_gate_enabled: bool = False
    boost_alert_enabled: bool = False
    clash_filter_enabled: bool = False

    minimum_account_age_days: int = Field(default=90, ge=0, le=3650)
    verification_timeout: int = Field(default=120, ge=30, le=900)
    verification_cooldown: int = Field(default=30, ge=1, le=3600)
    view_timeout: int = Field(default=180, ge=30, le=3600)
    rank_refresh_interval_hours: int = Field(default=24, ge=1, le=168)
    verification_access_log_retention_days: int = Field(default=90, ge=30, le=3650)
    privacy_policy_url: AnyHttpUrl | None = None

    verified_role_name: str = "Zweryfikowany"
    member_role_name: str = "Użytkownik"
    no_lol_role_name: str = "Nie posiadam konta w lolu"
    role_ids: dict[str, int] = Field(default_factory=dict)

    lol_servers: list[str] = Field(default_factory=lambda: ["EUNE", "EUW", "NA"])
    lol_ranks: list[str] = Field(default_factory=lambda: list(DEFAULT_RANKS))
    lol_positions: list[str] = Field(
        default_factory=lambda: ["Top", "Jungle", "Mid", "ADC", "Support", "Szukam Gry"]
    )
    optional_roles: list[str] = Field(
        default_factory=lambda: [
            "TFT",
            "LOR",
            "Valorant",
            "Dyskusje",
            "Lol Newsy",
            "Ogłoszenia",
            "Wild Rift",
        ]
    )
    warn_roles: dict[int, str] = Field(
        default_factory=lambda: {1: "Warn", 2: "Warn 2", 3: "TIMEOUT"}
    )
    warn_days: dict[str, int] = Field(
        default_factory=lambda: {"Warn": 7, "Warn 2": 14, "TIMEOUT": 3}
    )
    boost_keywords: list[str] = Field(
        default_factory=lambda: [
            "boost",
            "wbije rangę",
            "wbije range",
            "bost",
            "pomogę z",
            "pomoge z",
            "za free",
            "tanio",
        ]
    )

    @field_validator(
        "warn_channel_id",
        "zweryfikowani_channel_id",
        "szukanie_gry_channel_id",
        "komendy_botowe_channel_id",
        "mod_alert_channel_id",
        mode="before",
    )
    @classmethod
    def zero_is_missing(cls, value: object) -> object:
        return None if value in (None, "", 0, "0") else value

    @field_validator("role_ids")
    @classmethod
    def role_ids_must_be_positive(cls, value: dict[str, int]) -> dict[str, int]:
        invalid = [name for name, role_id in value.items() if role_id <= 0]
        if invalid:
            raise ValueError(f"Role IDs must be positive for: {', '.join(invalid)}")
        return value

    @model_validator(mode="after")
    def validate_feature_dependencies(self) -> Settings:
        required: list[tuple[bool, int | None, str]] = [
            (self.verification_enabled, self.zweryfikowani_channel_id, "ZWERYFIKOWANI_CHANNEL_ID"),
            (self.warnings_enabled, self.warn_channel_id, "WARN_CHANNEL_ID"),
            (self.member_logs_enabled, self.komendy_botowe_channel_id, "KOMENDY_BOTOWE_CHANNEL_ID"),
            (self.boost_alert_enabled, self.mod_alert_channel_id, "MOD_ALERT_CHANNEL_ID"),
            (self.clash_filter_enabled, self.szukanie_gry_channel_id, "SZUKANIE_GRY_CHANNEL_ID"),
        ]
        missing = [name for enabled, value, name in required if enabled and value is None]
        if missing:
            raise ValueError(f"Enabled features require: {', '.join(missing)}")
        expected_levels = {1, 2, 3}
        if set(self.warn_roles) != expected_levels:
            raise ValueError("WARN_ROLES must define exactly levels 1, 2 and 3")
        missing_durations = set(self.warn_roles.values()) - set(self.warn_days)
        if missing_durations:
            raise ValueError(
                "WARN_DAYS is missing durations for: " + ", ".join(sorted(missing_durations))
            )
        invalid_durations = [
            role_name for role_name, days in self.warn_days.items() if days < 1 or days > 3650
        ]
        if invalid_durations:
            raise ValueError(
                "Warning durations must be between 1 and 3650 days for: "
                + ", ".join(sorted(invalid_durations))
            )
        return self

    @cached_property
    def requires_message_content(self) -> bool:
        return self.boost_alert_enabled or self.clash_filter_enabled

    @cached_property
    def allowed_role_names(self) -> frozenset[str]:
        return frozenset(
            self.lol_servers
            + self.lol_ranks
            + self.lol_positions
            + self.optional_roles
            + [self.member_role_name, self.no_lol_role_name]
        )
