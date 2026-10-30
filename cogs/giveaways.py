import asyncio
import datetime
import json
import random
import time
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cog_utils import DB_FILE, log_event
from ui_common import base_embed, ok_embed, truncate

from cogs.channel_tools import format_duration, parse_duration


MAX_GIVEAWAY_DURATION = datetime.timedelta(days=30)
GIVEAWAY_CHECK_INTERVAL = 30


async def fetch_giveaway(giveaway_id: int) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ?",
            (giveaway_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return None
    keys = [
        "giveaway_id", "guild_id", "channel_id", "message_id", "host_id",
        "prize", "winners", "ends_at", "required_role_id", "status",
        "winner_ids", "created_at",
    ]
    data = dict(zip(keys, row))
    data["winner_ids"] = json.loads(data["winner_ids"] or "[]")
    return data


async def fetch_entry_count(giveaway_id: int) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway_id,),
        ) as cursor:
            (count,) = await cursor.fetchone()
    return int(count)


async def giveaway_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT giveaway_id, prize, status FROM giveaways WHERE guild_id = ? "
            "AND (CAST(giveaway_id AS TEXT) LIKE ? OR lower(prize) LIKE lower(?)) "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, ends_at LIMIT 25",
            (str(interaction.guild.id), f"%{current.strip()}%", f"%{current.strip()}%"),
        ) as cursor:
            rows = await cursor.fetchall()

    choices = []
    for giveaway_id, prize, status in rows:
        label = f"#{giveaway_id} {prize}"[:100]
        choices.append(app_commands.Choice(name=label, value=str(giveaway_id)))
    return choices


async def build_giveaway_embed(giveaway: dict) -> discord.Embed:
    entries = await fetch_entry_count(giveaway["giveaway_id"])
    status = giveaway["status"].title()
    color = "primary" if giveaway["status"] == "active" else "neutral"
    embed = base_embed(f"🎁 Giveaway #{giveaway['giveaway_id']}", color)
    embed.description = truncate(giveaway["prize"], 1500)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Winners", value=str(giveaway["winners"]), inline=True)
    embed.add_field(name="Entries", value=str(entries), inline=True)
    embed.add_field(name="Hosted by", value=f"<@{giveaway['host_id']}>", inline=True)
    embed.add_field(
        name="Ends",
        value=f"<t:{int(giveaway['ends_at'])}:R>",
        inline=True,
    )

    if giveaway["required_role_id"]:
        embed.add_field(
            name="Required Role",
            value=f"<@&{giveaway['required_role_id']}>",
            inline=True,
        )

    if giveaway["status"] == "ended" and giveaway["winner_ids"]:
        mentions = " ".join(f"<@{user_id}>" for user_id in giveaway["winner_ids"])
        embed.add_field(name="Winners", value=truncate(mentions, 1024), inline=False)

    embed.set_footer(text="Use the button below to enter!")
    return embed


class GiveawayEntryButton(discord.ui.Button):
    def __init__(self, giveaway_id: int, *, disabled: bool = False):
        super().__init__(
            label="Enter Giveaway",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"giveaway_entry:{giveaway_id}",
            disabled=disabled,
        )
        self.giveaway_id = giveaway_id

    async def callback(self, interaction: discord.Interaction):
        giveaway = await fetch_giveaway(self.giveaway_id)
        if giveaway is None or giveaway["status"] != "active":
            await interaction.response.send_message("That giveaway is no longer active.", ephemeral=True)
            return
        if interaction.guild.id != int(giveaway["guild_id"]):
            await interaction.response.send_message("That giveaway belongs to another server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("I couldn't find your member data.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Bots cannot enter giveaways.", ephemeral=True)
            return

        required_role_id = giveaway["required_role_id"]
        if required_role_id:
            required_role = interaction.guild.get_role(int(required_role_id))
            if required_role is None:
                await interaction.response.send_message(
                    "The required role no longer exists, so entries are paused. Ask staff to end the giveaway.",
                    ephemeral=True,
                )
                return
            if required_role not in member.roles:
                await interaction.response.send_message(
                    f"You need {required_role.mention} to enter this giveaway.",
                    ephemeral=True,
                )
                return

        async with aiosqlite.connect(DB_FILE) as db:
            try:
                await db.execute(
                    "INSERT INTO giveaway_entries (giveaway_id, user_id, joined_at) VALUES (?, ?, ?)",
                    (self.giveaway_id, str(member.id), str(discord.utils.utcnow())),
                )
                entered = True
            except aiosqlite.IntegrityError:
                entered = False
            await db.commit()

        if interaction.message is not None:
            try:
                await interaction.message.edit(
                    embed=await build_giveaway_embed(giveaway)
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message(
            "You entered the giveaway!" if entered else "You are already entered in this giveaway.",
            ephemeral=True,
        )


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int, *, active: bool = True):
        super().__init__(timeout=None)
        self.add_item(GiveawayEntryButton(giveaway_id, disabled=not active))


async def get_persistent_giveaway_views() -> list[GiveawayView]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT giveaway_id, status FROM giveaways"
        ) as cursor:
            rows = await cursor.fetchall()
    return [GiveawayView(giveaway_id, active=status == "active") for giveaway_id, status in rows]


