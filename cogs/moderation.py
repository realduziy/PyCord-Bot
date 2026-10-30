import asyncio
import datetime
import re
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import (
    DB_FILE,
    add_lockdown_channel,
    clear_active_lockdown,
    clear_lockdown_channels,
    get_active_lockdown,
    get_all_active_lockdowns,
    get_lockdown_channels,
    get_recent_cases,
    log_case,
    log_event,
    mark_automod_deleted,
    record_lockdown,
    remove_lockdown_channel,
)
from ui_common import EMOJI, base_embed, truncate


def _actor_can_act(actor: discord.Member, target) -> bool:
    """Role-hierarchy guard: actor must be above the target (if target is a member)."""
    if isinstance(target, discord.Member):
        if target.id == actor.id:
            return False
        if target.top_role >= actor.top_role:
            return False
    return True


def _bot_can_act(guild: discord.Guild, target) -> bool:
    """The bot's highest role must be above the target's."""
    if isinstance(target, discord.Member) and guild.me.top_role <= target.top_role:
        return False
    return True


# Discord only permits bulk-deleting messages younger than 14 days. Deleting
# older messages one-by-one is allowed, but doing it too quickly hits the
# per-channel DELETE rate limit, so those deletions are deliberately paced.
# Leave a small safety buffer below Discord's hard 14-day bulk-delete limit so
# clock differences cannot cause an API rejection at the boundary.
_BULK_DELETE_MAX_AGE = datetime.timedelta(days=13, hours=23)
_OLD_MESSAGE_DELETE_DELAY = 1.05


async def _bulk_delete_messages(channel: discord.TextChannel,
                                messages: list[discord.Message]) -> tuple[int, int]:
    """Bulk-delete messages, returning ``(deleted, already_gone)``.

    A message can be removed by another moderator/auto-mod after we fetch it
    but before the bulk request lands. ``NotFound`` is therefore treated as a
    per-message miss instead of failing the whole ``/clear`` command.
    """
    if not messages:
        return 0, 0

    # Prevent the raw message-delete logger from posting one embed per message
    # if a bulk request has to fall back to individual deletes below.
    for message in messages:
        mark_automod_deleted(message.id)

    try:
        await channel.delete_messages(messages)
        return len(messages), 0
    except discord.NotFound:
        deleted = already_gone = 0
        for index, message in enumerate(messages):
            if index:
                await asyncio.sleep(_OLD_MESSAGE_DELETE_DELAY)
            try:
                await message.delete()
                deleted += 1
            except discord.NotFound:
                already_gone += 1
        return deleted, already_gone


async def _delete_old_messages(messages: list[discord.Message]) -> tuple[int, int]:
    """Pace individual deletes for messages that are too old to bulk-delete."""
    deleted = already_gone = 0
    for index, message in enumerate(messages):
        # Mark before sleeping/deleting so the delete logger does not emit a
        # separate log embed (and API request) for every purged message.
        mark_automod_deleted(message.id)
        if index:
            await asyncio.sleep(_OLD_MESSAGE_DELETE_DELAY)
        try:
            await message.delete()
            deleted += 1
        except discord.NotFound:
            already_gone += 1
    return deleted, already_gone


async def _modlog(guild, action, actor, target, reason=None, extra=None):
    """Write a moderation action to the DB (case log) and the configured log channel."""
    target_id = str(getattr(target, "id", target))
    case_id = await log_case(str(guild.id), action, str(actor.id), target_id, reason)

    embed = base_embed(f"\U0001f6e1\ufe0f {action} \u00b7 Case #{case_id}", "danger")
    embed.add_field(name="Moderator", value=actor.mention, inline=True)
    target_value = target.mention if hasattr(target, "mention") else str(target)
    embed.add_field(name="Target", value=target_value, inline=True)
    if reason:
        embed.add_field(name="Reason", value=truncate(reason), inline=False)
    if extra:
        embed.add_field(name="Details", value=truncate(extra), inline=False)
    sent = await log_event(guild, "mod_action", embed)
    if sent is not None:
        # Store the actual log message so staff can edit the case later.
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE mod_cases SET log_channel_id = ?, log_message_id = ? "
                "WHERE case_id = ? AND guild_id = ?",
                (str(sent.channel.id), str(sent.id), case_id, str(guild.id)),
            )
            await db.commit()
    return case_id


async def _lock_channel(channel: discord.TextChannel, locked_by: str) -> str:
    """Lock one channel, remembering its previous state. Returns 'locked'|'already'|'error'."""
    guild_id = str(channel.guild.id)
    existing = await get_active_lockdown(guild_id, str(channel.id))
    if existing is not None:
        return "already"

    overwrite = channel.overwrites_for(channel.guild.default_role)
    prev_send = overwrite.send_messages  # True, False, or None (inherit)
    if prev_send is False:
        return "already"

    overwrite.send_messages = False
    try:
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
    except (discord.Forbidden, discord.HTTPException):
        return "error"
    await record_lockdown(guild_id, str(channel.id),
                          None if prev_send is None else int(bool(prev_send)), locked_by)
    return "locked"


