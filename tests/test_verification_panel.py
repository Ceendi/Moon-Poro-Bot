from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.cogs import verification
from moon_poro.cogs.verification import (
    DeleteVerificationConfirmationView,
    VerificationCog,
    VerificationStartView,
    _remove_user_verification,
    _request_rank_refresh_from_panel,
    _show_delete_confirmation,
)
from moon_poro.cogs.verification_legacy import (
    LegacyVerificationCog,
    LegacyVerificationRateLimiter,
    LegacyVerificationStartView,
)
from moon_poro.repositories import RankRefreshRequestResult, RankRefreshRequestStatus


class FakeMember:
    def __init__(self, user_id: int, *, guild: object | None = None) -> None:
        self.id = user_id
        self.guild = guild
        self.roles: list[object] = []
        self.remove_roles = AsyncMock()


def _panel_bot() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_cooldown=30,
            verification_global_rate_limit=4,
            verification_global_rate_period_seconds=10,
            rank_refresh_button_cooldown_seconds=1800,
        )
    )


def test_rso_and_legacy_views_are_persistent_and_keep_old_start_ids() -> None:
    bot = _panel_bot()
    limiter = LegacyVerificationRateLimiter(global_rate=4, global_period_seconds=10)
    rso_view = VerificationStartView(bot)
    legacy_view = LegacyVerificationStartView(bot, limiter)

    assert rso_view.timeout is None
    assert legacy_view.timeout is None
    assert {item.custom_id for item in rso_view.children} == {
        "verification:start:rso:v1",
        "verification:rank-refresh:v1",
        "verification:delete:v1",
    }
    assert {item.custom_id for item in legacy_view.children} == {
        "verification:start:profile-icon:v1",
        "verification:rank-refresh:v1",
        "verification:delete:v1",
    }
    assert [item.label for item in rso_view.children] == [
        "Zweryfikuj konto",
        "Odśwież rangę",
        "Usuń weryfikację",
    ]


async def test_rso_persistent_start_rejects_another_guild() -> None:
    bot = _panel_bot()
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(guild_id=999, response=response)

    await VerificationStartView(bot).children[0].callback(interaction)

    response.send_message.assert_awaited_once_with(
        "Weryfikację rozpocznij na skonfigurowanym serwerze.", ephemeral=True
    )


async def test_rso_start_blocks_reverification_while_delete_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=123)
    member = FakeMember(101, guild=guild)
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    pending_delete = SimpleNamespace(deletion_requested_at=datetime.now(UTC))
    sessions = SimpleNamespace(create=AsyncMock())
    bot = _panel_bot()
    bot.settings.verified_role_name = "Zweryfikowany"
    bot.settings.role_ids = {}
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=pending_delete))
    bot.verification_sessions = sessions
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        guild=guild,
        user=member,
        response=response,
    )

    await VerificationStartView(bot).children[0].callback(interaction)

    assert "już zapisane powiązanie" in response.send_message.await_args.args[0]
    sessions.create.assert_not_awaited()


async def test_rso_publishes_short_factual_embed() -> None:
    bot = _panel_bot()
    bot.settings.privacy_policy_url = "https://moonporo.pl/privacy/"
    bot.settings.rso_base_url = "https://moonporo.pl"
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

    await VerificationCog.publish_verification.callback(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["embed"].title == "Weryfikacja konta League of Legends"
    assert "oficjalne logowanie Riot" in kwargs["embed"].description
    assert "dywiz" not in kwargs["embed"].description.lower()
    assert isinstance(kwargs["view"], VerificationStartView)


async def test_legacy_publishes_icon_verification_embed() -> None:
    bot = _panel_bot()
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog.rate_limiter = LegacyVerificationRateLimiter(global_rate=4, global_period_seconds=10)
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

    await LegacyVerificationCog.publish_verification.callback(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["embed"].title == "Weryfikacja konta League of Legends"
    assert "ikoną profilu" in kwargs["embed"].description
    assert "tier Solo/Duo" in kwargs["embed"].fields[0].value
    assert isinstance(kwargs["view"], LegacyVerificationStartView)


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (RankRefreshRequestStatus.ENQUEUED, "do kolejki"),
        (RankRefreshRequestStatus.ALREADY_DUE, "już w kolejce"),
        (RankRefreshRequestStatus.ALREADY_CLAIMED, "już trwa"),
        (RankRefreshRequestStatus.BACKOFF_ACTIVE, "automatycznie"),
        (RankRefreshRequestStatus.NOT_LINKED, "Najpierw zweryfikuj"),
    ],
)
async def test_refresh_button_returns_private_queue_status(
    monkeypatch: pytest.MonkeyPatch,
    status: RankRefreshRequestStatus,
    fragment: str,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(return_value=RankRefreshRequestResult(status))
    )
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _request_rank_refresh_from_panel(bot, interaction)

    bot.verifications.request_rank_refresh.assert_awaited_once_with(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
    )
    assert fragment in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_refresh_button_reports_persistent_cooldown_in_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.COOLDOWN,
                retry_after_seconds=1201,
            )
        )
    )
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _request_rank_refresh_from_panel(bot, interaction)

    assert "21 min" in interaction.response.send_message.await_args.args[0]


