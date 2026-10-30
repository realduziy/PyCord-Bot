import json
import random
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import DB_FILE
from ui_common import base_embed, ok_embed


async def get_counting_state(guild_id: str) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT channel_id, enabled, current_count, last_user_id, high_count, "
            "total_counts, ignored_roles FROM counting_state WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return None
    keys = [
        "channel_id", "enabled", "current_count", "last_user_id", "high_count",
        "total_counts", "ignored_roles",
    ]
    state = dict(zip(keys, row))
    state["ignored_roles"] = json.loads(state["ignored_roles"] or "[]")
    return state


async def save_counting_state(guild_id: str, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [guild_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            f"UPDATE counting_state SET {assignments} WHERE guild_id = ?",
            params,
        )
        await db.commit()


class Games(commands.Cog):
    """Counting channel and future lightweight server games."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    counting = app_commands.Group(
        name="counting",
        description="Manage the server counting channel.",
    )

    @counting.command(name="set", description="Set or move the counting channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counting_set(self, interaction: discord.Interaction,
                           channel: discord.TextChannel):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO counting_state (guild_id, channel_id, enabled, current_count, "
                "last_user_id, high_count, total_counts) VALUES (?, ?, 1, 0, NULL, 0, 0) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, "
                "enabled = 1, current_count = 0, last_user_id = NULL",
                (str(interaction.guild.id), str(channel.id)),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Counting channel set to {channel.mention}. The next number is **1**."),
            ephemeral=True,
        )

    @counting.command(name="disable", description="Disable counting without deleting its stats.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counting_disable(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO counting_state (guild_id, enabled) VALUES (?, 0) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled = 0",
                (str(interaction.guild.id),),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed("Counting has been disabled. Statistics were preserved."),
            ephemeral=True,
        )

    @counting.command(name="reset", description="Reset the current count while preserving records.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counting_reset(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE counting_state SET current_count = 0, last_user_id = NULL "
                "WHERE guild_id = ?",
                (str(interaction.guild.id),),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed("The counting sequence has been reset. The next number is **1**."),
            ephemeral=True,
        )

    @counting.command(name="stats", description="Show counting statistics.")
    async def counting_stats(self, interaction: discord.Interaction):
        state = await get_counting_state(str(interaction.guild.id))
        if state is None:
            await interaction.response.send_message(
                embed=base_embed("🔢 Counting", "neutral", "Counting is not configured yet."),
                ephemeral=True,
            )
            return

        last_member = None
        if state["last_user_id"]:
            last_member = interaction.guild.get_member(int(state["last_user_id"]))

        embed = base_embed("🔢 Counting Stats", "primary")
        embed.add_field(name="Current", value=str(state["current_count"]), inline=True)
        embed.add_field(name="Highest", value=str(state["high_count"]), inline=True)
        embed.add_field(name="Total Counts", value=str(state["total_counts"]), inline=True)
        embed.add_field(
            name="Last Counter",
            value=last_member.mention if last_member else "None",
            inline=True,
        )
        embed.add_field(
            name="Status",
            value="Enabled" if state["enabled"] else "Disabled",
            inline=True,
        )
        channel = interaction.guild.get_channel(int(state["channel_id"])) if state["channel_id"] else None
        embed.add_field(
            name="Channel",
            value=channel.mention if channel else "Not set",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @counting.command(name="ignore", description="Stop a role from participating in counting.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counting_ignore(self, interaction: discord.Interaction, role: discord.Role):
        state = await get_counting_state(str(interaction.guild.id))
        ignored = set(state["ignored_roles"] if state else [])
        ignored.add(str(role.id))
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO counting_state (guild_id, ignored_roles) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET ignored_roles = excluded.ignored_roles",
                (str(interaction.guild.id), json.dumps(sorted(ignored))),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"{role.mention} can no longer participate in counting."),
            ephemeral=True,
        )

    @counting.command(name="unignore", description="Allow a role to participate in counting again.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counting_unignore(self, interaction: discord.Interaction, role: discord.Role):
        state = await get_counting_state(str(interaction.guild.id))
        ignored = set(state["ignored_roles"] if state else [])
        ignored.discard(str(role.id))
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE counting_state SET ignored_roles = ? WHERE guild_id = ?",
                (json.dumps(sorted(ignored)), str(interaction.guild.id)),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"{role.mention} can participate in counting again."),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        state = await get_counting_state(str(message.guild.id))
        if state is None or not state["enabled"] or state["channel_id"] is None:
            return
        if message.channel.id != int(state["channel_id"]):
            return

        member = message.author
        if not isinstance(member, discord.Member):
            return
        ignored_roles = set(state["ignored_roles"])
        if any(str(role.id) in ignored_roles for role in member.roles):
            return

        expected = state["current_count"] + 1
        content = message.content.strip()
        is_plain_number = (
            content == str(expected)
            and not message.attachments
            and not message.stickers
            and not message.embeds
            and state["last_user_id"] != str(member.id)
        )

        if not is_plain_number:
            try:
                await message.delete(delay=0.25)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        current = expected
        high = max(int(state["high_count"]), current)
        total = int(state["total_counts"]) + 1
        await save_counting_state(
            str(message.guild.id),
            current_count=current,
            last_user_id=str(member.id),
            high_count=high,
            total_counts=total,
        )

        try:
            await message.add_reaction("✅")
        except (discord.Forbidden, discord.HTTPException):
            pass

        if current % 100 == 0:
            try:
                await message.channel.send(
                    f"🎉 {member.mention} reached **{current:,}**! The next number is **{current + 1:,}**."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
