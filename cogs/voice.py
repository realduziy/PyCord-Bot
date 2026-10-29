import asyncio
import datetime
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View

from cog_utils import DB_FILE, log_event
from ui_common import base_embed, parse_snowflake

DEFAULT_TEMPLATE = "{user}'s Lounge"
DEFAULT_HUB_NAME = "\u2795 Join to Create"
MAX_CHANNEL_NAME = 32  # Discord voice channel name limit


def _render_name(template: str, member: discord.Member) -> str:
    name = (template or DEFAULT_TEMPLATE).replace("{user}", member.display_name)
    return name[:MAX_CHANNEL_NAME]


def _owner_overwrite(member: discord.Member) -> dict:
    """The temp channel owner gets channel-management permissions."""
    allow = discord.PermissionOverwrite(
        manage_channels=True,
        manage_permissions=True,
        move_members=True,
        mute_members=True,
        deafen_members=True,
    )
    return {member: allow}


# --- DB helpers ---
async def _get_hub(channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM voice_hubs WHERE channel_id = ?", (channel_id,)) as cur:
            return await cur.fetchone()


async def _add_hub(guild_id: str, channel_id: str, template: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO voice_hubs (guild_id, channel_id, name_template) "
            "VALUES (?, ?, ?)", (guild_id, channel_id, template))
        await db.commit()


async def _remove_hub(guild_id: str, channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM voice_hubs WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id))
        await db.commit()


async def _list_hubs(guild_id: str) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM voice_hubs WHERE guild_id = ?", (guild_id,)) as cur:
            return await cur.fetchall()


async def _add_temp(guild_id: str, channel_id: str, owner_id: str, hub_id: str,
                    panel_message_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO voice_temps "
            "(channel_id, guild_id, owner_id, hub_id, created_at, panel_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, guild_id, owner_id, hub_id,
             datetime.datetime.now(datetime.timezone.utc).isoformat(), panel_message_id))
        await db.commit()


