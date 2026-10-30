import asyncio
import datetime
import json
import time
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cog_utils import DB_FILE, TicketConfig, get_staff_bypass_roles, log_event
from cogs.channel_tools import parse_duration
from cogs.moderation import _lock_channel, _unlock_channel
from ui_common import base_embed, ok_embed, truncate


MAX_PRESET_DURATION = datetime.timedelta(days=7)
DEFENSE_CHECK_INTERVAL = 15

BUILTIN_PRESETS = {
    "raid": {
        "preset_id": None,
        "name": "raid",
        "description": "Locks regular channels while preserving staff, logging, and verification access.",
        "mode": "raid",
        "channel_ids": [],
        "duration_seconds": 900,
        "notice": "🚨 Raid mode is active. Staff will reopen channels when it is safe.",
    },
    "verification": {
        "preset_id": None,
        "name": "verification",
        "description": "Restricts general channels while keeping verification available.",
        "mode": "verification",
        "channel_ids": [],
        "duration_seconds": None,
        "notice": "🔐 This server is temporarily restricted to verified members.",
    },
    "maintenance": {
        "preset_id": None,
        "name": "maintenance",
        "description": "Locks normal channels while staff perform maintenance.",
        "mode": "maintenance",
        "channel_ids": [],
        "duration_seconds": None,
        "notice": "🛠️ This channel is temporarily closed for maintenance.",
    },
}


# ------------------------- Lockdown presets -------------------------

async def fetch_custom_preset(guild_id: str, preset: str) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        if preset.strip().isdigit():
            async with db.execute(
                "SELECT preset_id, name, description, mode, channel_ids, duration_seconds, notice "
                "FROM lockdown_presets WHERE guild_id = ? AND preset_id = ?",
                (guild_id, int(preset)),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT preset_id, name, description, mode, channel_ids, duration_seconds, notice "
                "FROM lockdown_presets WHERE guild_id = ? AND lower(name) = lower(?)",
                (guild_id, preset.strip()),
            ) as cursor:
                row = await cursor.fetchone()

    if row is None:
        return None
    return {
        "preset_id": row[0],
        "name": row[1],
        "description": row[2],
        "mode": row[3],
        "channel_ids": json.loads(row[4] or "[]"),
        "duration_seconds": row[5],
        "notice": row[6],
    }


async def resolve_preset(guild_id: str, preset: str) -> dict | None:
    custom = await fetch_custom_preset(guild_id, preset)
    if custom is not None:
        return custom
    return BUILTIN_PRESETS.get(preset.strip().lower())


async def preset_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current_value = current.strip().lower()
    choices = [
        app_commands.Choice(name=f"{data['name'].title()} (built-in)", value=name)
        for name, data in BUILTIN_PRESETS.items()
        if not current_value or current_value in name
    ]
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT preset_id, name FROM lockdown_presets WHERE guild_id = ? "
            "AND lower(name) LIKE lower(?) ORDER BY name LIMIT 20",
            (str(interaction.guild.id), f"%{current_value}%"),
        ) as cursor:
            rows = await cursor.fetchall()
    choices.extend(
        app_commands.Choice(name=name, value=str(preset_id))
        for preset_id, name in rows
    )
    return choices[:25]


async def preset_channels(guild: discord.Guild, preset: dict) -> list[discord.TextChannel]:
    cfg = await TicketConfig.fetch(str(guild.id))
    excluded = {
        str(cfg.log_channel) if cfg.log_channel else None,
        str(cfg.verify_channel) if cfg.verify_channel else None,
    }
    excluded.discard(None)

    if preset["mode"] == "custom":
        channels = [
            guild.get_channel(int(channel_id))
            for channel_id in preset["channel_ids"]
        ]
        return [channel for channel in channels if isinstance(channel, discord.TextChannel)]

    channels = []
    for channel in guild.text_channels:
        if preset["mode"] in {"raid", "verification"} and str(channel.id) in excluded:
            continue
        if preset["mode"] == "maintenance" and cfg.log_channel == channel.id:
            continue
        channels.append(channel)
    return channels


