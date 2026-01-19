import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get

import config
from functions import has_rank_roles, has_server_roles, has_other_roles, get_member_rank_roles
from cogs.weryfikacja import WeryfikacjaModal
from utils.constants import ROLE_ZWERYFIKOWANY, ROLE_UZYTKOWNIK, ROLE_NPKWL, SERVER_TRANSLATION
from utils.helpers import has_role
from utils.errors import handle_app_command_error


def _check_npkwl(member: discord.Member) -> bool:
    return has_role(member, ROLE_NPKWL)


def _check_zweryfikowany(member: discord.Member) -> bool:
    return has_role(member, ROLE_ZWERYFIKOWANY)


async def toggle_role(interaction: discord.Interaction, role_name: str, linked_role: str = None) -> None:
    role = get(interaction.guild.roles, name=role_name)
    if not role:
        await interaction.response.send_message("❌ Rola nie istnieje.", ephemeral=True)
        return
    if role in interaction.user.roles:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(f"❌ Usunięto rolę **{role}**.", ephemeral=True)
        if linked_role == ROLE_UZYTKOWNIK:
            linked = get(interaction.guild.roles, name=linked_role)
            if linked:
                await interaction.user.remove_roles(linked)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"✅ Dodano rolę **{role}**.", ephemeral=True)
        if linked_role == ROLE_UZYTKOWNIK:
            linked = get(interaction.guild.roles, name=linked_role)
            if linked:
                await interaction.user.add_roles(linked)


async def update_uzytkownik_role(interaction: discord.Interaction) -> None:
    uzytkownik = get(interaction.guild.roles, name=ROLE_UZYTKOWNIK)
    if not uzytkownik:
        return
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        return
    should_have = has_server_roles(member) and has_rank_roles(member)
    has_uzytkownik = uzytkownik in member.roles
    if should_have and not has_uzytkownik:
        await member.add_roles(uzytkownik)
    elif not should_have and has_uzytkownik:
        await member.remove_roles(uzytkownik)


class RankSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(max_values=1, placeholder="Aktualna dywizja solo/duo (najwyższa)", custom_id="ranks", options=[
        discord.SelectOption(label="Unranked", emoji='<:unranked:930537559988264960>'),
        discord.SelectOption(label="Iron", emoji='<:iron:930539302851579934>'),
        discord.SelectOption(label="Bronze", emoji='<:bronze:930537590552158249>'),
        discord.SelectOption(label="Silver", emoji='<:silver:930537622131073114>'),
        discord.SelectOption(label="Gold", emoji='<:gold:930537644222464011>'),
        discord.SelectOption(label="Platinum", emoji='<:platinum2:1131348018675863633>'),
        discord.SelectOption(label="Emerald", emoji='<:emerald:1131341463083548713>'),
        discord.SelectOption(label="Diamond", emoji='<:diamond:930537731573026877>'),
        discord.SelectOption(label="Master", emoji='<:master:930537748736139294>'),
        discord.SelectOption(label="GrandMaster", emoji='<:grandmaster:930537757808398366>'),
        discord.SelectOption(label="Challenger", emoji='<:challenger:930537769699262525>'),
    ])
    async def ranks(self, interaction: discord.Interaction, select: discord.ui.Select):
        if _check_zweryfikowany(interaction.user):
            await interaction.response.send_message("Posiadasz rolę **Zweryfikowany**, która automatycznie aktualizuje Ci rolę co 24h!", ephemeral=True)
            return
        if _check_npkwl(interaction.user):
            await interaction.response.send_message(f"❌ Nie możesz dostać roli ligowej posiadając rolę **{ROLE_NPKWL}**.", ephemeral=True)
            return
        
        selected_rank = select.values[0]
        new_role = get(interaction.guild.roles, name=selected_rank)
        if not new_role:
            await interaction.response.send_message("❌ Rola nie istnieje.", ephemeral=True)
            return
        
        old_ranks = get_member_rank_roles(interaction.user)
        if new_role in old_ranks:
            await interaction.response.send_message(f"Już posiadasz rolę **{new_role}**.", ephemeral=True)
            return
        
        for old_role in old_ranks:
            await interaction.user.remove_roles(old_role)
        await interaction.user.add_roles(new_role)
        
        msg = f"🔄 Zmieniono rolę **{old_ranks[0]}** na **{new_role}**." if old_ranks else f"➕ Wybrano rolę **{new_role}**."
        await interaction.response.send_message(msg, ephemeral=True)
        await update_uzytkownik_role(interaction)


