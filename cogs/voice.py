import datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import DB_FILE, log_event

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


async def _add_temp(guild_id: str, channel_id: str, owner_id: str, hub_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO voice_temps (channel_id, guild_id, owner_id, hub_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, guild_id, owner_id, hub_id,
             datetime.datetime.now(datetime.timezone.utc).isoformat()))
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


class Voice(commands.Cog):
    """Temporary voice channels: join a hub, get your own channel, gone when empty."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Clean up temp channels that went stale while the bot was offline."""
        for guild in self.bot.guilds:
            for temp in await _all_temps(str(guild.id)):
                channel = guild.get_channel(int(temp["channel_id"]))
                if channel is None:
                    await _remove_temp(temp["channel_id"])
                elif not channel.members:
                    try:
                        await channel.delete(reason="Cleaning up empty temp voice channel")
                    except (discord.Forbidden, discord.HTTPException):
                        continue
                    await _remove_temp(temp["channel_id"])

    # --- Core logic ---
    async def _spawn_temp(self, member: discord.Member, hub: discord.VoiceChannel,
                          template: str):
        """Create a temp channel for a member next to the hub and move them in."""
        name = _render_name(template, member)
        try:
            temp = await member.guild.create_voice_channel(
                name, category=hub.category, overwrites=_owner_overwrite(member),
                reason=f"Temp channel for {member} (joined hub)")
        except (discord.Forbidden, discord.HTTPException):
            return None

        await _add_temp(str(member.guild.id), str(temp.id), str(member.id), str(hub.id))
        try:
            await member.move_to(temp, reason="Moved to temp voice channel")
        except (discord.Forbidden, discord.HTTPException):
            # Couldn't move them — remove the channel we just made
            try:
                await temp.delete(reason="Member could not be moved")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await _remove_temp(str(temp.id))
            return None

        embed = discord.Embed(title="\U0001f50a Temp Voice Channel Created", color=0x57f287)
        embed.add_field(name="Channel", value=temp.name, inline=True)
        embed.add_field(name="Owner", value=f"{member} (`{member.id}`)", inline=True)
        embed.add_field(name="Hub", value=hub.name, inline=True)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await log_event(member.guild, "voice", embed)
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
        channel="An existing voice channel (mention or ID) to turn into a hub — leave empty to create a new one",
        name="Name for the new hub channel (only used when creating one)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def voice_hub_create(self, interaction: discord.Interaction,
                               channel: str = None, name: str = None):
        guild_id = str(interaction.guild.id)

        if channel:
            # Turn an existing channel into a hub
            raw_id = None
            if channel.strip().isdigit():
                raw_id = int(channel.strip())
            else:
                import re
                match = re.match(r"^<#(\d+)>$", channel.strip())
                if match:
                    raw_id = int(match.group(1))
            existing = interaction.guild.get_channel(raw_id) if raw_id else None
            if existing is None:
                await interaction.response.send_message(
                    "Couldn't find that channel — mention it or paste its ID.", ephemeral=True)
                return
            if not isinstance(existing, discord.VoiceChannel):
                await interaction.response.send_message(
                    f"{existing.mention} is not a voice channel.", ephemeral=True)
                return
            row = await _get_hub(str(existing.id))
            if row:
                await interaction.response.send_message(
                    f"{existing.mention} is already a hub.", ephemeral=True)
                return
            await _add_hub(guild_id, str(existing.id), DEFAULT_TEMPLATE)
            await interaction.response.send_message(
                f"\u2705 {existing.mention} is now a hub — anyone who joins gets their own "
                f"temp channel (named like `John's Lounge`).", ephemeral=True)
            return

        # No channel given: create a new hub channel
        await interaction.response.defer(ephemeral=True)
        new_channel = await interaction.guild.create_voice_channel(
            name or DEFAULT_HUB_NAME, category=interaction.channel.category,
            reason=f"Voice hub created by {interaction.user}")
        await _add_hub(guild_id, str(new_channel.id), DEFAULT_TEMPLATE)
        await interaction.followup.send(
            f"\u2705 Hub created: {new_channel.mention}\n"
            "Rename it anytime like a normal channel. Members who join get their own "
            f"temp channel (named like `John's Lounge`).", ephemeral=True)

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
                await self._delete_temp(channel)
                deleted += 1
        await interaction.followup.send(
            f"\U0001f5d1\ufe0f Cleaned up {deleted} empty temp channel(s).",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Voice(bot))