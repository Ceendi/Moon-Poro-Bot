from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from moon_poro.cogs.mod_stats import ModStatsCog, StatsPaginator


def make_interaction(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=123,
        guild=SimpleNamespace(get_member=Mock(return_value=None)),
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_stats_paginator_builds_latest_period_and_navigates() -> None:
    member = SimpleNamespace(mention="<@1>")
    guild = SimpleNamespace(
        get_member=Mock(side_effect=lambda user_id: member if user_id == 1 else None)
    )
    view = StatsPaginator(
        {
            (2026, 7): {1: (2, 3)},
            (2026, 8): {1: (4, 5), 2: (0, 0), 3: (1, 0)},
        },
        guild,
        owner_id=1,
        timeout=60,
    )

    embed = view.embed()

    assert embed.title.endswith("2026-08")
    assert embed.footer.text == "Strona 2/2"
    assert {field.name for field in embed.fields} == {"<@1>", "ID: 3"}

    interaction = make_interaction()
    previous = next(item for item in view.children if str(item.emoji) == "◀️")
    following = next(item for item in view.children if str(item.emoji) == "▶️")
    await previous.callback(interaction)
    assert view.page == 0
    await following.callback(interaction)
    assert view.page == 1
    assert interaction.response.edit_message.await_count == 2


async def test_stats_paginator_rejects_other_user() -> None:
    view = StatsPaginator({(2026, 8): {}}, SimpleNamespace(get_member=Mock()), 1, 60)
    interaction = make_interaction(user_id=2)

    assert not await view.interaction_check(interaction)
    interaction.response.send_message.assert_awaited_once_with(
        "To nie jest Twój paginator.", ephemeral=True
    )


async def test_mod_stats_reports_empty_data() -> None:
    bot = SimpleNamespace(
        moderation_stats=SimpleNamespace(list_for_guild=AsyncMock(return_value=[])),
        settings=SimpleNamespace(view_timeout=60),
    )
    cog = ModStatsCog(bot)
    interaction = make_interaction()

    await ModStatsCog.mod_stats.callback(cog, interaction)

    interaction.followup.send.assert_awaited_once_with(
        "Brak danych do wyświetlenia.", ephemeral=True
    )


async def test_mod_stats_sends_paginator() -> None:
    row = SimpleNamespace(
        year=2026,
        month=8,
        moderator_id=1,
        reports_count=2,
        warnings_count=3,
    )
    bot = SimpleNamespace(
        moderation_stats=SimpleNamespace(list_for_guild=AsyncMock(return_value=[row])),
        settings=SimpleNamespace(view_timeout=60),
    )
    cog = ModStatsCog(bot)
    interaction = make_interaction()

    await ModStatsCog.mod_stats.callback(cog, interaction)

    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    assert isinstance(interaction.followup.send.await_args.kwargs["view"], StatsPaginator)
