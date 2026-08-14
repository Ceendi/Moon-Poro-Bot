import pytest
from pydantic import ValidationError

from moon_poro.settings import Settings


def make_settings(**overrides):
    values = {
        "discord_token": "discord-secret",
        "riot_api_token": "riot-secret",
        "postgres_user": "bot",
        "postgres_password": "db-secret",  # pragma: allowlist secret
        "postgres_host": "127.0.0.1",
        "postgres_db": "bot",
        "guild_id": 123,
        "warn_channel_id": 1,
        "zweryfikowani_channel_id": 2,
        "komendy_botowe_channel_id": 3,
        "rso_public_base_url": "https://bot.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_message_content_is_disabled_for_core_features() -> None:
    settings = make_settings()
    assert settings.requires_message_content is False


def test_message_content_is_enabled_for_clash_filter() -> None:
    settings = make_settings(clash_filter_enabled=True, szukanie_gry_channel_id=4)
    assert settings.requires_message_content is True


def test_role_allowlist_contains_all_configured_groups() -> None:
    settings = make_settings()
    assert "Unranked" in settings.allowed_role_names
    assert "EUNE" in settings.allowed_role_names
    assert "Support" in settings.allowed_role_names
    assert "Dyskusje" in settings.allowed_role_names


def test_role_ids_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="Role IDs must be positive"):
        make_settings(role_ids={"Zweryfikowany": 0})


def test_warning_configuration_requires_all_levels() -> None:
    with pytest.raises(ValidationError, match="exactly levels 1, 2 and 3"):
        make_settings(warn_roles={1: "Warn"})


def test_enabled_feature_requires_its_channel() -> None:
    with pytest.raises(ValidationError, match="MOD_ALERT_CHANNEL_ID"):
        make_settings(boost_alert_enabled=True, mod_alert_channel_id=None)


def test_warning_roles_require_matching_durations() -> None:
    with pytest.raises(ValidationError, match="WARN_DAYS is missing"):
        make_settings(
            warn_roles={1: "First", 2: "Second", 3: "Third"},
            warn_days={"First": 7, "Second": 14},
        )


def test_warning_durations_must_stay_within_limit() -> None:
    with pytest.raises(ValidationError, match="between 1 and 3650"):
        make_settings(warn_days={"Warn": 0, "Warn 2": 14, "TIMEOUT": 3})


def test_zero_channel_id_is_normalized_to_missing() -> None:
    settings = make_settings(member_logs_enabled=False, komendy_botowe_channel_id=0)

    assert settings.komendy_botowe_channel_id is None


def test_verification_requires_public_rso_url() -> None:
    with pytest.raises(ValidationError, match="RSO_PUBLIC_BASE_URL"):
        make_settings(rso_public_base_url=None)


def test_profile_icon_verification_does_not_require_rso_url() -> None:
    settings = make_settings(
        verification_mode="legacy_icon",
        rso_public_base_url=None,
    )

    assert settings.verification_mode == "legacy_icon"
    assert settings.rso_public_base_url is None


def test_profile_icon_verification_has_conservative_rate_limits() -> None:
    settings = make_settings(verification_mode="legacy_icon")

    assert settings.verification_cooldown == 30
    assert settings.verification_icon_check_cooldown == 5
    assert settings.verification_global_rate_limit == 4
    assert settings.verification_global_rate_period_seconds == 10


def test_rank_refresh_is_staggered_with_durable_retry_defaults() -> None:
    settings = make_settings()

    assert settings.rank_refresh_interval_hours == 24
    assert settings.rank_refresh_worker_interval_seconds == 10
    assert settings.rank_refresh_retry_base_seconds == 300
    assert settings.rank_refresh_claim_timeout_seconds == 300


def test_public_rso_url_requires_https() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        make_settings(rso_public_base_url="http://bot.example.com")


def test_public_rso_url_rejects_base_path() -> None:
    with pytest.raises(ValidationError, match="must be an origin"):
        make_settings(rso_public_base_url="https://bot.example.com/rso")
