import platform
import re
import time

import discord
from colorama import Back, Fore, Style
from discord import app_commands
from discord.ext import commands

from cog_utils import TicketConfig, ensure_guild_config, set_guild_setting

# Which /set choices map to channels vs roles vs categories vs free text
CHANNEL_COLUMNS = {"join_channel", "leave_channel", "log_channel",
                   "ticket_panel_channel", "transcripts_channel"}
ROLE_COLUMNS = {"ticket_support_role"}
CATEGORY_COLUMNS = {"ticket_category"}
MESSAGE_COLUMNS = {"join_message", "leave_message"}

MENTION_RE = re.compile(r"^(?:<#(\d+)>|<@&(\d+)>|(\d+))$")


def _resolve_object_id(guild: discord.Guild, column: str, value: str):
    """Resolve a mention, raw ID, or exact name to an object id. None if not found."""
    match = MENTION_RE.match(value.strip())
    if match:
        return int(next(g for g in match.groups() if g))
    if column in CHANNEL_COLUMNS:
        channel = discord.utils.get(guild.text_channels, name=value)
        return channel.id if channel else None
    if column in CATEGORY_COLUMNS:
        category = discord.utils.get(guild.categories, name=value)
        return category.id if category else None
    if column in ROLE_COLUMNS:
        role = discord.utils.get(guild.roles, name=value)
        return role.id if role else None
    return None