async def _unlock_channel(channel: discord.TextChannel) -> str:
    """Restore a locked channel to its previous state. Returns 'unlocked'|'not_locked'|'error'."""
    guild_id = str(channel.guild.id)
    record = await get_active_lockdown(guild_id, str(channel.id))
    if record is None:
        # Not tracked — still make sure @everyone can send again
        overwrite = channel.overwrites_for(channel.guild.default_role)
        if overwrite.send_messages is False:
            overwrite.send_messages = None
            try:
                await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
            except (discord.Forbidden, discord.HTTPException):
                return "error"
        return "unlocked"

    prev_send, _, _ = record
    overwrite = channel.overwrites_for(channel.guild.default_role)
    if prev_send is None:
        overwrite.send_messages = None
    else:
        overwrite.send_messages = bool(prev_send)
    try:
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
    except (discord.Forbidden, discord.HTTPException):
        return "error"
    await clear_active_lockdown(guild_id, str(channel.id))
    return "unlocked"


def _lockdown_log_embed(title: str, color: int, actor: discord.Member,
                        channels: list[discord.TextChannel]) -> discord.Embed:
    color_key = "danger" if color == 0xed4245 else "success"
    embed = base_embed(title, color_key)
    embed.add_field(name="Moderator", value=actor.mention, inline=True)
    embed.add_field(name="Channels", value=" ".join(c.mention for c in channels) or "*none*",
                    inline=False)
    return embed


