import json
import random
import time
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import DB_FILE, log_event
from ui_common import base_embed, ok_embed, truncate


MAX_LEVEL = 10000


def xp_for_next_level(level: int) -> int:
    return int(100 * (level ** 1.5))


def level_for_xp(xp: int) -> tuple[int, int, int]:
    """Return (current level, progress into the level, XP required for next)."""
    level = 0
    remaining = max(0, xp)
    required = xp_for_next_level(1)

    while remaining >= required and level < MAX_LEVEL:
        remaining -= required
        level += 1
        required = xp_for_next_level(level + 1)

    return level, remaining, required


async def ensure_level_config(guild_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO leveling_config (guild_id) VALUES (?) ON CONFLICT(guild_id) DO NOTHING",
            (guild_id,),
        )
        await db.commit()


async def get_level_config(guild_id: str) -> dict:
    await ensure_level_config(guild_id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT enabled, xp_min, xp_max, cooldown_seconds, stack_roles, "
            "auto_create_roles, ignored_channels, ignored_roles "
            "FROM leveling_config WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()

    keys = [
        "enabled", "xp_min", "xp_max", "cooldown_seconds", "stack_roles",
        "auto_create_roles", "ignored_channels", "ignored_roles",
    ]
    config = dict(zip(keys, row))
    config["ignored_channels"] = set(json.loads(config["ignored_channels"] or "[]"))
    config["ignored_roles"] = set(json.loads(config["ignored_roles"] or "[]"))
    return config


async def update_level_config(guild_id: str, **values):
    await ensure_level_config(guild_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [guild_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            f"UPDATE leveling_config SET {assignments} WHERE guild_id = ?",
            params,
        )
        await db.commit()


async def get_level_user(guild_id: str, user_id: str) -> dict:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO leveling_users (guild_id, user_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id, user_id) DO NOTHING",
            (guild_id, user_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT xp, message_count, last_xp, opt_out FROM leveling_users "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()

    keys = ["xp", "message_count", "last_xp", "opt_out"]
    return dict(zip(keys, row))


async def fetch_role_rewards(guild_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT level, role_id FROM leveling_role_rewards WHERE guild_id = ? ORDER BY level",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"level": level, "role_id": role_id} for level, role_id in rows]


async def apply_level_roles(guild: discord.Guild, member: discord.Member,
                            new_level: int, config: dict,
                            actor: discord.abc.User | None = None) -> list[discord.Role]:
    rewards = [reward for reward in await fetch_role_rewards(str(guild.id))
               if reward["level"] <= new_level]
    if not rewards:
        return []

    if config["stack_roles"]:
        desired = rewards
    else:
        desired = [max(rewards, key=lambda reward: reward["level"])]

    desired_ids = {int(reward["role_id"]) for reward in desired}
    granted = []

    for reward in desired:
        role = guild.get_role(int(reward["role_id"]))
        if role is None and config["auto_create_roles"]:
            try:
                role = await guild.create_role(
                    name=f"Level {reward['level']}",
                    reason=f"Auto-created level reward for level {reward['level']}",
                )
            except (discord.Forbidden, discord.HTTPException):
                role = None

            if role is not None:
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute(
                        "UPDATE leveling_role_rewards SET role_id = ? WHERE guild_id = ? AND level = ?",
                        (str(role.id), str(guild.id), reward["level"]),
                    )
                    await db.commit()
                await log_event(
                    guild,
                    "level_role_reward",
                    actor,
                    role,
                    reason=f"Auto-created reward for level {reward['level']}",
                )

        if role is None:
            continue
        if guild.me.top_role <= role:
            continue

        if role not in member.roles:
            try:
                await member.add_roles(role, reason=f"Reached level {reward['level']}")
                granted.append(role)
            except (discord.Forbidden, discord.HTTPException):
                continue

        await log_event(
            guild,
            "level_role_reward",
            actor,
            role,
            reason=f"{member.mention} earned the level {reward['level']} reward",
        )

    if not config["stack_roles"]:
        # In highest-role mode, remove lower reward roles after granting the newest one.
        for reward in rewards:
            role_id = int(reward["role_id"])
            if role_id in desired_ids:
                continue
            role = guild.get_role(role_id)
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Replaced by a higher level reward")
                except (discord.Forbidden, discord.HTTPException):
                    pass

    return granted


def progress_bar(progress: int, required: int, length: int = 12) -> str:
    if required <= 0:
        return "▰" * length
    filled = int((progress / required) * length)
    filled = max(0, min(length, filled))
    return "▰" * filled + "▱" * (length - filled)


class Leveling(commands.Cog):
    """Message XP, ranks, leaderboards, and level role rewards."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._config_cache: dict[str, tuple[dict, float]] = {}
        self._counting_channel_cache: dict[str, tuple[str | None, float]] = {}
        self._xp_cooldowns: dict[tuple[str, str], float] = {}

    async def _get_cached_config(self, guild_id: str) -> dict:
        cached = self._config_cache.get(guild_id)
        if cached and cached[1] > time.time():
            return cached[0]
        config = await get_level_config(guild_id)
        self._config_cache[guild_id] = (config, time.time() + 60)
        return config

    async def _get_counting_channel(self, guild_id: str) -> str | None:
        cached = self._counting_channel_cache.get(guild_id)
        if cached and cached[1] > time.time():
            return cached[0]

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT channel_id FROM counting_state WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                row = await cursor.fetchone()
        channel_id = row[0] if row else None
        self._counting_channel_cache[guild_id] = (channel_id, time.time() + 60)
        return channel_id

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not isinstance(message.channel, discord.TextChannel):
            return

        guild_id = str(message.guild.id)
        member = message.author
        user_key = (guild_id, str(member.id))
        now = time.time()
        if len(self._xp_cooldowns) > 5000:
            cutoff = now - 3600
            self._xp_cooldowns = {
                key: last
                for key, last in self._xp_cooldowns.items()
                if last > cutoff
            }

        # Fast path: most messages arrive during an active cooldown and can be
        # skipped without a database query.
        last_seen = self._xp_cooldowns.get(user_key, 0)
        if now - last_seen < 15:
            return

        config = await self._get_cached_config(guild_id)
        if not config["enabled"]:
            return
        if str(message.channel.id) in config["ignored_channels"]:
            return

        # Never award XP inside the counting channel; bot level-up messages would
        # interfere with the sequence.
        if await self._get_counting_channel(guild_id) == str(message.channel.id):
            return

        if any(str(role.id) in config["ignored_roles"] for role in member.roles):
            return

        user = await get_level_user(guild_id, str(member.id))
        if user["opt_out"]:
            return

        if now - float(user["last_xp"] or 0) < config["cooldown_seconds"]:
            self._xp_cooldowns[user_key] = float(user["last_xp"] or 0)
            return
        self._xp_cooldowns[user_key] = now

        xp_gain = random.randint(config["xp_min"], config["xp_max"])
        old_level, _, _ = level_for_xp(int(user["xp"]))
        new_xp = int(user["xp"]) + xp_gain

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE leveling_users SET xp = ?, message_count = ?, last_xp = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (
                    new_xp,
                    int(user["message_count"]) + 1,
                    now,
                    guild_id,
                    str(member.id),
                ),
            )
            await db.commit()

        new_level, progress, required = level_for_xp(new_xp)
        if new_level <= old_level:
            return

        granted_roles = await apply_level_roles(
            message.guild,
            member,
            new_level,
            config,
            member,
        )

        role_text = ""
        if granted_roles:
            role_text = " Rewards: " + ", ".join(role.mention for role in granted_roles)

        try:
            await message.channel.send(
                f"🎉 {member.mention} reached level **{new_level}**!{role_text}"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @app_commands.command(name="rank", description="View your rank or another member's rank.")
    async def rank(self, interaction: discord.Interaction,
                   member: Optional[discord.Member] = None):
        target = member or interaction.user
        guild_id = str(interaction.guild.id)
        user = await get_level_user(guild_id, str(target.id))

        if target.id != interaction.user.id and user["opt_out"] and \
                not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "That member has opted out of public leveling profiles.",
                ephemeral=True,
            )
            return

        level, progress, required = level_for_xp(int(user["xp"]))
        embed = base_embed(f"📈 {target.display_name}'s Rank", "primary")
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=str(level), inline=True)
        embed.add_field(name="Total XP", value=f"{int(user['xp']):,}", inline=True)
        embed.add_field(name="Messages", value=str(int(user["message_count"])), inline=True)
        embed.add_field(
            name="Progress",
            value=f"{progress_bar(progress, required)} `{progress:,}/{required:,}`",
            inline=False,
        )
        if user["opt_out"]:
            embed.set_footer(text="XP gain is currently opted out.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="Show the server XP leaderboard.")
    async def leaderboard(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT user_id, xp, message_count FROM leveling_users "
                "WHERE guild_id = ? AND opt_out = 0 ORDER BY xp DESC LIMIT 10",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                embed=base_embed("📈 Leaderboard", "neutral", "No XP has been recorded yet."),
                ephemeral=True,
            )
            return

        lines = []
        for index, (user_id, xp, messages) in enumerate(rows, start=1):
            member = interaction.guild.get_member(int(user_id))
            name = member.mention if member else f"<@{user_id}>"
            level, _, _ = level_for_xp(int(xp))
            lines.append(
                f"**{index}.** {name} — Level {level} · {int(xp):,} XP · {int(messages):,} messages"
            )

        embed = base_embed("📈 XP Leaderboard", "primary", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leveloptout", description="Opt in or out of XP gain and leaderboards.")
    async def level_opt_out(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        user = await get_level_user(guild_id, str(interaction.user.id))
        new_value = 0 if user["opt_out"] else 1
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE leveling_users SET opt_out = ? WHERE guild_id = ? AND user_id = ?",
                (new_value, guild_id, str(interaction.user.id)),
            )
            await db.commit()

        state = "opted out" if new_value else "opted back in"
        await interaction.response.send_message(
            embed=ok_embed(f"You have {state} of leveling."),
            ephemeral=True,
        )

    levelconfig = app_commands.Group(
        name="levelconfig",
        description="Configure the leveling system.",
    )

    @levelconfig.command(name="enable", description="Enable or disable leveling.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_enable(self, interaction: discord.Interaction, enabled: bool):
        await update_level_config(str(interaction.guild.id), enabled=int(enabled))
        await interaction.response.send_message(
            embed=ok_embed(f"Leveling is now {'enabled' if enabled else 'disabled'}."),
            ephemeral=True,
        )

    @levelconfig.command(name="rates", description="Set XP gain and cooldown values.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_rates(
        self,
        interaction: discord.Interaction,
        xp_min: app_commands.Range[int, 1, 500],
        xp_max: app_commands.Range[int, 1, 1000],
        cooldown_seconds: app_commands.Range[int, 5, 3600],
    ):
        if xp_min > xp_max:
            await interaction.response.send_message(
                "Minimum XP cannot be greater than maximum XP.",
                ephemeral=True,
            )
            return

        await update_level_config(
            str(interaction.guild.id),
            xp_min=xp_min,
            xp_max=xp_max,
            cooldown_seconds=cooldown_seconds,
        )
        await interaction.response.send_message(
            embed=ok_embed(
                f"Leveling rates updated: {xp_min}–{xp_max} XP per message "
                f"with a {cooldown_seconds}s cooldown."
            ),
            ephemeral=True,
        )

    @levelconfig.command(name="stacking", description="Choose whether level reward roles stack.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_stacking(self, interaction: discord.Interaction, enabled: bool):
        await update_level_config(str(interaction.guild.id), stack_roles=int(enabled))
        await interaction.response.send_message(
            embed=ok_embed(
                f"Level reward roles will {'stack' if enabled else 'only keep the highest reward'}."
            ),
            ephemeral=True,
        )

    @levelconfig.command(name="auto-create", description="Let the bot create missing reward roles.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_auto_create(self, interaction: discord.Interaction, enabled: bool):
        await update_level_config(str(interaction.guild.id), auto_create_roles=int(enabled))
        await interaction.response.send_message(
            embed=ok_embed(
                f"Automatic reward-role creation is now {'enabled' if enabled else 'disabled'}."
            ),
            ephemeral=True,
        )

    @levelconfig.command(name="ignore-channel", description="Stop awarding XP in a channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_ignore_channel(self, interaction: discord.Interaction,
                                         channel: discord.TextChannel):
        guild_id = str(interaction.guild.id)
        config = await get_level_config(guild_id)
        ignored = set(config["ignored_channels"])
        ignored.add(str(channel.id))
        await update_level_config(guild_id, ignored_channels=json.dumps(sorted(ignored)))
        await interaction.response.send_message(
            embed=ok_embed(f"XP will no longer be awarded in {channel.mention}."),
            ephemeral=True,
        )

    @levelconfig.command(name="unignore-channel", description="Resume awarding XP in a channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_unignore_channel(self, interaction: discord.Interaction,
                                           channel: discord.TextChannel):
        guild_id = str(interaction.guild.id)
        config = await get_level_config(guild_id)
        ignored = set(config["ignored_channels"])
        ignored.discard(str(channel.id))
        await update_level_config(guild_id, ignored_channels=json.dumps(sorted(ignored)))
        await interaction.response.send_message(
            embed=ok_embed(f"XP can now be awarded in {channel.mention}."),
            ephemeral=True,
        )

    @levelconfig.command(name="ignore-role", description="Stop awarding XP to a role.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_ignore_role(self, interaction: discord.Interaction,
                                      role: discord.Role):
        guild_id = str(interaction.guild.id)
        config = await get_level_config(guild_id)
        ignored = set(config["ignored_roles"])
        ignored.add(str(role.id))
        await update_level_config(guild_id, ignored_roles=json.dumps(sorted(ignored)))
        await interaction.response.send_message(
            embed=ok_embed(f"Members with {role.mention} will no longer earn XP."),
            ephemeral=True,
        )

    @levelconfig.command(name="unignore-role", description="Resume awarding XP to a role.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_unignore_role(self, interaction: discord.Interaction,
                                        role: discord.Role):
        guild_id = str(interaction.guild.id)
        config = await get_level_config(guild_id)
        ignored = set(config["ignored_roles"])
        ignored.discard(str(role.id))
        await update_level_config(guild_id, ignored_roles=json.dumps(sorted(ignored)))
        await interaction.response.send_message(
            embed=ok_embed(f"Members with {role.mention} can earn XP again."),
            ephemeral=True,
        )

    @levelconfig.command(name="view", description="View leveling configuration.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig_view(self, interaction: discord.Interaction):
        config = await get_level_config(str(interaction.guild.id))
        embed = base_embed("📈 Leveling Configuration", "primary")
        embed.add_field(name="Enabled", value="Yes" if config["enabled"] else "No", inline=True)
        embed.add_field(
            name="XP",
            value=f"{config['xp_min']}–{config['xp_max']} per message",
            inline=True,
        )
        embed.add_field(
            name="Cooldown",
            value=f"{config['cooldown_seconds']}s",
            inline=True,
        )
        embed.add_field(
            name="Role Rewards",
            value="Stack" if config["stack_roles"] else "Highest only",
            inline=True,
        )
        embed.add_field(
            name="Auto-create Roles",
            value="Yes" if config["auto_create_roles"] else "No",
            inline=True,
        )
        embed.add_field(
            name="Ignored Channels",
            value=str(len(config["ignored_channels"])),
            inline=True,
        )
        embed.add_field(
            name="Ignored Roles",
            value=str(len(config["ignored_roles"])),
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    levelroles = app_commands.Group(
        name="levelroles",
        description="Manage level role rewards.",
    )

    @levelroles.command(name="add", description="Link a role to a level reward.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def levelroles_add(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, MAX_LEVEL],
        role: discord.Role,
    ):
        if role == interaction.guild.default_role or role.managed:
            await interaction.response.send_message(
                "Use a normal, non-managed role.",
                ephemeral=True,
            )
            return
        if interaction.guild.me.top_role <= role:
            await interaction.response.send_message(
                "That role is above my highest role, so I cannot assign it.",
                ephemeral=True,
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO leveling_role_rewards (guild_id, level, role_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, level) DO UPDATE SET role_id = excluded.role_id",
                (
                    str(interaction.guild.id),
                    level,
                    str(role.id),
                    str(interaction.user.id),
                    str(discord.utils.utcnow()),
                ),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Members reaching level {level} will receive {role.mention}."),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "level_role_reward",
            interaction.user,
            role,
            reason=f"Linked role to level {level}",
        )

    @levelroles.command(name="remove", description="Remove a level role reward.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelroles_remove(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, MAX_LEVEL],
    ):
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "DELETE FROM leveling_role_rewards WHERE guild_id = ? AND level = ?",
                (str(interaction.guild.id), level),
            )
            await db.commit()

        if cursor.rowcount == 0:
            await interaction.response.send_message(
                "No role reward exists for that level.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(f"Removed the role reward for level {level}."),
            ephemeral=True,
        )

    @levelroles.command(name="list", description="List level role rewards.")
    async def levelroles_list(self, interaction: discord.Interaction):
        rewards = await fetch_role_rewards(str(interaction.guild.id))
        if not rewards:
            await interaction.response.send_message(
                embed=base_embed("🎁 Level Role Rewards", "neutral", "No role rewards are configured."),
                ephemeral=True,
            )
            return

        lines = []
        for reward in rewards:
            role = interaction.guild.get_role(int(reward["role_id"]))
            role_text = role.mention if role else f"`deleted role {reward['role_id']}`"
            lines.append(f"**Level {reward['level']}:** {role_text}")

        await interaction.response.send_message(
            embed=base_embed("🎁 Level Role Rewards", "primary", "\n".join(lines)),
            ephemeral=True,
        )

    @app_commands.command(name="levelreset", description="Reset a member's XP and message count.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def level_reset(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        remove_reward_roles: bool = False,
    ):
        guild_id = str(interaction.guild.id)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE leveling_users SET xp = 0, message_count = 0, last_xp = 0 "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, str(member.id)),
            )
            await db.commit()

        removed = 0
        if remove_reward_roles:
            rewards = await fetch_role_rewards(guild_id)
            for reward in rewards:
                role = interaction.guild.get_role(int(reward["role_id"]))
                if role is not None and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Level data reset")
                        removed += 1
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        response = f"Reset leveling data for {member.mention}."
        if remove_reward_roles:
            response += f" Removed {removed} reward role(s)."
        await interaction.response.send_message(embed=ok_embed(response), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