async def choose_winners(guild: discord.Guild, giveaway: dict,
                         exclude_ids: Optional[set[int]] = None) -> list[discord.Member]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway["giveaway_id"],),
        ) as cursor:
            entry_ids = [int(row[0]) for row in await cursor.fetchall()]

    required_role = None
    if giveaway["required_role_id"]:
        required_role = guild.get_role(int(giveaway["required_role_id"]))

    eligible = []
    for user_id in entry_ids:
        if exclude_ids and user_id in exclude_ids:
            continue
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        if required_role is not None and required_role not in member.roles:
            continue
        eligible.append(member)

    if not eligible:
        return []
    return random.sample(eligible, min(int(giveaway["winners"]), len(eligible)))


async def end_giveaway(bot: commands.Bot, giveaway_id: int,
                       actor: discord.abc.User | None = None,
                       reason: str | None = None) -> bool:
    giveaway = await fetch_giveaway(giveaway_id)
    if giveaway is None or giveaway["status"] != "active":
        return False

    guild = bot.get_guild(int(giveaway["guild_id"]))
    if guild is None:
        return False

    winners = await choose_winners(guild, giveaway)
    winner_ids = [member.id for member in winners]

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE giveaways SET status = 'ended', winner_ids = ? WHERE giveaway_id = ?",
            (json.dumps(winner_ids), giveaway_id),
        )
        await db.commit()

    channel = guild.get_channel(int(giveaway["channel_id"]))
    if isinstance(channel, discord.TextChannel) and giveaway["message_id"]:
        try:
            message = await channel.fetch_message(int(giveaway["message_id"]))
            await message.edit(
                embed=await build_giveaway_embed(await fetch_giveaway(giveaway_id)),
                view=GiveawayView(giveaway_id, active=False),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    if isinstance(channel, discord.TextChannel):
        if winners:
            winner_text = " ".join(member.mention for member in winners)
            content = (
                f"🎉 Congratulations {winner_text}! You won **{giveaway['prize']}**!"
            )
        else:
            content = (
                f"Giveaway #{giveaway_id} ended, but there were no valid entries."
            )

        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await log_event(
        guild,
        "giveaway_end",
        actor,
        channel,
        reason=reason or "Giveaway ended",
        extra=(
            f"Prize: {truncate(giveaway['prize'], 300)}\n"
            f"Winners: {', '.join(f'<@{user_id}>' for user_id in winner_ids) or 'None'}"
        ),
    )
    return True


async def end_expired_giveaways(bot: commands.Bot):
    now = time.time()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT giveaway_id FROM giveaways WHERE status = 'active' AND ends_at <= ?",
            (now,),
        ) as cursor:
            expired = [row[0] for row in await cursor.fetchall()]

    for giveaway_id in expired:
        await end_giveaway(bot, giveaway_id, reason="Giveaway duration expired")


