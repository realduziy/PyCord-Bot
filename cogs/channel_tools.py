import asyncio
import datetime
import json
import re
import time
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cog_utils import DB_FILE, get_staff_bypass_roles, log_event
from ui_common import base_embed, err_embed, ok_embed, truncate


_DURATION_PART = re.compile(r"(\d+)\s*(d|h|m|s)", re.IGNORECASE)
MAX_SLOWMODE_DURATION = datetime.timedelta(hours=24)
MAX_FREEZE_DURATION = datetime.timedelta(days=7)
RESTORE_CHECK_INTERVAL = 15


def parse_duration(value: str, maximum: datetime.timedelta) -> datetime.timedelta:
    """Parse strings such as `30s`, `15m`, `2h`, or `1d`."""
    value = value.strip().lower()
    if not value:
        raise ValueError("Provide a duration such as `15m`, `2h`, or `1d`.")

    if value.isdigit():
        return datetime.timedelta(seconds=int(value))

    total = datetime.timedelta()
    matched = False
    for amount, unit in _DURATION_PART.findall(value):
        matched = True
        amount = int(amount)
        if unit == "d":
            total += datetime.timedelta(days=amount)
        elif unit == "h":
            total += datetime.timedelta(hours=amount)
        elif unit == "m":
            total += datetime.timedelta(minutes=amount)
        else:
            total += datetime.timedelta(seconds=amount)

    # Require the whole string to be duration parts (apart from whitespace).
    stripped = _DURATION_PART.sub("", value).strip()
    if not matched or stripped:
        raise ValueError("Use a duration such as `15m`, `2h`, or `1d`.")

    if total.total_seconds() <= 0:
        raise ValueError("Duration must be greater than zero.")
    if total > maximum:
        raise ValueError(f"Duration cannot exceed {maximum}.")
    return total


def format_duration(delta: datetime.timedelta) -> str:
    seconds = int(delta.total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


async def restore_slowmode(bot: commands.Bot, guild_id: str, channel_id: str,
                           previous_delay: int, actor_id: str, reason: str | None):
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return

    channel = guild.get_channel(int(channel_id))
    actor = guild.get_member(int(actor_id)) if actor_id else None
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.edit(
                slowmode_delay=previous_delay,
                reason="Temporary slowmode expired",
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    await log_event(
        guild,
        "slowmode_end",
        actor,
        channel,
        reason="Temporary slowmode restored",
        extra=f"Previous delay: {previous_delay}s" + (f"\nReason: {reason}" if reason else ""),
    )


async def restore_expired_slowmodes(bot: commands.Bot):
    now = time.time()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT guild_id, channel_id, previous_delay, actor_id, reason "
            "FROM slowmode_restorations WHERE ends_at <= ?",
            (now,),
        ) as cursor:
            expired = await cursor.fetchall()
        await db.executemany(
            "DELETE FROM slowmode_restorations WHERE guild_id = ? AND channel_id = ?",
            [(row[0], row[1]) for row in expired],
        )
        await db.commit()

    for guild_id, channel_id, previous_delay, actor_id, reason in expired:
        await restore_slowmode(
            bot,
            guild_id,
            channel_id,
            int(previous_delay),
            actor_id,
            reason,
        )


def _snapshot_overwrites(channel: discord.TextChannel) -> list[dict]:
    snapshot = []
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue
        send = getattr(overwrite, "send_messages", None)
        if send is not None:
            snapshot.append({"role_id": str(target.id), "send": send})
    return snapshot


async def unfreeze_channel(bot: commands.Bot, guild_id: str, channel_id: str,
                           actor: discord.abc.User | None,
                           reason: str | None = None,
                           log: bool = True) -> bool:
    """Restore a frozen channel. Returns True when an active freeze existed."""
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return False

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT overwrites_json, actor_id, reason FROM channel_freezes "
            "WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        await db.execute(
            "DELETE FROM channel_freezes WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        await db.commit()

    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        snapshot = json.loads(row[0])
        overwrites = channel.overwrites.copy()

        for role in list(overwrites):
            if isinstance(role, discord.Role):
                overwrites[role].send_messages = None

        for item in snapshot:
            role = guild.get_role(int(item["role_id"]))
            if role is None:
                continue
            overwrite = overwrites.get(role)
            if overwrite is None:
                overwrite = discord.PermissionOverwrite()
                overwrites[role] = overwrite
            overwrite.send_messages = bool(item["send"])

        try:
            await channel.edit(overwrites=overwrites, reason="Channel unfrozen")
        except (discord.Forbidden, discord.HTTPException):
            return True

    if log:
        original_actor = (
            guild.get_member(int(row[1]))
            if row[1] else None
        )
        channel = guild.get_channel(int(channel_id))
        await log_event(
            guild,
            "channel_unfreeze",
            actor or original_actor,
            channel,
            reason=reason or "Channel unfrozen",
            extra=(f"Original reason: {row[2]}" if row[2] else None),
        )
    return True


async def restore_expired_freezes(bot: commands.Bot):
    now = time.time()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT guild_id, channel_id, actor_id, reason FROM channel_freezes "
            "WHERE ends_at IS NOT NULL AND ends_at <= ?",
            (now,),
        ) as cursor:
            expired = await cursor.fetchall()
        await db.executemany(
            "DELETE FROM channel_freezes WHERE guild_id = ? AND channel_id = ?",
            [(row[0], row[1]) for row in expired],
        )
        await db.commit()

    for guild_id, channel_id, actor_id, reason in expired:
        actor = None
        guild = bot.get_guild(int(guild_id))
        if guild and actor_id:
            actor = guild.get_member(int(actor_id))
        await unfreeze_channel(
            bot,
            guild_id,
            channel_id,
            actor,
            reason="Temporary channel freeze expired",
        )