def _format_message(template: str, member: discord.Member) -> str:
    """Shared placeholder formatting for join/leave messages."""
    return (template
            .replace("{user}", member.mention)
            .replace("{member_count}", str(member.guild.member_count))
            .replace("{count}", str(member.guild.member_count)))


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        prfx = (Back.BLACK + Fore.GREEN + time.strftime("%H:%M:%S UTC", time.gmtime())
                + Back.RESET + Fore.WHITE + Style.BRIGHT)
        print(prfx + " Logged in as " + Fore.YELLOW + self.bot.user.name)
        print(prfx + " Bot ID " + Fore.YELLOW + str(self.bot.user.id))
        print(prfx + " Discord Version " + Fore.YELLOW + discord.__version__)
        print(prfx + " Python Version " + Fore.YELLOW + str(platform.python_version()))

        await self.bot.change_presence(
            status=discord.Status.online,
            activity=discord.Game("Need help? Run /help"),
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await ensure_guild_config(str(guild.id))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await TicketConfig.fetch(str(member.guild.id))
        if not cfg.join_channel:
            return
        channel = member.guild.get_channel(cfg.join_channel)
        if not channel:
            return
        msg = _format_message(cfg.join_message or "Welcome {user}!", member)
        try:
            await channel.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await TicketConfig.fetch(str(member.guild.id))
        if not cfg.leave_channel:
            return
        channel = member.guild.get_channel(cfg.leave_channel)
        if not channel:
            return
        msg = _format_message(cfg.leave_message or "{user} has left the server.", member)
        try:
            await channel.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # --- Server Configuration Commands ---
    @app_commands.command(name="set", description="Configure a bot setting for this server.")
    @app_commands.describe(
        setting="Which setting to change",
        value="Mention, ID, or name (channels/roles/categories), or text (messages)",
    )
    @app_commands.choices(setting=[
        app_commands.Choice(name="Log channel", value="log_channel"),
        app_commands.Choice(name="Join channel", value="join_channel"),
        app_commands.Choice(name="Leave channel", value="leave_channel"),
        app_commands.Choice(name="Ticket panel channel", value="ticket_panel_channel"),
        app_commands.Choice(name="Transcripts channel", value="transcripts_channel"),
        app_commands.Choice(name="Ticket category", value="ticket_category"),
        app_commands.Choice(name="Ticket support role", value="ticket_support_role"),
        app_commands.Choice(name="Join message", value="join_message"),
        app_commands.Choice(name="Leave message", value="leave_message"),
    ])
    @app_commands.checks.has_permissions(manage_messages=True)
    async def set_config(self, interaction: discord.Interaction,
                         setting: app_commands.Choice[str],
                         value: app_commands.Range[str, 1, 1000]):
        guild_id = str(interaction.guild.id)
        await ensure_guild_config(guild_id)
        column = setting.value

        # Free-text messages: store as-is, show a live preview with placeholders
        if column in MESSAGE_COLUMNS:
            await set_guild_setting(guild_id, column, value)
            preview = _format_message(value, interaction.user)
            await interaction.response.send_message(
                f"{setting.name} updated.\n"
                f"Preview: {preview}\n"
                "Placeholders: `{user}`, `{member_count}` (also accepts `{count}`)",
                ephemeral=True,
            )
            return

        # Channels / roles / categories: resolve mention, ID, or exact name
        raw_id = _resolve_object_id(interaction.guild, column, value)
        if raw_id is None:
            kind = ("role" if column in ROLE_COLUMNS
                    else "category" if column in CATEGORY_COLUMNS else "channel")
            await interaction.response.send_message(
                f"Couldn't find that {kind}. Mention it, paste its ID, or use its exact name.",
                ephemeral=True,
            )
            return

        if column in ROLE_COLUMNS:
            obj = interaction.guild.get_role(raw_id)
        else:
            obj = interaction.guild.get_channel(raw_id)

        if obj is None:
            await interaction.response.send_message(
                "Couldn't resolve that — double-check the mention or ID.", ephemeral=True
            )
            return
        if column in CATEGORY_COLUMNS and not isinstance(obj, discord.CategoryChannel):
            await interaction.response.send_message("That isn't a category.", ephemeral=True)
            return

        await set_guild_setting(guild_id, column, raw_id)
        label = obj.name if column in CATEGORY_COLUMNS else obj.mention
        await interaction.response.send_message(f"{setting.name} set to {label}.", ephemeral=True)

    @app_commands.command(name="setup", description="Check which bot settings are configured in this server.")
    async def setup_check(self, interaction: discord.Interaction):
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        guild = interaction.guild

        def status(value, kind="channel"):
            if not value:
                return "\u274c Not set"
            if kind == "role":
                obj = guild.get_role(value)
                return f"\u2705 {obj.mention}" if obj else "\u26a0\ufe0f Set, but no longer exists"
            obj = guild.get_channel(value)
            if not obj:
                return "\u26a0\ufe0f Set, but no longer exists"
            if kind == "category":
                if isinstance(obj, discord.CategoryChannel):
                    return f"\u2705 {obj.name} (category)"
                return "\u26a0\ufe0f Set, but not a category"
            return f"\u2705 {obj.mention}"

        embed = discord.Embed(title="\u2699\ufe0f Bot Setup Check", color=0x5865f2)
        embed.add_field(name="Log channel", value=status(cfg.log_channel), inline=True)
        embed.add_field(name="Ticket category", value=status(cfg.ticket_category, "category"), inline=True)
        embed.add_field(name="Support role", value=status(cfg.ticket_support_role, "role"), inline=True)
        embed.add_field(name="Panel channel", value=status(cfg.ticket_panel_channel), inline=True)
        embed.add_field(name="Transcripts channel", value=status(cfg.transcripts_channel), inline=True)
        embed.add_field(name="Join channel", value=status(cfg.join_channel), inline=True)
        embed.add_field(name="Leave channel", value=status(cfg.leave_channel), inline=True)
        embed.add_field(
            name="Join message",
            value=cfg.join_message or "\u274c Not set",
            inline=False,
        )
        embed.add_field(
            name="Leave message",
            value=cfg.leave_message or "\u274c Not set",
            inline=False,
        )
        embed.add_field(
            name="Next steps",
            value="Set anything marked \u274c with `/set`, then run `/sendticketpanel`.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Events(bot))
