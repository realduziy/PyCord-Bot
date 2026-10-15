import datetime
import logging
import platform
import re
import time

import discord
from colorama import Back, Fore, Style
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View

from cog_utils import (
    TicketConfig,
    backup_database,
    cleanup_stale_entries,
    ensure_guild_config,
    log_event,
    set_guild_setting,
)
from cogs.voice import _add_hub, _list_hubs, DEFAULT_TEMPLATE

log = logging.getLogger("duziy-bot.events")

# Which /set choices map to channels vs roles vs categories vs free text
CHANNEL_COLUMNS = {"join_channel", "leave_channel", "log_channel",
                   "ticket_panel_channel", "transcripts_channel", "verify_channel"}
ROLE_COLUMNS = {"ticket_support_role", "verified_role"}
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


def _member_embed(title: str, color: int, member: discord.Member) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Account created",
                    value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Members now", value=member.guild.member_count, inline=True)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


async def _value_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest channel/role/category names for the /set value argument."""
    guild = interaction.guild
    if guild is None:
        return []
    focused = interaction.namespace.setting
    column = focused.value if isinstance(focused, app_commands.Choice) else focused
    current = (current or "").lower()

    if column in CHANNEL_COLUMNS:
        pool = [(c.name, str(c.id)) for c in guild.text_channels]
    elif column in ROLE_COLUMNS:
        pool = [(r.name, str(r.id)) for r in guild.roles if not r.is_default()]
    elif column in CATEGORY_COLUMNS:
        pool = [(c.name, str(c.id)) for c in guild.categories]
    else:
        return []

    matches = [item for item in pool if current in item[0].lower()]
    return [app_commands.Choice(name=name[:100], value=value) for name, value in matches[:25]]


class QuickSetupView(View):
    """Confirmation buttons for /setup auto."""

    def __init__(self, cog):
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.button(label="Yes, set it up", style=discord.ButtonStyle.green, emoji="\u2705")
    async def confirm(self, interaction: discord.Interaction, button: Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="\u23f3 Setting everything up...", view=self)
        try:
            summary = await self.cog._run_quick_setup(interaction.guild)
            await interaction.edit_original_response(content=summary, view=self)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="\u274c I don't have permission to create channels/roles. "
                        "Give me the **Manage Channels** and **Manage Roles** permissions and try again.",
                view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="\u274c")
    async def cancel(self, interaction: discord.Interaction, button: Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Quick setup cancelled.", view=self)


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_cleanup.start()
        self.db_backup.start()

    def cog_unload(self):
        self.db_cleanup.cancel()
        self.db_backup.cancel()

    @tasks.loop(hours=6)
    async def db_cleanup(self):
        """Prune DB rows/refs pointing at deleted channels, roles and guilds."""
        removed = await cleanup_stale_entries(self.bot)
        if removed:
            log.info("DB cleanup removed stale entries: %s", removed)

    @db_cleanup.before_loop
    async def before_db_cleanup(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def db_backup(self):
        """Snapshot the database once a day; keeps the newest 7 snapshots."""
        dest = await backup_database()
        if dest is not None:
            log.info("Daily database backup saved: %s", dest.name)

    @db_backup.before_loop
    async def before_db_backup(self):
        await self.bot.wait_until_ready()

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
        if cfg.join_channel:
            channel = member.guild.get_channel(cfg.join_channel)
            if channel:
                msg = _format_message(cfg.join_message or "Welcome {user}!", member)
                try:
                    await channel.send(msg)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        # Also log the join to the log channel (toggleable via /logsettings)
        await log_event(member.guild, "members", _member_embed(
            "\U0001f7e2 Member Joined", 0x57f287, member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await TicketConfig.fetch(str(member.guild.id))
        if cfg.leave_channel:
            channel = member.guild.get_channel(cfg.leave_channel)
            if channel:
                msg = _format_message(cfg.leave_message or "{user} has left the server.", member)
                try:
                    await channel.send(msg)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await log_event(member.guild, "members", _member_embed(
            "\U0001f534 Member Left", 0xed4245, member))

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
        app_commands.Choice(name="Verified role (verification)", value="verified_role"),
        app_commands.Choice(name="Verify channel (verification)", value="verify_channel"),
        app_commands.Choice(name="Join message", value="join_message"),
        app_commands.Choice(name="Leave message", value="leave_message"),
    ])
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.autocomplete(value=_value_autocomplete)
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

    # --- Setup ---
    @app_commands.command(name="setup", description="Check this server's bot configuration.")
    async def setup_check(self, interaction: discord.Interaction):
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        guild = interaction.guild

        def status(value, kind="channel"):
            if not value:
                return "\u274c Not set"
            if kind == "role":
                obj = guild.get_role(value)
                return f"\u2705 {obj.mention}" if obj else "\u26a0\ufe0f Set, but no longer exists"
            if kind == "onoff":
                return "\u2705 Enabled" if value else "\u274c Disabled"
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
        embed.add_field(name="Verified role", value=status(cfg.verified_role, "role"), inline=True)
        verify_value = status(cfg.verification_enabled, "onoff")
        if cfg.verification_enabled:
            mode = "Captcha" if cfg.verify_mode == "captcha" else "One-click"
            verify_value += f" ({mode})"
        embed.add_field(name="Verification", value=verify_value, inline=True)
        embed.add_field(name="Auto-Mod", value=status(cfg.automod_enabled, "onoff"), inline=True)
        hubs = await _list_hubs(str(interaction.guild.id))
        hub_channels = [interaction.guild.get_channel(int(h["channel_id"])) for h in hubs]
        hub_channels = [c for c in hub_channels if c is not None]
        embed.add_field(name="Voice hub",
                        value=f"\u2705 {hub_channels[0].mention} (+{len(hub_channels) - 1} more)"
                        if hub_channels else "\u274c Not set",
                        inline=True)
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
            value="Set anything marked \u274c with `/set`, or run `/setup auto` to create "
                  "everything for you. Then run `/sendticketpanel`.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="backup", description="Create a backup of the bot database right now.")
    @app_commands.checks.has_permissions(administrator=True)
    async def backup_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        dest = await backup_database()
        if dest is None:
            await interaction.followup.send(
                "\u274c Backup failed — check the console for details.", ephemeral=True)
            return
        size_kb = dest.stat().st_size // 1024
        await interaction.followup.send(
            f"\U0001f4be Backup created: `{dest.name}` ({size_kb} KB)\n"
            f"Saved to the `backups/` folder — the newest 7 snapshots are kept.",
            ephemeral=True)

    @app_commands.command(name="cleanupdb", description="Remove database entries pointing at deleted channels, roles or guilds.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cleanup_db(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        removed = await cleanup_stale_entries(self.bot)
        if not removed:
            await interaction.followup.send("\u2705 Database is clean — no stale entries found.",
                                            ephemeral=True)
            return
        lines = [f"**{table}**: {count}" for table, count in removed.items()]
        embed = discord.Embed(title="\U0001f9f9 Database Cleanup", color=0x57f287,
                              description="Removed stale entries:\n" + "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="setup-auto", description="Automatically create and configure channels, roles and categories.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_auto(self, interaction: discord.Interaction):
        view = QuickSetupView(self)
        await interaction.response.send_message(
            "\U0001f6a8 **Quick Setup** will create and configure:\n"
            "\u2022 \U0001f4cb `#bot-logs` channel (logging)\n"
            "\u2022 \U0001f4c4 `#transcripts` channel (ticket transcripts)\n"
            "\u2022 \U0001f39f\ufe0f `#verify` channel (verification panel)\n"
            "\u2022 \U0001f4e9 `Tickets` category + \U0001f6e1\ufe0f `Support Team` role\n"
            "\u2022 \u2705 `Verified` role (assigned on verification)\n"
            "\u2022 \U0001f50a `\u2795 Join to Create` voice hub (temp voice channels)\n"
            "Existing settings you already configured are left untouched. Continue?",
            view=view, ephemeral=True)

    async def _run_quick_setup(self, guild: discord.Guild) -> str:
        guild_id = str(guild.id)
        await ensure_guild_config(guild_id)
        cfg = await TicketConfig.fetch(guild_id)
        created, reused = [], []

        def overwrites():
            perms = discord.PermissionOverwrite(view_channel=True, send_messages=False)
            bot_perms = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            return {guild.default_role: perms, guild.me: bot_perms}

        async def get_or_create_channel(name, **kwargs):
            existing = discord.utils.get(guild.text_channels, name=name)
            if existing:
                reused.append(f"#{name}")
                return existing
            ch = await guild.create_text_channel(name, **kwargs)
            created.append(f"#{name}")
            return ch

        async def get_or_create_role(name, **kwargs):
            existing = discord.utils.get(guild.roles, name=name)
            if existing:
                reused.append(f"@{name}")
                return existing
            role = await guild.create_role(name=name, **kwargs)
            created.append(f"@{name}")
            return role

        # Channels & roles (only fill in what's missing)
        log_ch = guild.get_channel(cfg.log_channel) if cfg.log_channel else None
        if not log_ch:
            log_ch = await get_or_create_channel("bot-logs", overwrites=overwrites())

        transcripts_ch = guild.get_channel(cfg.transcripts_channel) if cfg.transcripts_channel else None
        if not transcripts_ch:
            transcripts_ch = await get_or_create_channel("transcripts", overwrites=overwrites())

        verify_ch = guild.get_channel(cfg.verify_channel) if cfg.verify_channel else None
        if not verify_ch:
            verify_ch = await get_or_create_channel("verify")

        support_role = guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None
        if not support_role:
            support_role = await get_or_create_role("Support Team", color=discord.Color.blue())

        verified_role = guild.get_role(cfg.verified_role) if cfg.verified_role else None
        if not verified_role:
            verified_role = await get_or_create_role("Verified", color=discord.Color.green())

        category = guild.get_channel(cfg.ticket_category) if cfg.ticket_category else None
        if not (category and isinstance(category, discord.CategoryChannel)):
            category = await guild.create_category("Tickets")
            created.append("Tickets (category)")

        # Voice hub: create only if the server has none
        hubs = await _list_hubs(guild_id)
        existing_hubs = [c for h in hubs
                         if (c := guild.get_channel(int(h["channel_id"]))) is not None]
        if not existing_hubs:
            hub_channel = await guild.create_voice_channel(
                "\u2795 Join to Create", reason="Quick setup voice hub")
            await _add_hub(guild_id, str(hub_channel.id), DEFAULT_TEMPLATE)
            created.append("\u2795 Join to Create (voice hub)")

        # Persist everything
        updates = {
            "log_channel": log_ch.id,
            "transcripts_channel": transcripts_ch.id,
            "verify_channel": verify_ch.id,
            "ticket_support_role": support_role.id,
            "verified_role": verified_role.id,
            "ticket_category": category.id,
            "verification_enabled": 1,
        }
        for column, value in updates.items():
            await set_guild_setting(guild_id, column, value)

        lines = ["\u2705 **Quick setup complete!**"]
        if created:
            lines.append("Created: " + ", ".join(created))
        if reused:
            lines.append("Reused: " + ", ".join(reused))
        lines.append("\nNext: run `/sendticketpanel` and `/sendverifypanel` in their channels.")
        return "\n".join(lines)


async def setup(bot):
    await bot.add_cog(Events(bot))