class LockdownGroup(app_commands.Group):
    """Lockdown commands: start/end lockdowns and manage default channels."""

    def __init__(self, cog):
        super().__init__(name="lockdown", description="Lock and unlock channels.")
        self.cog = cog

    @app_commands.command(name="start", description="Lock channels (defaults to your configured channels, else this channel).")
    @app_commands.describe(channels="Channels to lock — leave empty to use your default lockdown channels")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def start(self, interaction: discord.Interaction,
                    channels: Optional[str] = None):
        # Accept either mentions or rely on defaults/current channel
        mentioned = [c for c in (interaction.channel,)]
        if channels:
            ids = [int(i) for i in re.findall(r"\d+", channels)]
            resolved = [interaction.guild.get_channel(i) for i in ids]
            mentioned = [c for c in resolved if isinstance(c, discord.TextChannel)]
            if not mentioned:
                await interaction.response.send_message(
                    "No valid text channels found — mention them like #general.", ephemeral=True)
                return
        else:
            defaults = await get_lockdown_channels(str(interaction.guild.id))
            if defaults:
                mentioned = [c for c in
                             (interaction.guild.get_channel(int(cid)) for cid in defaults)
                             if isinstance(c, discord.TextChannel)]
            if not mentioned and isinstance(interaction.channel, discord.TextChannel):
                mentioned = [interaction.channel]

        locked, skipped = [], []
        for channel in mentioned:
            result = await _lock_channel(channel, str(interaction.user.id))
            (locked if result == "locked" else skipped).append(channel)

        if locked:
            names = ", ".join(c.mention for c in locked)
            await interaction.response.send_message(f"\U0001f512 Locked: {names}")
            await log_event(interaction.guild, "lockdown_start", _lockdown_log_embed(
                "\U0001f512 Lockdown Started", 0xed4245, interaction.user, locked))
        else:
            await interaction.response.send_message(
                "Nothing to lock — those channels are already locked or I lack permissions.",
                ephemeral=True)

    @app_commands.command(name="end", description="Unlock channels (defaults to all currently locked channels).")
    @app_commands.describe(channels="Channels to unlock — leave empty to unlock every locked channel")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def end(self, interaction: discord.Interaction, channels: Optional[str] = None):
        if channels:
            ids = [int(i) for i in re.findall(r"\d+", channels)]
            targets = [interaction.guild.get_channel(i) for i in ids]
            targets = [c for c in targets if isinstance(c, discord.TextChannel)]
        else:
            # Unlock everything currently tracked as locked
            ids = await get_all_active_lockdowns(str(interaction.guild.id))
            targets = [c for c in
                       (interaction.guild.get_channel(int(cid)) for cid in ids)
                       if isinstance(c, discord.TextChannel)]

        if not targets:
            await interaction.response.send_message(
                "No locked channels found.", ephemeral=True)
            return

        unlocked, failed = [], []
        for channel in targets:
            (unlocked if await _unlock_channel(channel) == "unlocked" else failed).append(channel)

        if unlocked:
            names = ", ".join(c.mention for c in unlocked)
            await interaction.response.send_message(f"\U0001f513 Unlocked: {names}")
            await log_event(interaction.guild, "lockdown_end", _lockdown_log_embed(
                "\U0001f513 Lockdown Ended", 0x57f287, interaction.user, unlocked))
        else:
            await interaction.response.send_message(
                "Couldn't unlock those channels — check my permissions.", ephemeral=True)

    @app_commands.command(name="defaults", description="Add, remove, list or clear the default channels lockdown uses.")
    @app_commands.describe(action="What to do", channel="The channel (only needed for add/remove)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List", value="list"),
        app_commands.Choice(name="Clear", value="clear"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def defaults(self, interaction: discord.Interaction,
                       action: app_commands.Choice[str],
                       channel: Optional[discord.TextChannel] = None):
        guild_id = str(interaction.guild.id)

        if action.value == "list":
            ids = await get_lockdown_channels(guild_id)
            if not ids:
                await interaction.response.send_message(
                    "No default lockdown channels set. When `/lockdown start` is run without "
                    "channels, it falls back to the current channel.", ephemeral=True)
                return
            mentions = [interaction.guild.get_channel(int(cid)) for cid in ids]
            lines = [c.mention if c else f"`{cid}` (deleted)" for c, cid in zip(mentions, ids)]
            embed = discord.Embed(title="\U0001f512 Default Lockdown Channels",
                                  description="\n".join(lines), color=0x5865f2)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if action.value == "clear":
            await clear_lockdown_channels(guild_id)
            await interaction.response.send_message(
                "\U0001f5d1\ufe0f Default lockdown channels cleared.", ephemeral=True)
            return

        if channel is None:
            await interaction.response.send_message(
                "Pick a channel for add/remove.", ephemeral=True)
            return

        if action.value == "add":
            await add_lockdown_channel(guild_id, str(channel.id))
            await interaction.response.send_message(
                f"\u2705 {channel.mention} added to the default lockdown list.", ephemeral=True)
        else:
            await remove_lockdown_channel(guild_id, str(channel.id))
            await interaction.response.send_message(
                f"\u2705 {channel.mention} removed from the default lockdown list.", ephemeral=True)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lockdown_group = LockdownGroup(self)
        self.bot.tree.add_command(self.lockdown_group)

    async def cog_load(self):
        # Loaded lazily so server_defense can import this module's lock helpers
        # without creating a circular import.
        from cogs.server_defense import LockdownPresetGroup
        self.lockdown_group.add_command(LockdownPresetGroup())

    async def cog_unload(self):
        self.bot.tree.remove_command("lockdown", type=discord.AppCommandType.chat_input)

    @app_commands.command(name="mute", description="Timeout (mute) a member for a number of minutes.")
    @app_commands.describe(member="The member to mute", minutes="Duration in minutes (max 28 days)",
                           reason="Why they are being muted")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member,
                   minutes: app_commands.Range[int, 1, 40320] = 60,
                   reason: str = "No reason provided."):
        if not _actor_can_act(interaction.user, member):
            await interaction.response.send_message(
                "You can't mute someone at or above your role (or yourself).", ephemeral=True)
            return
        if not _bot_can_act(interaction.guild, member):
            await interaction.response.send_message(
                "I can't mute that user — their role is at or above mine.", ephemeral=True)
            return

        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(
            f"{member.mention} has been muted for {minutes} minute(s). Reason: {reason}", ephemeral=True)
        await _modlog(interaction.guild, "Mute", interaction.user, member,
                      reason=reason, extra=f"Duration: {minutes} minute(s)")

    @app_commands.command(name="unmute", description="Remove a timeout (or legacy Muted role).")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        was_timed_out = member.is_timed_out()
        legacy_role = discord.utils.get(interaction.guild.roles, name="Muted")
        had_legacy = legacy_role is not None and legacy_role in member.roles

        if not was_timed_out and not had_legacy:
            await interaction.response.send_message(f"{member.mention} is not muted.", ephemeral=True)
            return

        if was_timed_out:
            await member.timeout(None, reason=f"Unmuted by {interaction.user}")
        if had_legacy:
            await member.remove_roles(legacy_role, reason=f"Unmuted by {interaction.user}")

        await interaction.response.send_message(f"{member.mention} has been unmuted.", ephemeral=True)
        await _modlog(interaction.guild, "Unmute", interaction.user, member)

    @app_commands.command(name="kick", description="Kicks a user.")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, user: discord.User,
                   reason: str = "No reason provided."):
        member = interaction.guild.get_member(user.id)
        if member is not None:
            if not _actor_can_act(interaction.user, member):
                await interaction.response.send_message(
                    "You can't kick someone at or above your role (or yourself).", ephemeral=True)
                return
            if not _bot_can_act(interaction.guild, member):
                await interaction.response.send_message(
                    "I can't kick that user — their role is at or above mine.", ephemeral=True)
                return

        if member is not None:
            # Local import avoids a circular dependency with ModWorkflows.
            from cogs.mod_workflows import snapshot_member_roles
            await snapshot_member_roles(member, "Before kick")
        await interaction.guild.kick(user, reason=reason)
        await interaction.response.send_message(f"{user.mention} has been kicked. Reason: {reason}", ephemeral=True)
        await _modlog(interaction.guild, "Kick", interaction.user, user, reason)

    @app_commands.command(name="ban", description="Bans a user.")
    @app_commands.describe(user="The user to ban", reason="Why they are being banned",
                           delete_message_days="Days of their messages to delete (0-7)")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, user: discord.User,
                  reason: str = "No reason provided.",
                  delete_message_days: app_commands.Range[int, 0, 7] = 0):
        member = interaction.guild.get_member(user.id)
        if member is not None:
            if not _actor_can_act(interaction.user, member):
                await interaction.response.send_message(
                    "You can't ban someone at or above your role (or yourself).", ephemeral=True)
                return
            if not _bot_can_act(interaction.guild, member):
                await interaction.response.send_message(
                    "I can't ban that user — their role is at or above mine.", ephemeral=True)
                return

        if member is not None:
            # Local import avoids a circular dependency with ModWorkflows.
            from cogs.mod_workflows import snapshot_member_roles
            await snapshot_member_roles(member, "Before ban")
        await interaction.guild.ban(user, reason=reason, delete_message_days=delete_message_days)
        await interaction.response.send_message(f"{user.mention} has been banned. Reason: {reason}", ephemeral=True)
        await _modlog(interaction.guild, "Ban", interaction.user, user, reason,
                      extra=f"Deleted {delete_message_days} day(s) of messages")

    @app_commands.command(name="unban", description="Unbans a user.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user: discord.User):
        try:
            await interaction.guild.unban(user)
        except discord.NotFound:
            await interaction.response.send_message(
                f"{user.mention} is not banned in this server.", ephemeral=True)
            return
        await interaction.response.send_message(f"{user.mention} has been unbanned.", ephemeral=True)
        await _modlog(interaction.guild, "Unban", interaction.user, user)

    @app_commands.command(name="clear", description="Clears the specified amount of messages (1-100).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.checks.cooldown(1, 5.0,
                                  key=lambda i: (i.guild_id, i.channel_id))
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command only works in text channels.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        messages = [message async for message in channel.history(limit=amount)]
        if not messages:
            await interaction.followup.send("There are no messages to delete.", ephemeral=True)
            return

        cutoff = datetime.datetime.now(datetime.timezone.utc) - _BULK_DELETE_MAX_AGE
        bulkable = [m for m in messages if m.created_at >= cutoff]
        too_old = [m for m in messages if m.created_at < cutoff]

        bulk_deleted, bulk_missing = await _bulk_delete_messages(channel, bulkable)
        old_deleted, old_missing = await _delete_old_messages(too_old)

        deleted = bulk_deleted + old_deleted
        already_gone = bulk_missing + old_missing
        response = f"Deleted {deleted} message{'s' if deleted != 1 else ''}."
        if too_old:
            response += (f" {len(too_old)} were outside Discord's bulk-delete window and were "
                         "deleted individually to stay under Discord's rate limit.")
        if already_gone:
            response += f" {already_gone} had already been deleted."
        await interaction.followup.send(response, ephemeral=True)

        await _modlog(interaction.guild, "Clear", interaction.user, channel,
                      extra=f"{deleted} messages purged in {channel.mention}")

    @app_commands.command(name="cases", description="Show the most recent moderation cases.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def cases(self, interaction: discord.Interaction):
        def target_mention(target_id: str) -> str:
            """Render channels, roles, and members with their proper mention syntax."""
            try:
                target_value = int(target_id)
            except ValueError:
                return target_id
            if interaction.guild.get_channel(target_value) is not None:
                return f"<#{target_value}>"
            if interaction.guild.get_role(target_value) is not None:
                return f"<@&{target_value}>"
            return f"<@{target_value}>"

        rows = await get_recent_cases(str(interaction.guild.id), 10)
        embed = base_embed(f"{EMOJI['logs']} Recent Mod Cases", "primary")
        if not rows:
            embed.description = "No moderation cases recorded yet."
        else:
            for row in rows:
                reason = row["reason"] or "No reason provided."
                embed.add_field(
                    name=f"Case #{row['case_id']} \u00b7 {row['action']}",
                    value=f"Moderator: <@{row['actor_id']}>\nTarget: {target_mention(row['target_id'])}\nReason: {reason}",
                    inline=True,
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