async def active_preset_run(guild_id: str) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT run_id, preset_name, channel_ids, ends_at, started_by "
            "FROM lockdown_preset_runs WHERE guild_id = ? AND status = 'active' "
            "ORDER BY run_id DESC LIMIT 1",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "run_id": row[0],
        "preset_name": row[1],
        "channel_ids": json.loads(row[2] or "[]"),
        "ends_at": row[3],
        "started_by": row[4],
    }


async def restore_active_preset(guild: discord.Guild,
                                actor: discord.abc.User | None = None,
                                reason: str = "Lockdown preset restored") -> dict | None:
    run = await active_preset_run(str(guild.id))
    if run is None:
        return None

    restored = []
    for channel_id in run["channel_ids"]:
        channel = guild.get_channel(int(channel_id))
        if isinstance(channel, discord.TextChannel) and \
                await _unlock_channel(channel) == "unlocked":
            restored.append(channel)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE lockdown_preset_runs SET status = 'restored' WHERE run_id = ?",
            (run["run_id"],),
        )
        await db.commit()

    embed = base_embed("🔓 Lockdown Preset Restored", "primary")
    embed.add_field(name="Preset", value=run["preset_name"], inline=True)
    embed.add_field(name="Channels Restored", value=str(len(restored)), inline=True)
    embed.add_field(name="Reason", value=truncate(reason, 500), inline=False)
    await log_event(guild, "lockdown_end", embed)
    return run


async def start_preset_run(
    guild: discord.Guild,
    preset: dict,
    actor: discord.abc.User,
    *,
    duration: datetime.timedelta | None = None,
    announce_channel: discord.TextChannel | None = None,
    reason: str | None = None,
) -> dict | None:
    selected_duration = duration
    if selected_duration is None and preset.get("duration_seconds"):
        selected_duration = datetime.timedelta(seconds=int(preset["duration_seconds"]))

    # One active preset at a time. Restoring the previous run also prevents two
    # preset systems from trying to own the same channel locks.
    await restore_active_preset(guild, actor, "Replaced by a new lockdown preset")

    channels = await preset_channels(guild, preset)
    locked = []
    skipped = []
    for channel in channels:
        result = await _lock_channel(channel, str(actor.id))
        (locked if result == "locked" else skipped).append(channel)

    if not locked:
        return None

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "INSERT INTO lockdown_preset_runs (guild_id, preset_name, channel_ids, "
            "started_by, started_at, ends_at, status) VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (
                str(guild.id),
                preset["name"],
                json.dumps([str(channel.id) for channel in locked]),
                str(actor.id),
                str(discord.utils.utcnow()),
                time.time() + selected_duration.total_seconds() if selected_duration else None,
            ),
        )
        await db.commit()
        run_id = cursor.lastrowid

    if announce_channel is not None and preset.get("notice"):
        try:
            await announce_channel.send(truncate(preset["notice"], 1800))
        except (discord.Forbidden, discord.HTTPException):
            pass

    embed = base_embed("🔒 Lockdown Preset Applied", "warning")
    embed.add_field(name="Preset", value=preset["name"], inline=True)
    embed.add_field(name="Channels Locked", value=str(len(locked)), inline=True)
    if selected_duration:
        embed.add_field(name="Duration", value=str(selected_duration), inline=True)
    if reason:
        embed.add_field(name="Reason", value=truncate(reason, 500), inline=False)
    await log_event(guild, "lockdown_start", embed)
    return {"run_id": run_id, "locked": locked, "skipped": skipped}


# ------------------------- Anti-raid state -------------------------

async def ensure_antiraid_config(guild_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO anti_raid_config (guild_id) VALUES (?) "
            "ON CONFLICT(guild_id) DO NOTHING",
            (guild_id,),
        )
        await db.execute(
            "INSERT INTO anti_raid_state (guild_id) VALUES (?) "
            "ON CONFLICT(guild_id) DO NOTHING",
            (guild_id,),
        )
        await db.commit()


async def get_antiraid_config(guild_id: str) -> dict:
    await ensure_antiraid_config(guild_id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT enabled, join_threshold, window_seconds, new_account_days, "
            "quarantine_role_id, action, duration_seconds FROM anti_raid_config "
            "WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    keys = [
        "enabled", "join_threshold", "window_seconds", "new_account_days",
        "quarantine_role_id", "action", "duration_seconds",
    ]
    return dict(zip(keys, row))