class ServerButtons(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _handle_server_button(self, interaction: discord.Interaction, server_name: str):
        if _check_npkwl(interaction.user):
            await interaction.response.send_message(f"❌ Nie możesz dostać roli ligowej posiadając rolę **{ROLE_NPKWL}**.", ephemeral=True)
            return
        if _check_zweryfikowany(interaction.user):
            data = await self.bot.pool.fetch('SELECT server FROM zweryfikowani WHERE id=$1;', interaction.user.id)
            if data:
                verified_server = SERVER_TRANSLATION.get(data[0]['server'])
                if verified_server == server_name:
                    await interaction.response.send_message("❌ Masz zweryfikowane konto na tym regionie.", ephemeral=True)
                    return
        await toggle_role(interaction, server_name)
        await update_uzytkownik_role(interaction)

    @discord.ui.button(label="EUNE", style=discord.ButtonStyle.red, custom_id="eune")
    async def eune(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_server_button(interaction, "EUNE")

    @discord.ui.button(label="EUW", style=discord.ButtonStyle.red, custom_id="euw")
    async def euw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_server_button(interaction, "EUW")

    @discord.ui.button(label="NA", style=discord.ButtonStyle.red, custom_id="na")
    async def na(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_server_button(interaction, "NA")


class OptionalRoles(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _handle_position(self, interaction: discord.Interaction, position: str):
        if _check_npkwl(interaction.user):
            await interaction.response.send_message(f"❌ Nie możesz dostać roli posiadając rolę **{ROLE_NPKWL}**.", ephemeral=True)
            return
        await toggle_role(interaction, position)
        await update_uzytkownik_role(interaction)

    @discord.ui.button(label="Top", style=discord.ButtonStyle.green, custom_id="top", row=0)
    async def top(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_position(interaction, "Top")

    @discord.ui.button(label="Jungle", style=discord.ButtonStyle.green, custom_id="jungle", row=0)
    async def jungle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_position(interaction, "Jungle")

    @discord.ui.button(label="Mid", style=discord.ButtonStyle.green, custom_id="mid", row=0)
    async def mid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_position(interaction, "Mid")

    @discord.ui.button(label="ADC", style=discord.ButtonStyle.green, custom_id="adc", row=0)
    async def adc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_position(interaction, "ADC")

    @discord.ui.button(label="Support", style=discord.ButtonStyle.green, custom_id="support", row=0)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_position(interaction, "Support")

    @discord.ui.button(label="TFT", style=discord.ButtonStyle.blurple, custom_id="tft", row=1)
    async def tft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_role(interaction, "TFT")

    @discord.ui.button(label="LOR", style=discord.ButtonStyle.blurple, custom_id="lor", row=1)
    async def lor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_role(interaction, "LOR")

    @discord.ui.button(label="Valorant", style=discord.ButtonStyle.blurple, custom_id="valorant", row=1)
    async def valorant(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_role(interaction, "Valorant")

    @discord.ui.button(label="Wild Rift", style=discord.ButtonStyle.blurple, custom_id="wild_rift", row=1)
    async def wild_rift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_role(interaction, "Wild Rift")

    @discord.ui.button(label="Ogłoszenia", style=discord.ButtonStyle.gray, custom_id="ogloszenia", row=2)
    async def ogloszenia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_role(interaction, "Ogłoszenia")

    @discord.ui.button(label="Lol Newsy", style=discord.ButtonStyle.gray, custom_id="lolkowe_newsy", row=2)
    async def lol_newsy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_role(interaction, "Lol Newsy")


class MiscButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Usuń wszystkie role", style=discord.ButtonStyle.gray, custom_id="usun_w_role", row=1)
    async def remove_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        is_verified = _check_zweryfikowany(interaction.user)
        
        removable = config.lol_other + config.OPTIONAL_ROLES
        if not is_verified:
            removable += config.lol_ranks + config.lol_servers + [ROLE_UZYTKOWNIK, ROLE_NPKWL]
        
        roles_to_remove = [r for r in interaction.user.roles if r.name in removable]
        if roles_to_remove:
            await interaction.user.remove_roles(*roles_to_remove)
        
        msg = "✅ Usunięto wszystkie role poza dywizją i regionem." if is_verified else "✅ Usunięto wszystkie role!"
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Nie posiadam konta w lolu", style=discord.ButtonStyle.gray, custom_id="npkwl", row=0)
    async def npkwl(self, interaction: discord.Interaction, button: discord.ui.Button):
        if has_rank_roles(interaction.user) or has_server_roles(interaction.user) or has_other_roles(interaction.user):
            await interaction.response.send_message(f"❌ Nie możesz dostać roli **{ROLE_NPKWL}** posiadając role ligowe.", ephemeral=True)
            return
        if _check_zweryfikowany(interaction.user):
            await interaction.response.send_message(f"❌ Nie możesz dostać roli **{ROLE_NPKWL}** posiadając rolę Zweryfikowany!", ephemeral=True)
            return
        await toggle_role(interaction, ROLE_NPKWL, ROLE_UZYTKOWNIK)


class VerificationButton(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.cooldowns = commands.CooldownMapping.from_cooldown(1.0, config.VERIFICATION_COOLDOWN, lambda i: i.user)

    @discord.ui.button(label="🔐 Weryfikacja", style=discord.ButtonStyle.red, custom_id="weryfikacja")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        retry_after = self.cooldowns.update_rate_limit(interaction)
        if retry_after:
            await interaction.response.send_message(f"⏳ Spróbuj ponownie za {int(retry_after)}s!", ephemeral=True)
            return
        if _check_zweryfikowany(interaction.user):
            await interaction.response.send_message("**Już jesteś zweryfikowany.** Użyj komendy `/usun_weryfikacje`.", ephemeral=True)
            return
        await interaction.response.send_modal(WeryfikacjaModal(self.bot))


class RoleAssignmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(RankSelect())
        bot.add_view(ServerButtons(bot))
        bot.add_view(OptionalRoles(bot))
        bot.add_view(MiscButtons())
        bot.add_view(VerificationButton(bot))

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="przyznawanie_roli", description="Wysyła przyciski do przyznawania ról")
    async def przyznawanie_roli(self, interaction: discord.Interaction):
        await interaction.response.send_message(content='**Role Obowiązkowe**\nDywizja:', view=RankSelect())
        await interaction.channel.send(content="Region:", view=ServerButtons(self.bot))
        await interaction.channel.send(content="»»————-\n**Role opcjonalne**", view=OptionalRoles(self.bot))
        await interaction.channel.send(content='»»————-', view=MiscButtons())
        await interaction.channel.send(content="»»————-")

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="weryfikacja", description="Wysyła przycisk do weryfikacji")
    async def weryfikacja(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            content="»»————-\n**Weryfikacja konta w lolu**\nPrzypisuje twoje konto do discorda i automatyczne aktualizuje role wraz ze zmianą dywizji! Nikt nie widzi twojego nicku (w tym moderacja).\nWymagana by brać udział we wszelkich konkursach/giveaway'ach/turniejach oraz w rekrutacji.\n__Moderacja zastrzega sobie prawo do wymagania weryfikacji od danego użytkownika.__\n»»————-",
            view=VerificationButton(self.bot)
        )

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="start", description="Wysyła przyciski startowe")
    async def start(self, interaction: discord.Interaction):
        await interaction.response.send_message(content='**Role Obowiązkowe**\nDywizja:', view=RankSelect())
        await interaction.channel.send(content="Region:", view=ServerButtons(self.bot))
        await interaction.channel.send(content='»»————-', view=MiscButtons())
        await interaction.channel.send(content='»»————-')

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if not await handle_app_command_error(interaction, error):
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleAssignmentCog(bot), guild=discord.Object(id=config.guild_id))