class ChannelTools(commands.Cog):
    """Temporary slowmode and channel-freeze utilities."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.restore_task.start()

    async def cog_unload(self):
        self.restore_task.cancel()
        task = self.restore_task.get_task()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM slowmode_restorations WHERE guild_id = ? AND channel_id = ?",
                (str(channel.guild.id), str(channel.id)),
            )
            await db.execute(
                "DELETE FROM channel_freezes WHERE guild_id = ? AND channel_id = ?",
                (str(channel.guild.id), str(channel.id)),
            )
            await db.commit()

    @tasks.loop(seconds=RESTORE_CHECK_INTERVAL)
    async def restore_task(self):
        await restore_expired_slowmodes(self.bot)
        await restore_expired_freezes(self.bot)

    @restore_task.before_loop
    async def before_restore_task(self):
        await self.bot.wait_until_ready()

    slowmode = app_commands.Group(
        name="slowmode",
        description="Manage temporary channel slowmode.",
    )
    freeze = app_commands.Group(
        name="freeze",
        description="Freeze or unfreeze a text channel.",
    )

    @slowmode.command(name="set", description="Apply temporary slowmode to a channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: str,
        interval: app_commands.Range[int, 0, 21600],
        reason: Optional[str] = None,
    ):
        try:
            delta = parse_duration(duration, MAX_SLOWMODE_DURATION)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        previous_delay = channel.slowmode_delay or 0
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO slowmode_restorations (guild_id, channel_id, previous_delay, "
                "ends_at, actor_id, reason) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, channel_id) DO UPDATE SET "
                "previous_delay = excluded.previous_delay, ends_at = excluded.ends_at, "
                "actor_id = excluded.actor_id, reason = excluded.reason",
                (
                    str(interaction.guild.id),
                    str(channel.id),
                    previous_delay,
                    time.time() + delta.total_seconds(),
                    str(interaction.user.id),
                    reason,
                ),
            )
            await db.commit()

        try:
            await channel.edit(
                slowmode_delay=interval,
                reason=f"Temporary slowmode by {interaction.user}: {reason or 'No reason'}",
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't apply slowmode to that channel.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(
                f"Slowmode set to {interval}s in {channel.mention} for "
                f"{format_duration(delta)}."
            ),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "slowmode_start",
            interaction.user,
            channel,
            reason=reason,
            extra=(
                f"Delay: {interval}s\nDuration: {format_duration(delta)}\n"
                f"Previous delay: {previous_delay}s"
            ),
        )

    @slowmode.command(name="status", description="Show active temporary slowmodes.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode_status(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT channel_id, previous_delay, ends_at, actor_id, reason "
                "FROM slowmode_restorations WHERE guild_id = ? ORDER BY ends_at",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🐌 Temporary Slowmode", "neutral", "No active temporary slowmodes."),
                ephemeral=True,
            )
            return

        embed = base_embed("🐌 Active Temporary Slowmodes", "primary")
        for channel_id, previous_delay, ends_at, actor_id, reason in rows[:25]:
            channel = interaction.guild.get_channel(int(channel_id))
            actor = interaction.guild.get_member(int(actor_id)) if actor_id else None
            remaining = max(0, int(ends_at - time.time()))
            value = (
                f"Channel: {channel.mention if channel else '`deleted`'}\n"
                f"Remaining: {format_duration(datetime.timedelta(seconds=remaining))}\n"
                f"Previous delay: {previous_delay}s\n"
                f"Applied by: {actor.mention if actor else '`unknown`'}"
            )
            if reason:
                value += f"\nReason: {truncate(reason, 120)}"
            embed.add_field(name=f"Restore in {format_duration(datetime.timedelta(seconds=remaining))}",
                            value=value, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @slowmode.command(name="cancel", description="Cancel temporary slowmode immediately.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode_cancel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT previous_delay, actor_id, reason FROM slowmode_restorations "
                "WHERE guild_id = ? AND channel_id = ?",
                (str(interaction.guild.id), str(channel.id)),
            ) as cursor:
                row = await cursor.fetchone()
            await db.execute(
                "DELETE FROM slowmode_restorations WHERE guild_id = ? AND channel_id = ?",
                (str(interaction.guild.id), str(channel.id)),
            )
            await db.commit()

        if row is None:
            await interaction.response.send_message(
                "That channel does not have an active temporary slowmode.",
                ephemeral=True,
            )
            return

        try:
            await channel.edit(slowmode_delay=0, reason="Temporary slowmode cancelled")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "The timer was removed, but I couldn't reset the channel slowmode.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(f"Temporary slowmode cancelled in {channel.mention}."),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "slowmode_end",
            interaction.user,
            channel,
            reason="Temporary slowmode cancelled",
            extra=f"Original previous delay was {row[0]}s",
        )

    @freeze.command(name="apply", description="Freeze a text channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def freeze_apply(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        delta = None
        if duration:
            try:
                delta = parse_duration(duration, MAX_FREEZE_DURATION)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT 1 FROM channel_freezes WHERE guild_id = ? AND channel_id = ?",
                (str(interaction.guild.id), str(channel.id)),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    await interaction.response.send_message(
                        "That channel is already frozen.",
                        ephemeral=True,
                    )
                    return

            snapshot = _snapshot_overwrites(channel)
            await db.execute(
                "INSERT INTO channel_freezes (guild_id, channel_id, overwrites_json, "
                "ends_at, actor_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(interaction.guild.id),
                    str(channel.id),
                    json.dumps(snapshot),
                    time.time() + delta.total_seconds() if delta else None,
                    str(interaction.user.id),
                    reason,
                    str(discord.utils.utcnow()),
                ),
            )
            await db.commit()

        overwrites = channel.overwrites.copy()
        default_overwrite = overwrites.get(interaction.guild.default_role)
        if default_overwrite is None:
            default_overwrite = discord.PermissionOverwrite()
            overwrites[interaction.guild.default_role] = default_overwrite
        default_overwrite.send_messages = False

        configured_bypass = await get_staff_bypass_roles(str(interaction.guild.id))
        if configured_bypass:
            staff_roles = [
                role for role_id in configured_bypass
                if (role := interaction.guild.get_role(role_id)) is not None
            ]
        else:
            # Fallback preserves the previous automatic staff detection.
            staff_roles = [
                role for role in interaction.guild.roles
                if role != interaction.guild.default_role and (
                    role.permissions.administrator
                    or role.permissions.manage_guild
                    or role.permissions.manage_channels
                    or role.permissions.manage_messages
                )
            ]
        for role in staff_roles:
            overwrite = overwrites.get(role)
            if overwrite is None:
                overwrite = discord.PermissionOverwrite()
                overwrites[role] = overwrite
            overwrite.send_messages = True

        try:
            await channel.edit(
                overwrites=overwrites,
                reason=f"Channel frozen by {interaction.user}: {reason or 'No reason'}",
            )
        except (discord.Forbidden, discord.HTTPException):
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    "DELETE FROM channel_freezes WHERE guild_id = ? AND channel_id = ?",
                    (str(interaction.guild.id), str(channel.id)),
                )
                await db.commit()
            await interaction.response.send_message(
                "I couldn't freeze that channel.",
                ephemeral=True,
            )
            return

        duration_text = format_duration(delta) if delta else "Until manually unfrozen"
        await interaction.response.send_message(
            embed=ok_embed(f"{channel.mention} has been frozen for {duration_text}."),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "channel_freeze",
            interaction.user,
            channel,
            reason=reason,
            extra=f"Duration: {duration_text}",
        )

    @freeze.command(name="status", description="Show frozen channels.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def freeze_status(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT channel_id, ends_at, actor_id, reason FROM channel_freezes "
                "WHERE guild_id = ? ORDER BY created_at",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🧊 Channel Freezes", "neutral", "No channels are currently frozen."),
                ephemeral=True,
            )
            return

        embed = base_embed("🧊 Frozen Channels", "primary")
        for channel_id, ends_at, actor_id, reason in rows[:25]:
            channel = interaction.guild.get_channel(int(channel_id))
            actor = interaction.guild.get_member(int(actor_id)) if actor_id else None
            if ends_at:
                remaining = max(0, int(ends_at - time.time()))
                duration_text = format_duration(datetime.timedelta(seconds=remaining))
            else:
                duration_text = "Manual"

            value = (
                f"Channel: {channel.mention if channel else '`deleted`'}\n"
                f"Duration: {duration_text}\n"
                f"Frozen by: {actor.mention if actor else '`unknown`'}"
            )
            if reason:
                value += f"\nReason: {truncate(reason, 120)}"
            embed.add_field(name=channel.name if channel else "Deleted channel",
                            value=value, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @freeze.command(name="unfreeze", description="Unfreeze a text channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def freeze_unfreeze(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        reason: Optional[str] = None,
    ):
        restored = await unfreeze_channel(
            self.bot,
            str(interaction.guild.id),
            str(channel.id),
            interaction.user,
            reason=reason,
        )
        if not restored:
            await interaction.response.send_message(
                "That channel is not frozen.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(f"{channel.mention} has been unfrozen."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelTools(bot))