class Giveaways(commands.Cog):
    """Persistent giveaway system."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.giveaway_task.start()

    async def cog_unload(self):
        self.giveaway_task.cancel()
        task = self.giveaway_task.get_task()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    @tasks.loop(seconds=GIVEAWAY_CHECK_INTERVAL)
    async def giveaway_task(self):
        await end_expired_giveaways(self.bot)

    @giveaway_task.before_loop
    async def before_giveaway_task(self):
        await self.bot.wait_until_ready()

    giveaway = app_commands.Group(
        name="giveaway",
        description="Create and manage giveaways.",
    )

    @giveaway.command(name="create", description="Create a new giveaway.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(send_messages=True, embed_links=True)
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: app_commands.Range[str, 1, 200],
        duration: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: Optional[discord.TextChannel] = None,
        required_role: Optional[discord.Role] = None,
    ):
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Giveaways can only be posted in text channels.",
                ephemeral=True,
            )
            return

        try:
            delta = parse_duration(duration, MAX_GIVEAWAY_DURATION)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, "
                "ends_at, required_role_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    str(interaction.guild.id),
                    str(target_channel.id),
                    str(interaction.user.id),
                    prize.strip(),
                    winners,
                    time.time() + delta.total_seconds(),
                    str(required_role.id) if required_role else None,
                    str(discord.utils.utcnow()),
                ),
            )
            await db.commit()
            giveaway_id = cursor.lastrowid

        giveaway = await fetch_giveaway(giveaway_id)
        view = GiveawayView(giveaway_id, active=True)
        self.bot.add_view(view)
        try:
            message = await target_channel.send(
                f"🎁 New giveaway from {interaction.user.mention}!",
                embed=await build_giveaway_embed(giveaway),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    "DELETE FROM giveaways WHERE giveaway_id = ?",
                    (giveaway_id,),
                )
                await db.commit()
            await interaction.followup.send(
                "I couldn't post the giveaway to that channel.",
                ephemeral=True,
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE giveaways SET message_id = ? WHERE giveaway_id = ?",
                (str(message.id), giveaway_id),
            )
            await db.commit()

        await interaction.followup.send(
            embed=ok_embed(
                f"Giveaway #{giveaway_id} created in {target_channel.mention} "
                f"for {format_duration(delta)}."
            ),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "giveaway_start",
            interaction.user,
            target_channel,
            reason=prize.strip(),
            extra=(
                f"Winners: {winners}\nDuration: {format_duration(delta)}\n"
                f"Required role: {required_role.mention if required_role else 'None'}"
            ),
        )

    @giveaway.command(name="end", description="End an active giveaway early.")
    @app_commands.autocomplete(giveaway=giveaway_autocomplete)
    async def giveaway_end(self, interaction: discord.Interaction, giveaway: str,
                           reason: Optional[str] = None):
        data = await fetch_giveaway(int(giveaway))
        if data is None or data["guild_id"] != str(interaction.guild.id):
            await interaction.response.send_message("Giveaway not found.", ephemeral=True)
            return

        host_id = int(data["host_id"])
        if interaction.user.id != host_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only the host or someone with Manage Server can end this giveaway.",
                ephemeral=True,
            )
            return

        ended = await end_giveaway(self.bot, data["giveaway_id"], interaction.user, reason)
        if not ended:
            await interaction.response.send_message("That giveaway is already ended.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=ok_embed(f"Giveaway #{data['giveaway_id']} ended."),
            ephemeral=True,
        )

    @giveaway.command(name="reroll", description="Reroll the winners of an ended giveaway.")
    @app_commands.autocomplete(giveaway=giveaway_autocomplete)
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway: str):
        data = await fetch_giveaway(int(giveaway))
        if data is None or data["guild_id"] != str(interaction.guild.id):
            await interaction.response.send_message("Giveaway not found.", ephemeral=True)
            return
        if data["status"] != "ended":
            await interaction.response.send_message("Rerolls are only available after a giveaway ends.", ephemeral=True)
            return

        host_id = int(data["host_id"])
        if interaction.user.id != host_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Only the host or someone with Manage Server can reroll this giveaway.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        previous = set(data["winner_ids"])
        winners = await choose_winners(guild, data, exclude_ids=previous)
        if not winners:
            await interaction.response.send_message(
                "There are no additional eligible entries to reroll.",
                ephemeral=True,
            )
            return

        winner_ids = [member.id for member in winners]
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE giveaways SET winner_ids = ? WHERE giveaway_id = ?",
                (json.dumps(winner_ids), data["giveaway_id"]),
            )
            await db.commit()

        channel = guild.get_channel(int(data["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            mentions = " ".join(member.mention for member in winners)
            try:
                await channel.send(
                    f"🎁 Reroll for giveaway #{data['giveaway_id']}: {mentions} won **{data['prize']}**!"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message(
            embed=ok_embed(f"Rerolled giveaway #{data['giveaway_id']}."),
            ephemeral=True,
        )
        await log_event(
            guild,
            "giveaway_update",
            interaction.user,
            channel,
            reason=f"Rerolled giveaway #{data['giveaway_id']}",
            extra="New winners: " + ", ".join(f"<@{user_id}>" for user_id in winner_ids),
        )

    @giveaway.command(name="list", description="List recent giveaways.")
    async def giveaway_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT giveaway_id, prize, status, ends_at FROM giveaways WHERE guild_id = ? "
                "ORDER BY giveaway_id DESC LIMIT 10",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🎁 Giveaways", "neutral", "No giveaways have been created."),
                ephemeral=True,
            )
            return

        embed = base_embed("🎁 Recent Giveaways", "primary")
        for giveaway_id, prize, status, ends_at in rows:
            value = f"Prize: {truncate(prize, 80)}\nStatus: {status.title()}"
            if status == "active":
                value += f"\nEnds: <t:{int(ends_at)}:R>"
            embed.add_field(name=f"#{giveaway_id}", value=value, inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @giveaway.command(name="delete", description="Delete a giveaway and remove its entry records.")
    @app_commands.autocomplete(giveaway=giveaway_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_delete(self, interaction: discord.Interaction, giveaway: str):
        data = await fetch_giveaway(int(giveaway))
        if data is None or data["guild_id"] != str(interaction.guild.id):
            await interaction.response.send_message("Giveaway not found.", ephemeral=True)
            return

        if data["channel_id"] and data["message_id"]:
            channel = interaction.guild.get_channel(int(data["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(data["message_id"]))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id = ?",
                (data["giveaway_id"],),
            )
            await db.execute(
                "DELETE FROM giveaways WHERE giveaway_id = ?",
                (data["giveaway_id"],),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Giveaway #{data['giveaway_id']} was deleted."),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "giveaway_update",
            interaction.user,
            None,
            reason=f"Deleted giveaway #{data['giveaway_id']}",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))