async def update_antiraid_config(guild_id: str, **values):
    await ensure_antiraid_config(guild_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [guild_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            f"UPDATE anti_raid_config SET {assignments} WHERE guild_id = ?",
            params,
        )
        await db.commit()


async def get_antiraid_state(guild_id: str) -> dict:
    await ensure_antiraid_config(guild_id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT active, triggered_at, ends_at, alert_channel_id, alert_message_id, "
            "quarantined_user_ids, triggered_by FROM anti_raid_state WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return {
        "active": bool(row[0]),
        "triggered_at": row[1],
        "ends_at": row[2],
        "alert_channel_id": row[3],
        "alert_message_id": row[4],
        "quarantined_user_ids": json.loads(row[5] or "[]"),
        "triggered_by": row[6],
    }


async def update_antiraid_state(guild_id: str, **values):
    await ensure_antiraid_config(guild_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [guild_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            f"UPDATE anti_raid_state SET {assignments} WHERE guild_id = ?",
            params,
        )
        await db.commit()


def is_staff_member(member: discord.Member,
                    extra_role_ids: set[int] | None = None) -> bool:
    permissions = member.guild_permissions
    return (
        permissions.administrator
        or permissions.manage_guild
        or permissions.manage_channels
        or permissions.manage_messages
        or bool(extra_role_ids and any(role.id in extra_role_ids for role in member.roles))
    )


async def prune_antiraid_joins(guild_id: str, window_seconds: int):
    cutoff = time.time() - window_seconds
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM anti_raid_joins WHERE guild_id = ? AND joined_at < ?",
            (guild_id, cutoff),
        )
        await db.commit()


async def recent_join_counts(guild_id: str, window_seconds: int,
                             new_account_days: int) -> tuple[int, int]:
    cutoff = time.time() - window_seconds
    new_account_cutoff = time.time() - new_account_days * 86400
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM anti_raid_joins WHERE guild_id = ? AND joined_at >= ?",
            (guild_id, cutoff),
        ) as cursor:
            (all_joins,) = await cursor.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM anti_raid_joins WHERE guild_id = ? AND joined_at >= ? "
            "AND account_created_at >= ?",
            (guild_id, cutoff, new_account_cutoff),
        ) as cursor:
            (new_joins,) = await cursor.fetchone()
    return int(all_joins), int(new_joins)


async def quarantine_member(member: discord.Member, role: discord.Role) -> bool:
    bypass_roles = await get_staff_bypass_roles(str(member.guild.id))
    if is_staff_member(member, bypass_roles) or role in member.roles:
        return False
    try:
        await member.add_roles(role, reason="Anti-raid quarantine")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


class AntiRaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Disarm",
        style=discord.ButtonStyle.danger,
        emoji="🛡️",
        custom_id="antiraid:disarm",
    )
    async def disarm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server permission to disarm anti-raid mode.",
                ephemeral=True,
            )
            return
        disarmed = await disarm_antiraid(interaction.guild, interaction.user, "Disarmed from staff alert")
        if disarmed:
            await interaction.response.send_message("Anti-raid mode disarmed.", ephemeral=True)
        else:
            await interaction.response.send_message("Anti-raid mode is not active.", ephemeral=True)

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="antiraid:status",
    )
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = await get_antiraid_config(str(interaction.guild.id))
        state = await get_antiraid_state(str(interaction.guild.id))
        await interaction.response.send_message(
            embed=await antiraid_status_embed(interaction.guild, config, state),
            ephemeral=True,
        )