async def _get_temp(channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM voice_temps WHERE channel_id = ?", (channel_id,)) as cur:
            return await cur.fetchone()


async def _remove_temp(channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM voice_temps WHERE channel_id = ?", (channel_id,))
        await db.commit()


async def _set_temp_owner(channel_id: str, owner_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE voice_temps SET owner_id = ? WHERE channel_id = ?",
            (owner_id, channel_id))
        await db.commit()


async def _all_temps(guild_id: str) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM voice_temps WHERE guild_id = ?", (guild_id,)) as cur:
            return await cur.fetchall()


# --- Control panel ---
def _panel_embed(vc: discord.VoiceChannel, owner_id: str) -> discord.Embed:
    everyone = vc.overwrites_for(vc.guild.default_role)
    locked = everyone.connect is False
    embed = base_embed(f"\U0001f50a {vc.name}", "success")
    embed.set_footer(text="Only the channel owner (or staff) can use these controls.")
    embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
    embed.add_field(name="Status", value="\U0001f512 Locked" if locked else "\U0001f513 Open",
                    inline=True)
    embed.add_field(name="User Limit",
                    value=str(vc.user_limit) if vc.user_limit else "No limit", inline=True)
    embed.add_field(name="Members", value=len(vc.members), inline=True)
    return embed


class RenameModal(Modal):
    name = TextInput(label="New channel name", max_length=MAX_CHANNEL_NAME, required=True)

    def __init__(self, cog):
        super().__init__(title="Rename Channel", timeout=120)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        vc, temp = await self.cog._panel_context(interaction, respond=True)
        if vc is None:
            return
        await vc.edit(name=self.name.value.strip(), reason=f"Renamed by {interaction.user}")
        await interaction.response.send_message(
            f"\u2705 Channel renamed to **{vc.name}**.", ephemeral=True)
        await self.cog._refresh_panel(vc, temp)


class LimitModal(Modal):
    limit = TextInput(label="User limit (0 = no limit)", placeholder="e.g. 5",
                      max_length=2, required=True, default="0")

    def __init__(self, cog):
        super().__init__(title="Set User Limit", timeout=120)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        vc, temp = await self.cog._panel_context(interaction, respond=True)
        if vc is None:
            return
        value = self.limit.value.strip()
        if not value.isdigit() or not 0 <= int(value) <= 99:
            await interaction.response.send_message(
                "Enter a number between 0 and 99.", ephemeral=True)
            return
        await vc.edit(user_limit=int(value), reason=f"Limit set by {interaction.user}")
        await interaction.response.send_message(
            f"\u2705 User limit set to {value if int(value) else 'no limit'}.",
            ephemeral=True)
        await self.cog._refresh_panel(vc, temp)


class InviteModal(Modal):
    user = TextInput(label="User to invite (mention or ID)",
                     placeholder="@someone or 123456789", required=True)

    def __init__(self, cog):
        super().__init__(title="Invite User", timeout=120)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        vc, temp = await self.cog._panel_context(interaction, respond=True)
        if vc is None:
            return
        raw_id = parse_snowflake(self.user.value)
        target = interaction.guild.get_member(raw_id) if raw_id else None
        if target is None:
            await interaction.response.send_message(
                "Couldn't find that user — use a mention or their ID.", ephemeral=True)
            return
        try:
            await target.send(
                f"\U0001f4e2 You've been invited to **{vc.name}** in "
                f"**{interaction.guild.name}**! Join the voice channel when you're ready.")
            sent = " They've been DMed."
        except (discord.Forbidden, discord.HTTPException):
            sent = " (Couldn't DM them — tell them to join manually!)"
        await interaction.response.send_message(
            f"\u2705 Invited {target.mention} to the channel.{sent}", ephemeral=True)


class KickModal(Modal):
    user = TextInput(label="User to kick (mention or ID)",
                     placeholder="@someone or 123456789", required=True)

    def __init__(self, cog):
        super().__init__(title="Kick from Channel", timeout=120)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        vc, temp = await self.cog._panel_context(interaction, respond=True)
        if vc is None:
            return
        raw_id = parse_snowflake(self.user.value)
        target = interaction.guild.get_member(raw_id) if raw_id else None
        if target is None or target not in vc.members:
            await interaction.response.send_message(
                "That user isn't in the voice channel.", ephemeral=True)
            return
        if target.id == int(temp["owner_id"]):
            await interaction.response.send_message(
                "You can't kick the channel owner.", ephemeral=True)
            return
        await target.move_to(None, reason=f"Kicked by {interaction.user}")
        await interaction.response.send_message(
            f"\U0001f6ab Kicked {target.mention} from the channel.", ephemeral=True)
        await self.cog._refresh_panel(vc, temp)


class ChannelControlsView(View):
    """Persistent control panel posted in each temp channel's built-in chat."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Lock", style=discord.ButtonStyle.danger,
                       emoji="\U0001f512", custom_id="voice_lock_btn")
    async def lock(self, interaction: discord.Interaction, button: Button):
        vc, temp = await self.cog._panel_context(interaction, respond=True)
        if vc is None:
            return
        everyone = vc.overwrites_for(interaction.guild.default_role)
        everyone.connect = False
        await vc.set_permissions(interaction.guild.default_role, overwrite=everyone,
                                 reason=f"Locked by {interaction.user}")
        await interaction.response.send_message("\U0001f512 Channel locked.", ephemeral=True)
        await self.cog._refresh_panel(vc, temp)

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.success,
                       emoji="\U0001f513", custom_id="voice_unlock_btn")
    async def unlock(self, interaction: discord.Interaction, button: Button):
        vc, temp = await self.cog._panel_context(interaction, respond=True)
        if vc is None:
            return
        everyone = vc.overwrites_for(interaction.guild.default_role)
        everyone.connect = None
        await vc.set_permissions(interaction.guild.default_role, overwrite=everyone,
                                 reason=f"Unlocked by {interaction.user}")
        await interaction.response.send_message("\U0001f513 Channel unlocked.", ephemeral=True)
        await self.cog._refresh_panel(vc, temp)

    @discord.ui.button(label="Limit", style=discord.ButtonStyle.primary,
                       emoji="\U0001f465", custom_id="voice_limit_btn")
    async def limit(self, interaction: discord.Interaction, button: Button):
        if (await self.cog._panel_context(interaction, respond=True)) == (None, None):
            return
        await interaction.response.send_modal(LimitModal(self.cog))

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.primary,
                       emoji="\u270f\ufe0f", custom_id="voice_rename_btn")
    async def rename(self, interaction: discord.Interaction, button: Button):
        if (await self.cog._panel_context(interaction, respond=True)) == (None, None):
            return
        await interaction.response.send_modal(RenameModal(self.cog))

    @discord.ui.button(label="Invite", style=discord.ButtonStyle.success,
                       emoji="\U0001f4e9", custom_id="voice_invite_btn")
    async def invite(self, interaction: discord.Interaction, button: Button):
        if (await self.cog._panel_context(interaction, respond=True)) == (None, None):
            return
        await interaction.response.send_modal(InviteModal(self.cog))

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger,
                       emoji="\U0001f6ab", custom_id="voice_kick_btn")
    async def kick(self, interaction: discord.Interaction, button: Button):
        if (await self.cog._panel_context(interaction, respond=True)) == (None, None):
            return
        await interaction.response.send_modal(KickModal(self.cog))


class Voice(commands.Cog):
    """Temporary voice channels with VoiceMaster-style control panels."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Clean up temp channels that went stale while the bot was offline.

        Existence is confirmed via the API (never the cache alone), and a
        live channel is only deleted when the member cache is complete
        (guild.chunked) — a cache miss must never wipe a busy channel.
        """
        for guild in self.bot.guilds:
            for temp in await _all_temps(str(guild.id)):
                channel_id = int(temp["channel_id"])
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.NotFound:
                    await _remove_temp(temp["channel_id"])  # confirmed gone
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    continue  # can't verify right now — leave it alone
                if not channel.members and getattr(guild, "chunked", False):
                    try:
                        await channel.delete(reason="Cleaning up empty temp voice channel")
                    except (discord.Forbidden, discord.HTTPException):
                        continue
                    await _remove_temp(temp["channel_id"])

    # --- Panel helpers ---
    async def _panel_context(self, interaction: discord.Interaction,
                             respond: bool = False):
        """Resolve the voice channel + temp row for a panel interaction.

        The panel lives in the voice channel's built-in chat, so the channel
        is simply interaction.channel.
        """
        guild = interaction.guild
        temp = await _get_temp(str(interaction.channel.id))
        if temp is None or temp["guild_id"] != str(guild.id):
            if respond:
                await interaction.response.send_message(
                    "This control panel isn't attached to an active temp channel.",
                    ephemeral=True)
            return None, None
        vc = guild.get_channel(int(temp["channel_id"]))
        if vc is None:
            await _remove_temp(temp["channel_id"])
            if respond:
                await interaction.response.send_message(
                    "The voice channel no longer exists.", ephemeral=True)
            return None, None
        # Only the owner (or staff with manage_channels) may use the controls
        is_owner = str(interaction.user.id) == temp["owner_id"]
        if not is_owner and not interaction.user.guild_permissions.manage_channels:
            if respond:
                await interaction.response.send_message(
                    "Only the channel owner can use these controls.", ephemeral=True)
            return None, None
        return vc, temp

    async def _refresh_panel(self, vc: discord.VoiceChannel, temp):
        """Update the pinned control embed after an action."""
        if not temp or not temp["panel_message_id"]:
            return
        try:
            message = await vc.fetch_message(int(temp["panel_message_id"]))
            await message.edit(embed=_panel_embed(vc, temp["owner_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # --- Core logic ---
    async def _spawn_temp(self, member: discord.Member, hub: discord.VoiceChannel,
                          template: str):
        """Create a temp channel and post its control panel in the built-in chat."""
        guild = member.guild
        guild_id = str(guild.id)
        name = _render_name(template, member)

        overwrites = _owner_overwrite(member)
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=True)
        try:
            temp = await guild.create_voice_channel(
                name, category=hub.category, overwrites=overwrites,
                reason=f"Temp channel for {member} (joined hub)")
        except (discord.Forbidden, discord.HTTPException):
            return None

        panel_message_id = ""
        try:
            panel = await temp.send(embed=_panel_embed(temp, str(member.id)),
                                    view=ChannelControlsView(self))
            await panel.pin(reason="Channel control panel")
            panel_message_id = str(panel.id)
        except (discord.Forbidden, discord.HTTPException):
            pass  # voice channel still works without a panel

        await _add_temp(guild_id, str(temp.id), str(member.id), str(hub.id),
                        panel_message_id)

        try:
            await member.move_to(temp, reason="Moved to temp voice channel")
        except (discord.Forbidden, discord.HTTPException):
            try:
                await temp.delete(reason="Member could not be moved")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await _remove_temp(str(temp.id))
            return None

        embed = base_embed("\U0001f50a Temp Voice Channel Created", "success")
        embed.add_field(name="Channel", value=temp.name, inline=True)
        embed.add_field(name="Owner", value=f"{member} (`{member.id}`)", inline=True)
        embed.add_field(name="Hub", value=hub.name, inline=True)
        await log_event(guild, "voice_temp", embed)
        return temp

    async def _delete_temp(self, channel: discord.VoiceChannel):
        try:
            await channel.delete(reason="Temp voice channel is empty")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await _remove_temp(str(channel.id))

    async def _transfer_or_delete(self, channel: discord.VoiceChannel, temp):
        """Owner left: hand ownership to the next member, or delete if empty."""
        if not channel.members:
            await self._delete_temp(channel)
            return
        if temp["owner_id"] != "0" and temp["owner_id"] in {str(m.id) for m in channel.members}:
            return  # owner still inside
        new_owner = channel.members[0]
        await _set_temp_owner(str(channel.id), str(new_owner.id))
        try:
            allow = discord.PermissionOverwrite(
                manage_channels=True, manage_permissions=True,
                move_members=True, mute_members=True, deafen_members=True)
            await channel.set_permissions(new_owner, overwrite=allow,
                                          reason="Temp channel ownership transferred")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await self._refresh_panel(channel, await _get_temp(str(channel.id)))

    # --- Events ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        # Joined a hub? Spawn a temp channel.
        if after.channel and after.channel != before.channel:
            hub_row = await _get_hub(str(after.channel.id))
            if hub_row:
                await self._spawn_temp(member, after.channel, hub_row["name_template"])

        # Left a temp channel? Transfer ownership or delete it.
        if before.channel and before.channel != after.channel:
            temp = await _get_temp(str(before.channel.id))
            if temp:
                channel = member.guild.get_channel(before.channel.id)
                if channel is None:
                    await _remove_temp(str(before.channel.id))
                else:
                    await self._transfer_or_delete(channel, temp)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if isinstance(channel, discord.VoiceChannel):
            await _remove_hub(str(channel.guild.id), str(channel.id))
            await _remove_temp(str(channel.id))

    # --- Commands ---
    @app_commands.command(name="voicehub", description="Create a voice hub, or turn an existing channel into one.")
    @app_commands.describe(
        channel="An existing voice channel to turn into a hub — pick one, or leave empty to create a new one",
        name="Name for the new hub channel (only used when creating one)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def voice_hub_create(self, interaction: discord.Interaction,
                               channel: Optional[discord.VoiceChannel] = None,
                               name: Optional[str] = None):
        guild_id = str(interaction.guild.id)

        if channel is not None:
            row = await _get_hub(str(channel.id))
            if row:
                await interaction.response.send_message(
                    f"\u2705 {channel.mention} is already a hub.", ephemeral=True)
                return
            await _add_hub(guild_id, str(channel.id), DEFAULT_TEMPLATE)
            await interaction.response.send_message(
                f"\u2705 {channel.mention} is now a hub — anyone who joins gets their own "
                f"temp channel with a control panel in its chat.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            new_channel = await interaction.guild.create_voice_channel(
                name or DEFAULT_HUB_NAME,
                category=interaction.channel.category,
                reason=f"Voice hub created by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't create the voice hub. Check that my role can create "
                "channels in that category.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "Discord couldn't create the voice hub. Try a different name or category.",
                ephemeral=True,
            )
            return

        await _add_hub(guild_id, str(new_channel.id), DEFAULT_TEMPLATE)
        await interaction.followup.send(
            f"\u2705 Hub created: {new_channel.mention}\n"
            "Rename it anytime like a normal channel. Members who join get their own "
            "temp channel with a control panel in its chat.", ephemeral=True)

    @app_commands.command(name="voicehubremove", description="Remove a voice hub (the channel itself stays).")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def voice_hub_remove(self, interaction: discord.Interaction,
                               channel: discord.VoiceChannel):
        row = await _get_hub(str(channel.id))
        if not row or row["guild_id"] != str(interaction.guild.id):
            await interaction.response.send_message(
                f"{channel.mention} isn't a voice hub.", ephemeral=True)
            return
        await _remove_hub(str(interaction.guild.id), str(channel.id))
        await interaction.response.send_message(
            f"\u2705 {channel.mention} is no longer a hub. It's a normal channel now.",
            ephemeral=True)

    @app_commands.command(name="voicehubs", description="List all voice hubs in this server.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def voice_hub_list(self, interaction: discord.Interaction):
        rows = await _list_hubs(str(interaction.guild.id))
        embed = discord.Embed(title="\U0001f50a Voice Hubs", color=0x5865f2)
        if not rows:
            embed.description = ("No hubs yet. Create one with `/voicehub` — anyone who "
                                 "joins it gets their own temporary channel.")
        else:
            for row in rows:
                channel = interaction.guild.get_channel(int(row["channel_id"]))
                status = channel.mention if channel else f"`{row['channel_id']}` (deleted)"
                template = (row["name_template"] or DEFAULT_TEMPLATE).replace("{user}", "User")
                embed.add_field(name=status,
                                value=f"Template: `{template}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="voicecleanup", description="Delete all empty temp voice channels right now.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def voice_cleanup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        deleted = 0
        for temp in await _all_temps(str(interaction.guild.id)):
            channel = interaction.guild.get_channel(int(temp["channel_id"]))
            if channel is None:
                await _remove_temp(temp["channel_id"])
            elif not channel.members:
                if deleted:
                    # Channel deletion is a route-limited endpoint. Pacing bulk
                    # cleanup avoids bursts of Discord 429 responses.
                    await asyncio.sleep(1.05)
                await self._delete_temp(channel)
                deleted += 1
        await interaction.followup.send(
            f"\U0001f5d1\ufe0f Cleaned up {deleted} empty temp channel(s).",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Voice(bot))
