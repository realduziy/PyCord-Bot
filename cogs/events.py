import asyncio
import logging
import platform
import time

import discord
from colorama import Back, Fore, Style
from discord import app_commands
from discord.ext import commands, tasks

from cog_utils import (
    TicketConfig,
    backup_database,
    cleanup_stale_entries,
    close_session,
    ensure_guild_config,
    log_event,
)
from ui_common import (
    EMOJI,
    base_embed,
)
from cogs.automod import prune_spam_history
from cogs._hub import (
    QUICK_SETUP_PROMPT,
    QuickSetupView,
    SetupHubView,
    _format_message,
    build_setup_hub_embed,
)

log = logging.getLogger("duziy-bot.events")


def _member_embed(title: str, color: str, member: discord.Member) -> discord.Embed:
    embed = base_embed(title, color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Account created",
                    value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Members now", value=member.guild.member_count, inline=True)
    return embed


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_cleanup.start()
        self.db_backup.start()

    async def cog_unload(self):
        self.db_cleanup.cancel()
        self.db_backup.cancel()
        # Tidy up the shared HTTP session (discord.py 2.x awaits async cog_unload)
        await close_session()

    @tasks.loop(hours=6)
    async def db_cleanup(self):
        """Prune DB rows/refs pointing at deleted channels, roles and guilds."""
        removed = await cleanup_stale_entries(self.bot)
        prune_spam_history()  # auto-mod's per-user message buffers
        if removed:
            log.info("DB cleanup removed stale entries: %s", removed)

    @db_cleanup.before_loop
    async def before_db_cleanup(self):
        await self.bot.wait_until_ready()
        # Let guild/channel caches fully settle before the first run so
        # nothing is judged "stale" off an incomplete cache
        await asyncio.sleep(120)

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
        # Heal any guild that has no config row (e.g. invited while the bot
        # was crashed or offline) — one row per guild, forever
        for guild in self.bot.guilds:
            try:
                await ensure_guild_config(str(guild.id))
                cfg = await TicketConfig.fetch(str(guild.id))
                log.info(
                    "Guild %r (%s): log=%s tickets=%s verify=%s automod=%s",
                    guild.name, guild.id,
                    "set" if cfg.log_channel else "NONE",
                    "set" if cfg.ticket_category else "NONE",
                    "on" if cfg.verification_enabled else "off",
                    "on" if cfg.automod_enabled else "off",
                )
            except Exception:
                log.exception("Failed to ensure config for guild %s", guild.id)

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
        await log_event(member.guild, "member_join", _member_embed(
            "\U0001f7e2 Member Joined", "success", member))

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
        await log_event(member.guild, "member_leave", _member_embed(
            "\U0001f534 Member Left", "danger", member))

    # --- Setup ---
    @app_commands.command(name="setup", description="Check this server's bot configuration and open setup panels.")
    async def setup_check(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await build_setup_hub_embed(interaction.guild),
            view=SetupHubView(), ephemeral=True)

    @app_commands.command(name="setup-auto", description="Automatically create and configure channels, roles and categories.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_auto(self, interaction: discord.Interaction):
        await interaction.response.send_message(QUICK_SETUP_PROMPT,
                                                view=QuickSetupView(), ephemeral=True)

    @app_commands.command(name="backup", description="Create a backup of the bot database right now.")
    @app_commands.checks.has_permissions(administrator=True)
    async def backup_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        dest = await backup_database()
        if dest is None:
            await interaction.followup.send(
                f"{EMOJI['no']} Backup failed — check the console for details.", ephemeral=True)
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
        prune_spam_history()
        if not removed:
            await interaction.followup.send(f"{EMOJI['ok']} Database is clean — no stale entries found.",
                                            ephemeral=True)
            return
        lines = [f"**{table}**: {count}" for table, count in removed.items()]
        embed = base_embed("\U0001f9f9 Database Cleanup", "success",
                           "Removed stale entries:\n" + "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Events(bot))
