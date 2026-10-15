import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Select, View

from cog_utils import (
    LOG_EVENTS,
    TicketConfig,
    consume_automod_deleted,
    is_log_event_enabled,
    log_event,
    set_log_event,
)

# Console trace for message events — makes it obvious whether the bot is
# receiving deletes/edits at all (check the console when testing logging).
audit_log = logging.getLogger("duziy-bot.audit")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _base(title: str, color: int) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.timestamp = _now()
    return embed


def _content(value, limit=1024):
    if not value:
        return "*no text content*"
    return value[:limit]


def _channel_label(channel) -> str:
    kind = {discord.TextChannel: "Text", discord.VoiceChannel: "Voice",
            discord.CategoryChannel: "Category", discord.StageChannel: "Stage"}.get(
                type(channel), "Channel")
    return f"{kind} channel"


def _category_name(channel) -> str:
    return channel.category.name if getattr(channel, "category", None) else "None"


async def _status_embed(guild_id: str) -> discord.Embed:
    embed = discord.Embed(title="\U0001f4cb Log Event Status", color=0x5865f2)
    lines = []
    for key, name in LOG_EVENTS.items():
        mark = "\u2705" if await is_log_event_enabled(guild_id, key) else "\u274c"
        lines.append(f"{mark} {name}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Pick an event in the dropdown below to toggle it.")
    embed.timestamp = _now()
    return embed


class LogPanelView(View):
    """Interactive panel: pick an event from the dropdown to toggle it on/off."""

    def __init__(self):
        super().__init__(timeout=300)
        options = [
            discord.SelectOption(label=name[:100], value=key, emoji="\U0001f4cb",
                                 description="Toggle this log event")
            for key, name in LOG_EVENTS.items()
        ][:25]
        self.select = Select(placeholder="Pick an event to toggle...", min_values=1,
                             max_values=1, options=options, custom_id="log_toggle_select")
        self.select.callback = self._toggle
        self.add_item(self.select)

    async def _toggle(self, interaction: discord.Interaction):
        if not self.select.values:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        chosen = self.select.values[0]
        guild_id = str(interaction.guild.id)
        enabled = await is_log_event_enabled(guild_id, chosen)
        await set_log_event(guild_id, chosen, not enabled)
        state = "\u2705 enabled" if not enabled else "\u274c disabled"
        await interaction.response.edit_message(
            embed=await _status_embed(guild_id), view=self)
        await interaction.followup.send(
            f"{state} **{LOG_EVENTS[chosen]}** logging.", ephemeral=True)


