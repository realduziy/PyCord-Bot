import datetime

import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import get_recent_cases, log_action, log_case


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


async def _modlog(guild, action, actor, target, reason=None, extra=None):
    """Write a moderation action to the DB (case log) and the configured log channel."""
    target_id = str(getattr(target, "id", target))
    case_id = await log_case(str(guild.id), action, str(actor.id), target_id, reason)

    embed = discord.Embed(title=f"\U0001f6e1\ufe0f {action} \u00b7 Case #{case_id}", color=0xed4245)
    embed.add_field(name="Moderator", value=actor.mention, inline=True)
    target_value = target.mention if hasattr(target, "mention") else str(target)
    embed.add_field(name="Target", value=target_value, inline=True)
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    if extra:
        embed.add_field(name="Details", value=extra, inline=False)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await log_action(guild, embed)
    return case_id


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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

        await interaction.guild.ban(user, reason=reason, delete_message_days=delete_message_days)
        await interaction.response.send_message(f"{user.mention} has been banned. Reason: {reason}", ephemeral=True)
        await _modlog(interaction.guild, "Ban", interaction.user, user, reason,
                      extra=f"Deleted {delete_message_days} day(s) of messages")

    @app_commands.command(name="unban", description="Unbans a user.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user: discord.User):
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"{user.mention} has been unbanned.", ephemeral=True)
        await _modlog(interaction.guild, "Unban", interaction.user, user)

    @app_commands.command(name="clear", description="Clears the specified amount of messages (1-100).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
        await _modlog(interaction.guild, "Clear", interaction.user, interaction.channel,
                      extra=f"{len(deleted)} messages purged in {interaction.channel.mention}")

    @app_commands.command(name="lockdown", description="Locks down a channel.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lockdown(self, interaction: discord.Interaction, channel: discord.TextChannel):
        role = interaction.guild.default_role
        permissions = channel.overwrites_for(role)
        permissions.send_messages = False
        await channel.set_permissions(role, overwrite=permissions)
        await interaction.response.send_message(f"{channel.mention} has been locked down.")
        await _modlog(interaction.guild, "Lockdown", interaction.user, channel)

    @app_commands.command(name="unlock", description="Unlocks a channel.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel):
        role = interaction.guild.default_role
        permissions = channel.overwrites_for(role)
        permissions.send_messages = None
        await channel.set_permissions(role, overwrite=permissions)
        await interaction.response.send_message(f"{channel.mention} has been unlocked.")
        await _modlog(interaction.guild, "Unlock", interaction.user, channel)

    @app_commands.command(name="cases", description="Show the most recent moderation cases.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def cases(self, interaction: discord.Interaction):
        rows = await get_recent_cases(str(interaction.guild.id), 10)
        embed = discord.Embed(title="\U0001f4cb Recent Mod Cases", color=0x5865f2)
        if not rows:
            embed.description = "No moderation cases recorded yet."
        else:
            for row in rows:
                reason = row["reason"] or "No reason provided."
                embed.add_field(
                    name=f"Case #{row['case_id']} \u00b7 {row['action']}",
                    value=f"Moderator: <@{row['actor_id']}>\nTarget: <@{row['target_id']}>\nReason: {reason}",
                    inline=True,
                )
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