async def antiraid_status_embed(guild: discord.Guild,
                                config: dict,
                                state: dict) -> discord.Embed:
    embed = base_embed("🚨 Anti-Raid Status", "warning" if state["active"] else "primary")
    embed.add_field(name="Enabled", value="Yes" if config["enabled"] else "No", inline=True)
    embed.add_field(name="Active", value="Yes" if state["active"] else "No", inline=True)
    embed.add_field(
        name="Threshold",
        value=f"{config['join_threshold']} joins / {config['window_seconds']}s",
        inline=True,
    )
    embed.add_field(name="Action", value=config["action"].replace("_", " ").title(), inline=True)
    if config["duration_seconds"]:
        embed.add_field(
            name="Duration",
            value=str(datetime.timedelta(seconds=config["duration_seconds"])),
            inline=True,
        )
    if state["active"] and state["ends_at"]:
        embed.add_field(
            name="Auto-Disarm",
            value=f"<t:{int(state['ends_at'])}:R>",
            inline=True,
        )
    return embed


async def trigger_antiraid(guild: discord.Guild,
                           actor: discord.abc.User | None,
                           reason: str) -> bool:
    guild_id = str(guild.id)
    config = await get_antiraid_config(guild_id)
    state = await get_antiraid_state(guild_id)
    if state["active"]:
        return False

    duration = None
    if config["duration_seconds"]:
        duration = datetime.timedelta(seconds=config["duration_seconds"])
    ends_at = time.time() + duration.total_seconds() if duration else None

    quarantined: list[str] = []
    if config["action"] in {"lockdown", "lockdown_quarantine"}:
        cfg = await TicketConfig.fetch(guild_id)
        log_channel = guild.get_channel(cfg.log_channel) if cfg.log_channel else None
        await start_preset_run(
            guild,
            BUILTIN_PRESETS["raid"],
            actor or guild.me,
            duration=duration,
            announce_channel=log_channel if isinstance(log_channel, discord.TextChannel) else None,
            reason=reason,
        )

    alert_message = None
    alert_channel = None
    embed = base_embed("🚨 Anti-Raid Triggered", "warning")
    embed.add_field(name="Reason", value=truncate(reason, 500), inline=False)
    embed.add_field(name="Action", value=config["action"].replace("_", " ").title(), inline=True)
    if duration:
        embed.add_field(name="Auto-Disarm", value=str(duration), inline=True)

    cfg = await TicketConfig.fetch(guild_id)
    channel = guild.get_channel(cfg.log_channel) if cfg.log_channel else None
    if isinstance(channel, discord.TextChannel):
        try:
            alert_message = await channel.send(embed=embed, view=AntiRaidView())
            alert_channel = channel
        except (discord.Forbidden, discord.HTTPException):
            alert_message = None

    if alert_message is None:
        await log_event(guild, "anti_raid_trigger", embed)

    await update_antiraid_state(
        guild_id,
        active=1,
        triggered_at=time.time(),
        ends_at=ends_at,
        alert_channel_id=str(alert_channel.id) if alert_channel else None,
        alert_message_id=str(alert_message.id) if alert_message else None,
        quarantined_user_ids=json.dumps(quarantined),
        triggered_by=str(actor.id) if actor else None,
    )

    if actor is not None:
        embed.add_field(name="Armed by", value=actor.mention, inline=True)
        await log_event(guild, "anti_raid_arm", embed)
    return True


async def disarm_antiraid(guild: discord.Guild,
                          actor: discord.abc.User | None,
                          reason: str) -> bool:
    guild_id = str(guild.id)
    state = await get_antiraid_state(guild_id)
    if not state["active"]:
        return False

    await restore_active_preset(guild, actor, "Anti-raid disarmed")

    removed = 0
    guild_state = await get_antiraid_state(guild_id)
    for user_id in guild_state["quarantined_user_ids"]:
        member = guild.get_member(int(user_id))
        if member is None:
            continue
        config = await get_antiraid_config(guild_id)
        if config["quarantine_role_id"]:
            role = guild.get_role(int(config["quarantine_role_id"]))
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Anti-raid disarmed")
                    removed += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass

    await update_antiraid_state(
        guild_id,
        active=0,
        ends_at=None,
        alert_channel_id=None,
        alert_message_id=None,
        quarantined_user_ids="[]",
        triggered_by=None,
    )

    embed = base_embed("🛡️ Anti-Raid Disarmed", "primary")
    embed.add_field(name="Reason", value=truncate(reason, 500), inline=False)
    embed.add_field(name="Quarantine Roles Removed", value=str(removed), inline=True)
    await log_event(guild, "anti_raid_disarm", embed)
    return True