class Logging(commands.Cog):
    """Full server audit logging — everything is toggleable via /logsettings."""

    def __init__(self, bot):
        self.bot = bot

    async def _is_log_channel(self, channel) -> bool:
        cfg = await TicketConfig.fetch(str(channel.guild.id))
        return cfg.log_channel == channel.id

    # --- Commands ---
    @app_commands.command(name="logsettings", description="Open the interactive log settings panel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logsettings(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await _status_embed(str(interaction.guild.id)),
            view=LogPanelView(), ephemeral=True)

    @app_commands.command(name="logstatus", description="Show which log events are currently enabled.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logstatus(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await _status_embed(str(interaction.guild.id)), ephemeral=True)

    # --- Messages (raw events: fire even when the message isn't in the cache) ---
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        message = payload.cached_message
        audit_log.info("DELETE captured: #%s | cached=%s | id=%s",
                       channel, message is not None, payload.message_id)
        if message is not None:
            if message.author.bot:
                return
            if consume_automod_deleted(message.id):
                return  # auto-mod already logged this deletion
            if not message.content and not message.attachments:
                return
            embed = _base("\U0001f5d1\ufe0f Message Deleted", 0xed4245)
            embed.add_field(name="Author", value=f"{message.author} (`{message.author.id}`)",
                            inline=True)
            embed.add_field(name="Channel", value=channel.mention, inline=True)
            embed.add_field(name="Content", value=_content(message.content), inline=False)
            if message.attachments:
                embed.add_field(name="Attachments",
                                value="\n".join(a.filename for a in message.attachments)[:1024],
                                inline=False)
        else:
            if consume_automod_deleted(payload.message_id):
                return
            # Discord doesn't send content for old messages — log what we can
            embed = _base("\U0001f5d1\ufe0f Message Deleted", 0xed4245)
            embed.add_field(name="Channel", value=channel.mention, inline=True)
            embed.add_field(name="Author", value="Unknown (not in cache)", inline=True)
            embed.add_field(name="Content",
                            value="*Message was sent before the bot's cache window — "
                                  "content unavailable.*", inline=False)
        await log_event(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None or not payload.message_ids:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        audit_log.info("PURGE captured: #%s | count=%s", channel, len(payload.message_ids))
        user_messages = [m for m in payload.cached_messages
                         if not m.author.bot and not consume_automod_deleted(m.id)]
        embed = _base("\U0001f9f9 Messages Purged", 0xed4245)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Count", value=len(payload.message_ids), inline=True)
        if user_messages:
            sample = "\n".join(f"**{m.author}:** {_content(m.content, 200)}"
                               for m in user_messages[:5])
            embed.add_field(name="Sample", value=sample[:1024], inline=False)
        else:
            embed.add_field(name="Sample",
                            value="*Messages were outside the bot's cache window — "
                                  "content unavailable.*", inline=False)
        await log_event(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        # The raw payload always includes the author and the NEW content
        author = payload.data.get("author") or {}
        if author.get("bot"):
            return
        after_content = payload.data.get("content") or ""
        before_message = payload.cached_message
        before_content = before_message.content if before_message else None
        if before_content == after_content:
            return  # embed/link-preview-only update
        audit_log.info("EDIT captured: #%s | cached_before=%s", channel, before_message is not None)
        author_value = (f"<@{author['id']}> (`{author['id']}`)"
                        if author.get("id") else "Unknown")
        embed = _base("\u270f\ufe0f Message Edited", 0xfee75c)
        embed.add_field(name="Author", value=author_value, inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Jump",
                        value=f"[Go to message](https://discord.com/channels/"
                              f"{guild.id}/{channel.id}/{payload.message_id})", inline=True)
        embed.add_field(name="Before",
                        value=_content(before_content) if before_content
                        else "*not in cache — unavailable*", inline=False)
        embed.add_field(name="After", value=_content(after_content), inline=False)
        await log_event(guild, "messages", embed)

    # --- Channels ---
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = _base("\U0001f7e2 Channel Created", 0x57f287)
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=_channel_label(channel), inline=True)
        embed.add_field(name="Category", value=_category_name(channel), inline=True)
        await log_event(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = _base("\U0001f534 Channel Deleted", 0xed4245)
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=_channel_label(channel), inline=True)
        embed.add_field(name="Category", value=_category_name(channel), inline=True)
        await log_event(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel,
                                      after: discord.abc.GuildChannel):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if _category_name(before) != _category_name(after):
            changes.append(f"Category: `{_category_name(before)}` \u2192 `{_category_name(after)}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            if hasattr(before, "topic") and before.topic != after.topic:
                changes.append("Topic changed")
        if not changes:
            return  # permission-only edits etc. (too noisy to diff here)
        embed = _base("\U0001f7e1 Channel Updated", 0xfee75c)
        embed.add_field(name="Channel", value=f"#{after.name}", inline=False)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after.guild, "channels", embed)

    # --- Roles ---
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = _base("\U0001f7e2 Role Created", 0x57f287)
        embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(name="Color", value=str(role.color) if role.color else "Default", inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        await log_event(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = _base("\U0001f534 Role Deleted", 0xed4245)
        embed.add_field(name="Role", value=role.name, inline=True)
        embed.add_field(name="Had Members", value=str(len(role.members)), inline=True)
        embed.add_field(name="Color", value=str(role.color) if role.color else "Default", inline=True)
        await log_event(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.color != after.color:
            changes.append(f"Color: `{before.color}` \u2192 `{after.color}`")
        if before.hoist != after.hoist:
            changes.append(f"Hoisted: {before.hoist} \u2192 {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"Mentionable: {before.mentionable} \u2192 {after.mentionable}")
        if not changes:
            return  # permission-only changes (too noisy to fully diff)
        embed = _base("\U0001f7e1 Role Updated", 0xfee75c)
        embed.add_field(name="Role", value=after.mention, inline=False)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after.guild, "roles", embed)

    # --- Punishments (catches manual bans/timeouts done outside the bot) ---
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        embed = _base("\U0001f528 Member Banned", 0xed4245)
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=True)
        await log_event(guild, "mod", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = _base("\U0001f513 Member Unbanned", 0x57f287)
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=True)
        await log_event(guild, "mod", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Timeout changes
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                until = after.timed_out_until.strftime("%Y-%m-%d %H:%M UTC")
                embed = _base("\u23f0 Member Timed Out", 0xed4245)
                embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
                embed.add_field(name="Until", value=until, inline=True)
                await log_event(after.guild, "mod", embed)
            else:
                embed = _base("\U0001f513 Timeout Removed", 0x57f287)
                embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
                await log_event(after.guild, "mod", embed)
        # Nickname changes
        if before.nick != after.nick:
            embed = _base("\U0001f4dd Nickname Changed", 0xfee75c)
            embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
            embed.add_field(name="Before", value=before.nick or "*none*", inline=True)
            embed.add_field(name="After", value=after.nick or "*none*", inline=True)
            await log_event(after.guild, "members", embed)
        # Role add/remove
        before_roles = set(before.roles) - {before.guild.default_role}
        after_roles = set(after.roles) - {after.guild.default_role}
        added = after_roles - before_roles
        removed = before_roles - after_roles
        if added or removed:
            embed = _base("\U0001f3ad Roles Changed", 0xfee75c)
            embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
            if added:
                embed.add_field(name="Added",
                                value=", ".join(r.mention for r in added)[:1024], inline=False)
            if removed:
                embed.add_field(name="Removed",
                                value=", ".join(r.name for r in removed)[:1024], inline=False)
            await log_event(after.guild, "members", embed)

    # --- Invites & server ---
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        embed = _base("\U0001f4e9 Invite Created", 0x57f287)
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        embed.add_field(name="Inviter",
                        value=f"{invite.inviter} (`{invite.inviter.id}`)"
                        if invite.inviter else "Unknown", inline=True)
        await log_event(invite.guild, "server", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild is None:
            return
        embed = _base("\U0001f5d1\ufe0f Invite Deleted", 0xed4245)
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        embed.add_field(name="Channel",
                        value=invite.channel.mention if invite.channel else "Unknown",
                        inline=True)
        await log_event(invite.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.afk_channel != after.afk_channel:
            changes.append("AFK channel changed")
        if before.verification_level != after.verification_level:
            changes.append(f"Verification level: {before.verification_level} "
                           f"\u2192 {after.verification_level}")
        if not changes:
            return
        embed = _base("\U0001f3db\ufe0f Server Updated", 0xfee75c)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after, "server", embed)


async def setup(bot):
    await bot.add_cog(Logging(bot))