async def test_delete_button_requires_ephemeral_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=object()))
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _show_delete_confirmation(bot, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    view = kwargs["view"]
    assert isinstance(view, DeleteVerificationConfirmationView)
    assert {item.label for item in view.children} == {"Tak, usuń powiązanie", "Anuluj"}


async def test_delete_confirmation_calls_the_shared_removal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    remove = AsyncMock(return_value="Usunięto.")
    monkeypatch.setattr(verification, "_remove_user_verification", remove)
    view = DeleteVerificationConfirmationView(bot, owner_id=101)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101),
        response=SimpleNamespace(edit_message=AsyncMock()),
    )
    confirm = next(item for item in view.children if item.custom_id.endswith(":confirm:v1"))

    await confirm.callback(interaction)

    remove.assert_awaited_once_with(bot, interaction)
    interaction.response.edit_message.assert_awaited_once_with(content="Usunięto.", view=None)


async def test_shared_delete_keeps_region_rank_and_member_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = object()
    region = object()
    rank = object()
    member_role = object()
    audit_delete = AsyncMock()
    channel = SimpleNamespace(
        get_partial_message=Mock(return_value=SimpleNamespace(delete=audit_delete))
    )
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    member = FakeMember(101, guild=guild)
    member.roles = [verified, region, rank, member_role]
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    monkeypatch.setattr(verification.discord.abc, "Messageable", type(channel))
    monkeypatch.setattr(verification, "find_role", Mock(return_value=verified))
    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        message_id=456,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verified_role_name="Zweryfikowany",
            zweryfikowani_channel_id=789,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=SimpleNamespace(
            request_verification_deletion=AsyncMock(return_value=link),
            finalize_verification_deletion=AsyncMock(return_value=True),
            retry_verification_deletion=AsyncMock(),
        ),
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )
    interaction = SimpleNamespace(guild_id=123, user=member, guild=guild)

    message = await _remove_user_verification(bot, interaction)

    member.remove_roles.assert_awaited_once_with(
        verified, reason="Usunięcie weryfikacji przez użytkownika"
    )
    assert region in member.roles and rank in member.roles and member_role in member.roles
    assert "pozostają bez zmian" in message
    audit_delete.assert_awaited_once()
    bot.verifications.finalize_verification_deletion.assert_awaited_once_with(
        123,
        101,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=created_at,
    )


async def test_shared_delete_reports_pending_and_retries_when_discord_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    guild = SimpleNamespace(get_channel=Mock(return_value=None))
    member = FakeMember(101, guild=guild)
    verified = object()
    member.roles = [verified]
    member.remove_roles = AsyncMock(side_effect=FakeHTTPException())
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification, "find_role", Mock(return_value=verified))
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        message_id=456,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
    )
    repository = SimpleNamespace(
        request_verification_deletion=AsyncMock(return_value=link),
        retry_verification_deletion=AsyncMock(return_value=300),
        finalize_verification_deletion=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verified_role_name="Zweryfikowany",
            zweryfikowani_channel_id=789,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=repository,
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )
    interaction = SimpleNamespace(guild_id=123, user=member, guild=guild)

    message = await _remove_user_verification(bot, interaction)

    assert "w kolejce" in message
    repository.retry_verification_deletion.assert_awaited_once_with(
        123,
        101,
        expected_created_at=created_at,
        base_delay_seconds=300,
    )
    repository.finalize_verification_deletion.assert_not_awaited()


async def test_shared_delete_retries_audit_cleanup_before_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    audit_delete = AsyncMock(side_effect=FakeHTTPException())
    channel = SimpleNamespace(
        get_partial_message=Mock(return_value=SimpleNamespace(delete=audit_delete))
    )
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    member = FakeMember(101, guild=guild)
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification.discord.abc, "Messageable", type(channel))
    monkeypatch.setattr(verification, "find_role", Mock(return_value=None))
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        message_id=456,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
    )
    repository = SimpleNamespace(
        request_verification_deletion=AsyncMock(return_value=link),
        retry_verification_deletion=AsyncMock(return_value=300),
        finalize_verification_deletion=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verified_role_name="Zweryfikowany",
            zweryfikowani_channel_id=789,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=repository,
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )

    message = await _remove_user_verification(
        bot, SimpleNamespace(guild_id=123, user=member, guild=guild)
    )

    assert "w kolejce" in message
    repository.retry_verification_deletion.assert_awaited_once()
    repository.finalize_verification_deletion.assert_not_awaited()