async def restore_expired_defense_states(bot: commands.Bot):
    now = time.time()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT guild_id, preset_name FROM lockdown_preset_runs "
            "WHERE status = 'active' AND ends_at IS NOT NULL AND ends_at <= ?",
            (now,),
        ) as cursor:
            expired_runs = await cursor.fetchall()
        async with db.execute(
            "SELECT guild_id FROM anti_raid_state WHERE active = 1 "
            "AND ends_at IS NOT NULL AND ends_at <= ?",
            (now,),
        ) as cursor:
            expired_raids = await cursor.fetchall()

    for guild_id, _preset_name in expired_runs:
        guild = bot.get_guild(int(guild_id))
        if guild is not None:
            await restore_active_preset(guild, None, "Lockdown preset duration expired")

    for (guild_id,) in expired_raids:
        guild = bot.get_guild(int(guild_id))
        if guild is not None:
            await disarm_antiraid(guild, None, "Anti-raid duration expired")


# ------------------------- Slash-command groups -------------------------

class LockdownPresetGroup(app_commands.Group):
    """Saved and built-in lockdown presets."""

    def __init__(self):
        super().__init__(name="preset", description="Run and manage lockdown presets.")

    @app_commands.command(name="list", description="List built-in and custom lockdown presets.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT preset_id, name, description, mode, duration_seconds "
                "FROM lockdown_presets WHERE guild_id = ? ORDER BY name",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()

        embed = base_embed("🔒 Lockdown Presets", "primary")
        for name, data in BUILTIN_PRESETS.items():
            duration = str(datetime.timedelta(seconds=data["duration_seconds"])) if data["duration_seconds"] else "Manual"
            embed.add_field(
                name=f"{name.title()} · Built-in",
                value=truncate(f"{data['description']}\nDuration: {duration}", 400),
                inline=False,
            )
        for preset_id, name, description, mode, duration_seconds in rows[:20]:
            duration = str(datetime.timedelta(seconds=duration_seconds)) if duration_seconds else "Manual"
            embed.add_field(
                name=f"{name} · #{preset_id}",
                value=truncate(f"{description or mode.title()} mode\nDuration: {duration}", 400),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="create", description="Create a custom lockdown preset.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 80],
        description: Optional[str],
        mode: app_commands.Choice[str],
        duration: Optional[str] = None,
        notice: Optional[str] = None,
    ):
        try:
            delta = parse_duration(duration, MAX_PRESET_DURATION) if duration else None
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_FILE) as db:
                cursor = await db.execute(
                    "INSERT INTO lockdown_presets (guild_id, name, description, mode, "
                    "channel_ids, duration_seconds, notice, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                    (
                        str(interaction.guild.id),
                        name.strip(),
                        description,
                        mode.value,
                        int(delta.total_seconds()) if delta else None,
                        notice,
                        str(interaction.user.id),
                        str(discord.utils.utcnow()),
                    ),
                )
                await db.commit()
                preset_id = cursor.lastrowid
        except aiosqlite.IntegrityError:
            await interaction.response.send_message("A preset with that name already exists.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=ok_embed(
                f"Preset **{name.strip()}** created with ID `{preset_id}`. "
                "For custom mode, add channels with `/lockdown preset channel-add`."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="Edit a custom lockdown preset.")
    @app_commands.autocomplete(preset=preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_edit(
        self,
        interaction: discord.Interaction,
        preset: str,
        name: Optional[app_commands.Range[str, 1, 80]] = None,
        description: Optional[str] = None,
        mode: Optional[app_commands.Choice[str]] = None,
        duration: Optional[str] = None,
        notice: Optional[str] = None,
    ):
        data = await fetch_custom_preset(str(interaction.guild.id), preset)
        if data is None:
            await interaction.response.send_message("Custom preset not found.", ephemeral=True)
            return

        try:
            delta = parse_duration(duration, MAX_PRESET_DURATION) if duration else None
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        values = {}
        if name is not None:
            values["name"] = name.strip()
        if description is not None:
            values["description"] = description
        if mode is not None:
            values["mode"] = mode.value
        if duration is not None:
            values["duration_seconds"] = int(delta.total_seconds()) if delta else None
        if notice is not None:
            values["notice"] = notice

        if not values:
            await interaction.response.send_message("No changes were provided.", ephemeral=True)
            return

        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    f"UPDATE lockdown_presets SET {assignments} WHERE guild_id = ? AND preset_id = ?",
                    list(values.values()) + [str(interaction.guild.id), data["preset_id"]],
                )
                await db.commit()
        except aiosqlite.IntegrityError:
            await interaction.response.send_message("A preset with that name already exists.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=ok_embed(f"Preset **{values.get('name', data['name'])}** updated."),
            ephemeral=True,
        )

    @app_commands.command(name="delete", description="Delete a custom lockdown preset.")
    @app_commands.autocomplete(preset=preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_delete(self, interaction: discord.Interaction, preset: str):
        data = await fetch_custom_preset(str(interaction.guild.id), preset)
        if data is None:
            await interaction.response.send_message("Custom preset not found.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM lockdown_presets WHERE guild_id = ? AND preset_id = ?",
                (str(interaction.guild.id), data["preset_id"]),
            )
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed(f"Preset **{data['name']}** deleted."),
            ephemeral=True,
        )

    @app_commands.command(name="channel-add", description="Add a channel to a custom preset.")
    @app_commands.autocomplete(preset=preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_channel_add(
        self,
        interaction: discord.Interaction,
        preset: str,
        channel: discord.TextChannel,
    ):
        data = await fetch_custom_preset(str(interaction.guild.id), preset)
        if data is None:
            await interaction.response.send_message("Custom preset not found.", ephemeral=True)
            return
        channels = set(data["channel_ids"])
        channels.add(str(channel.id))
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE lockdown_presets SET channel_ids = ? WHERE preset_id = ?",
                (json.dumps(sorted(channels)), data["preset_id"]),
            )
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed(f"Added {channel.mention} to **{data['name']}**."),
            ephemeral=True,
        )

    @app_commands.command(name="channel-remove", description="Remove a channel from a custom preset.")
    @app_commands.autocomplete(preset=preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_channel_remove(
        self,
        interaction: discord.Interaction,
        preset: str,
        channel: discord.TextChannel,
    ):
        data = await fetch_custom_preset(str(interaction.guild.id), preset)
        if data is None:
            await interaction.response.send_message("Custom preset not found.", ephemeral=True)
            return
        channels = set(data["channel_ids"])
        channels.discard(str(channel.id))
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE lockdown_presets SET channel_ids = ? WHERE preset_id = ?",
                (json.dumps(sorted(channels)), data["preset_id"]),
            )
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed(f"Removed {channel.mention} from **{data['name']}**."),
            ephemeral=True,
        )

    @app_commands.command(name="channel-list", description="List channels attached to a custom preset.")
    @app_commands.autocomplete(preset=preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_channel_list(self, interaction: discord.Interaction, preset: str):
        data = await fetch_custom_preset(str(interaction.guild.id), preset)
        if data is None:
            await interaction.response.send_message("Custom preset not found.", ephemeral=True)
            return

        if not data["channel_ids"]:
            await interaction.response.send_message(
                embed=base_embed("🔒 Preset Channels", "neutral", "This preset has no custom channels."),
                ephemeral=True,
            )
            return

        lines = []
        for channel_id in data["channel_ids"]:
            channel = interaction.guild.get_channel(int(channel_id))
            lines.append(channel.mention if channel else f"`deleted channel {channel_id}`")

        await interaction.response.send_message(
            embed=base_embed(
                f"🔒 Channels for {data['name']}",
                "primary",
                "\n".join(lines),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="run", description="Apply a lockdown preset.")
    @app_commands.autocomplete(preset=preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def preset_run(
        self,
        interaction: discord.Interaction,
        preset: str,
        duration: Optional[str] = None,
    ):
        data = await resolve_preset(str(interaction.guild.id), preset)
        if data is None:
            await interaction.response.send_message("Preset not found.", ephemeral=True)
            return
        try:
            delta = parse_duration(duration, MAX_PRESET_DURATION) if duration else None
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        result = await start_preset_run(
            interaction.guild,
            data,
            interaction.user,
            duration=delta,
            announce_channel=interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None,
            reason="Applied through /lockdown preset run",
        )
        if result is None:
            await interaction.followup.send(
                "I couldn't lock any channels for that preset.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=ok_embed(
                f"Preset **{data['name']}** applied to {len(result['locked'])} channel(s)."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="active", description="Show active lockdown preset runs.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preset_active(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT run_id, preset_name, started_by, ends_at FROM lockdown_preset_runs "
                "WHERE guild_id = ? AND status = 'active' ORDER BY run_id DESC",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🔒 Active Presets", "neutral", "No lockdown presets are active."),
                ephemeral=True,
            )
            return
        embed = base_embed("🔒 Active Lockdown Presets", "primary")
        for run_id, name, actor_id, ends_at in rows[:10]:
            value = f"Run: #{run_id}\nStarted by: <@{actor_id}>"
            if ends_at:
                value += f"\nExpires: <t:{int(ends_at)}:R>"
            embed.add_field(name=name, value=value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="restore", description="Restore the currently active lockdown preset.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def preset_restore(self, interaction: discord.Interaction):
        restored = await restore_active_preset(
            interaction.guild,
            interaction.user,
            "Restored through /lockdown preset restore",
        )
        if restored is None:
            await interaction.response.send_message("No active lockdown preset was found.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=ok_embed(f"Preset **{restored['preset_name']}** restored."),
            ephemeral=True,
        )


class ServerDefense(commands.Cog):
    """Anti-raid detection and response controls."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.defense_task.start()

    async def cog_unload(self):
        self.defense_task.cancel()
        task = self.defense_task.get_task()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    @tasks.loop(seconds=DEFENSE_CHECK_INTERVAL)
    async def defense_task(self):
        await restore_expired_defense_states(self.bot)

    @defense_task.before_loop
    async def before_defense_task(self):
        await self.bot.wait_until_ready()

    antiraid = app_commands.Group(
        name="antiraid",
        description="Configure and control anti-raid mode.",
    )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = str(member.guild.id)
        config = await get_antiraid_config(guild_id)
        state = await get_antiraid_state(guild_id)
        if not config["enabled"] and not state["active"]:
            return

        created_timestamp = member.created_at.timestamp() if member.created_at else 0
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO anti_raid_joins (guild_id, user_id, joined_at, account_created_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, str(member.id), time.time(), created_timestamp),
            )
            await db.commit()

        await prune_antiraid_joins(guild_id, config["window_seconds"])
        all_joins, new_joins = await recent_join_counts(
            guild_id,
            config["window_seconds"],
            config["new_account_days"],
        )

        if config["enabled"] and not state["active"] and (
            all_joins >= config["join_threshold"] or new_joins >= config["join_threshold"]
        ):
            await trigger_antiraid(
                member.guild,
                None,
                f"Detected {all_joins} joins / {new_joins} new accounts in "
                f"{config['window_seconds']} seconds",
            )

        latest_state = await get_antiraid_state(guild_id)
        if latest_state["active"] and config["action"] in {"quarantine", "lockdown_quarantine"}:
            role = None
            if config["quarantine_role_id"]:
                role = member.guild.get_role(int(config["quarantine_role_id"]))
            if role is not None and await quarantine_member(member, role):
                quarantined = set(latest_state["quarantined_user_ids"])
                quarantined.add(str(member.id))
                await update_antiraid_state(
                    guild_id,
                    quarantined_user_ids=json.dumps(sorted(quarantined)),
                )

    @antiraid.command(name="enable", description="Enable or disable automatic anti-raid detection.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_enable(self, interaction: discord.Interaction, enabled: bool):
        await update_antiraid_config(str(interaction.guild.id), enabled=int(enabled))
        await interaction.response.send_message(
            embed=ok_embed(f"Automatic anti-raid detection is now {'enabled' if enabled else 'disabled'}."),
            ephemeral=True,
        )

    @antiraid.command(name="threshold", description="Set join-rate detection threshold.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_threshold(
        self,
        interaction: discord.Interaction,
        joins: app_commands.Range[int, 2, 100],
        window_seconds: app_commands.Range[int, 5, 3600],
    ):
        await update_antiraid_config(
            str(interaction.guild.id),
            join_threshold=joins,
            window_seconds=window_seconds,
        )
        await interaction.response.send_message(
            embed=ok_embed(f"Anti-raid will trigger at {joins} joins within {window_seconds} seconds."),
            ephemeral=True,
        )

    @antiraid.command(name="new-account", description="Set the new-account age used by detection.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_new_account(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 365],
    ):
        await update_antiraid_config(str(interaction.guild.id), new_account_days=days)
        await interaction.response.send_message(
            embed=ok_embed(f"Accounts younger than {days} day(s) count as new accounts."),
            ephemeral=True,
        )

    @antiraid.command(name="action", description="Set the anti-raid response and duration.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_action(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: Optional[str] = None,
    ):
        try:
            delta = parse_duration(duration, MAX_PRESET_DURATION) if duration else None
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await update_antiraid_config(
            str(interaction.guild.id),
            action=action.value,
            duration_seconds=int(delta.total_seconds()) if delta else None,
        )
        await interaction.response.send_message(
            embed=ok_embed(f"Anti-raid action set to {action.name.replace('_', ' ')}."),
            ephemeral=True,
        )

    @antiraid.command(name="quarantine-role", description="Set the role used to quarantine new arrivals.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def antiraid_quarantine_role(self, interaction: discord.Interaction,
                                       role: Optional[discord.Role] = None):
        if role is not None:
            if role == interaction.guild.default_role or role.managed:
                await interaction.response.send_message("Use a normal, non-managed role.", ephemeral=True)
                return
            if interaction.guild.me.top_role <= role:
                await interaction.response.send_message(
                    "That role is above my highest role, so I cannot assign it.",
                    ephemeral=True,
                )
                return

        await update_antiraid_config(
            str(interaction.guild.id),
            quarantine_role_id=str(role.id) if role else None,
        )
        message = f"Quarantine role set to {role.mention}." if role else "Quarantine role cleared."
        await interaction.response.send_message(embed=ok_embed(message), ephemeral=True)

    @antiraid.command(name="status", description="View anti-raid settings and state.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_status(self, interaction: discord.Interaction):
        config = await get_antiraid_config(str(interaction.guild.id))
        state = await get_antiraid_state(str(interaction.guild.id))
        await interaction.response.send_message(
            embed=await antiraid_status_embed(interaction.guild, config, state),
            ephemeral=True,
        )

    @antiraid.command(name="arm", description="Manually trigger the configured anti-raid response.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_arm(self, interaction: discord.Interaction,
                           reason: Optional[str] = None):
        triggered = await trigger_antiraid(
            interaction.guild,
            interaction.user,
            reason or "Manually armed by staff",
        )
        if not triggered:
            await interaction.response.send_message("Anti-raid mode is already active.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=ok_embed("Anti-raid mode armed."),
            ephemeral=True,
        )

    @antiraid.command(name="disarm", description="Disarm anti-raid mode and restore its response.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_disarm(self, interaction: discord.Interaction,
                              reason: Optional[str] = None):
        disarmed = await disarm_antiraid(
            interaction.guild,
            interaction.user,
            reason or "Manually disarmed by staff",
        )
        if not disarmed:
            await interaction.response.send_message("Anti-raid mode is not active.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=ok_embed("Anti-raid mode disarmed."),
            ephemeral=True,
        )

    @antiraid.command(name="reset", description="Clear anti-raid join history and inactive state.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antiraid_reset(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        state = await get_antiraid_state(guild_id)
        if state["active"]:
            await interaction.response.send_message(
                "Anti-raid mode is active. Use `/antiraid disarm` first so lockdowns and quarantine roles are restored safely.",
                ephemeral=True,
            )
            return
        await update_antiraid_state(
            guild_id,
            active=0,
            ends_at=None,
            alert_channel_id=None,
            alert_message_id=None,
            quarantined_user_ids="[]",
            triggered_by=None,
        )
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM anti_raid_joins WHERE guild_id = ?", (guild_id,))
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed("Anti-raid history and inactive state were reset."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerDefense(bot))
