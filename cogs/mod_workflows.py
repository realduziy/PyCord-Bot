import datetime
import json
import time
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput

from cog_utils import DB_FILE, log_event, mark_automod_deleted
from cogs.channel_tools import parse_duration
from cogs.moderation import _actor_can_act, _bot_can_act, _modlog
from ui_common import base_embed, ok_embed, truncate


CASE_STATUS_COLOR = "primary"


async def fetch_case(guild_id: str, case_id: int) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT case_id, action, actor_id, target_id, reason, created_at, "
            "log_channel_id, log_message_id, edited_by, edited_at "
            "FROM mod_cases WHERE guild_id = ? AND case_id = ?",
            (guild_id, case_id),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return None
    keys = [
        "case_id", "action", "actor_id", "target_id", "reason", "created_at",
        "log_channel_id", "log_message_id", "edited_by", "edited_at",
    ]
    return dict(zip(keys, row))


async def case_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT case_id, action, target_id FROM mod_cases WHERE guild_id = ? "
            "AND CAST(case_id AS TEXT) LIKE ? ORDER BY case_id DESC LIMIT 25",
            (str(interaction.guild.id), f"%{current.strip()}%"),
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        app_commands.Choice(name=f"#{case_id} {action}", value=str(case_id))
        for case_id, action, target_id in rows
    ]


def target_mention(guild: discord.Guild, target_id: str) -> str:
    """Use the proper mention syntax for members, channels, or roles."""
    try:
        target_value = int(target_id)
    except ValueError:
        return target_id
    target = guild.get_member(target_value)
    if target is not None:
        return target.mention
    if guild.get_channel(target_value) is not None:
        return f"<#{target_value}>"
    if guild.get_role(target_value) is not None:
        return f"<@&{target_value}>"
    return f"<@{target_id}>"


def build_case_embed(case: dict, guild: discord.Guild) -> discord.Embed:
    actor = guild.get_member(int(case["actor_id"]))
    target = target_mention(guild, case["target_id"])
    embed = base_embed(
        f"🛡️ {case['action']} · Case #{case['case_id']}",
        CASE_STATUS_COLOR,
    )
    embed.add_field(
        name="Moderator",
        value=actor.mention if actor else f"<@{case['actor_id']}>",
        inline=True,
    )
    embed.add_field(
        name="Target",
        value=target,
        inline=True,
    )
    if case["reason"]:
        embed.add_field(name="Reason", value=truncate(case["reason"]), inline=False)
    if case["edited_by"]:
        edited_by = guild.get_member(int(case["edited_by"]))
        embed.add_field(
            name="Last edited by",
            value=edited_by.mention if edited_by else f"<@{case['edited_by']}>",
            inline=True,
        )
    if case["edited_at"]:
        try:
            edited_timestamp = int(
                datetime.datetime.fromisoformat(case["edited_at"]).timestamp()
            )
            embed.add_field(
                name="Last edited",
                value=f"<t:{edited_timestamp}:R>",
                inline=True,
            )
        except ValueError:
            pass
    return embed


async def edit_case_reason(interaction: discord.Interaction,
                           case_id: int,
                           new_reason: str) -> bool:
    guild_id = str(interaction.guild.id)
    case = await fetch_case(guild_id, case_id)
    if case is None:
        return False

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE mod_cases SET reason = ?, edited_by = ?, edited_at = ? "
            "WHERE guild_id = ? AND case_id = ?",
            (
                new_reason.strip(),
                str(interaction.user.id),
                str(discord.utils.utcnow()),
                guild_id,
                case_id,
            ),
        )
        await db.commit()

    updated = await fetch_case(guild_id, case_id)
    if case["log_channel_id"] and case["log_message_id"]:
        channel = interaction.guild.get_channel(int(case["log_channel_id"]))
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(int(case["log_message_id"]))
                await message.edit(embed=build_case_embed(updated, interaction.guild))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    embed = base_embed(f"✏️ Case #{case_id} Updated", "primary")
    embed.add_field(name="Action", value=case["action"], inline=True)
    embed.add_field(name="Target", value=target_mention(interaction.guild, case["target_id"]), inline=True)
    embed.add_field(name="New reason", value=truncate(new_reason), inline=False)
    await log_event(interaction.guild, "mod_case_update", embed)
    return True


async def get_role_maintenance_enabled(guild_id: str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT enabled FROM role_maintenance_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return bool(row[0]) if row else False


async def get_ignored_maintenance_roles(guild_id: str) -> set[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT role_id FROM role_maintenance_ignored_roles WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


def eligible_snapshot_roles(member: discord.Member, ignored_roles: set[str]) -> list[discord.Role]:
    return [
        role
        for role in member.roles
        if role != member.guild.default_role
        and not role.managed
        and role < member.guild.me.top_role
        and str(role.id) not in ignored_roles
    ]


async def snapshot_member_roles(member: discord.Member,
                                reason: str = "Before moderation action"):
    """Save removable roles before a kick/ban if maintenance is enabled."""
    guild_id = str(member.guild.id)
    if not await get_role_maintenance_enabled(guild_id):
        return False

    ignored = await get_ignored_maintenance_roles(guild_id)
    role_ids = [str(role.id) for role in eligible_snapshot_roles(member, ignored)]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO role_snapshots (guild_id, user_id, role_ids, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "role_ids = excluded.role_ids, active = 1, updated_at = excluded.updated_at",
            (
                guild_id,
                str(member.id),
                json.dumps(role_ids),
                str(discord.utils.utcnow()),
                str(discord.utils.utcnow()),
            ),
        )
        await db.commit()
    return True


async def fetch_role_snapshot(guild_id: str, user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT role_ids, active, updated_at FROM role_snapshots "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "role_ids": json.loads(row[0] or "[]"),
        "active": bool(row[1]),
        "updated_at": row[2],
    }


async def restore_snapshot_roles(guild: discord.Guild,
                                 member: discord.Member,
                                 actor: discord.abc.User | None = None) -> tuple[int, int]:
    guild_id = str(guild.id)
    snapshot = await fetch_role_snapshot(guild_id, str(member.id))
    if snapshot is None or not snapshot["active"]:
        return 0, 0

    ignored = await get_ignored_maintenance_roles(guild_id)
    restored = skipped = 0
    for role_id in snapshot["role_ids"]:
        role = guild.get_role(int(role_id))
        if role is None or role.managed or str(role.id) in ignored:
            skipped += 1
            continue
        if guild.me.top_role <= role:
            skipped += 1
            continue
        if role in member.roles:
            restored += 1
            continue
        try:
            await member.add_roles(role, reason="Role maintenance restore")
            restored += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped += 1

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE role_snapshots SET active = 0, updated_at = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (str(discord.utils.utcnow()), guild_id, str(member.id)),
        )
        await db.commit()

    embed = base_embed("🧰 Roles Restored", "primary")
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Restored", value=str(restored), inline=True)
    embed.add_field(name="Skipped", value=str(skipped), inline=True)
    await log_event(guild, "role_restored", embed)
    return restored, skipped


class ReasonModal(Modal):
    def __init__(self, member: discord.Member, action: str):
        title = f"{action} {member.display_name}"[:45]
        super().__init__(title=title, timeout=180)
        self.member = member
        self.action = action
        self.reason = TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            placeholder="Reason shown in the mod case and user DM",
            max_length=900,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await perform_member_action(
            interaction,
            self.member,
            self.action,
            self.reason.value.strip(),
        )


class TimeoutModal(Modal):
    def __init__(self, member: discord.Member, source_message: discord.Message | None = None):
        super().__init__(title=f"Timeout {member.display_name}"[:45], timeout=180)
        self.member = member
        self.source_message = source_message
        self.duration = TextInput(
            label="Duration",
            placeholder="Examples: 10m, 2h, 1d",
            max_length=20,
            required=True,
        )
        self.reason = TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=True,
        )
        self.add_item(self.duration)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            delta = parse_duration(self.duration.value, datetime.timedelta(days=28))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await perform_member_action(
            interaction,
            self.member,
            "Timeout",
            self.reason.value.strip(),
            duration=delta,
            source_message=self.source_message,
        )


async def perform_member_action(
    interaction: discord.Interaction,
    member: discord.Member,
    action: str,
    reason: str,
    duration: datetime.timedelta | None = None,
    source_message: discord.Message | None = None,
):
    guild = interaction.guild
    if member.bot:
        await interaction.response.send_message("That action cannot target bots.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("You cannot use that action on yourself.", ephemeral=True)
        return
    if not _actor_can_act(interaction.user, member):
        await interaction.response.send_message(
            "You cannot moderate someone with an equal or higher role.",
            ephemeral=True,
        )
        return
    if not _bot_can_act(guild, member):
        await interaction.response.send_message(
            "My highest role must be above the target's highest role.",
            ephemeral=True,
        )
        return

    dm_notice = f"You were {action.lower()} in **{guild.name}**."
    if reason:
        dm_notice += f"\nReason: {reason}"
    if duration:
        dm_notice += f"\nDuration: {format_timedelta(duration)}"

    # Notify before kicks/bans while the shared guild still exists. Warnings and
    # timeouts can notify after the Discord-side action completes.
    if action in {"Kick", "Ban", "Warn", "Timeout"}:
        try:
            await member.send(dm_notice)
        except (discord.Forbidden, discord.HTTPException):
            pass

    extra = None
    try:
        if action == "Warn":
            pass
        elif action == "Timeout":
            await member.timeout_for(duration, reason=reason)
            extra = f"Duration: {format_timedelta(duration)}"
        elif action == "Kick":
            await snapshot_member_roles(member, "Before kick")
            await member.kick(reason=reason)
        elif action == "Ban":
            await snapshot_member_roles(member, "Before ban")
            await member.ban(reason=reason, delete_message_days=0)
        else:
            raise ValueError(f"Unsupported action: {action}")
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(
            f"I couldn't {action.lower()} that member.",
            ephemeral=True,
        )
        return

    if source_message is not None:
        extra = (extra + "\n" if extra else "") + f"Source: {source_message.jump_url}"

    case_id = await _modlog(guild, action, interaction.user, member, reason, extra)
    await interaction.response.send_message(
        embed=ok_embed(f"{action} applied to {member.mention}. Case #{case_id}."),
        ephemeral=True,
    )


def format_timedelta(delta: datetime.timedelta) -> str:
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


class ModWorkflows(commands.Cog):
    """Context-menu moderation, editable cases, statistics, and role maintenance."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.context_menus: list[app_commands.ContextMenu] = []

    async def cog_load(self):
        # discord.py ContextMenu objects are top-level app commands and cannot
        # be decorated inside a cog class. Register bound callback methods here.
        self.context_menus = [
            app_commands.ContextMenu(name="Warn User", callback=self.warn_user_context),
            app_commands.ContextMenu(name="Timeout User", callback=self.timeout_user_context),
            app_commands.ContextMenu(name="Kick User", callback=self.kick_user_context),
            app_commands.ContextMenu(name="Ban User", callback=self.ban_user_context),
            app_commands.ContextMenu(name="View Mod History", callback=self.mod_history_context),
            app_commands.ContextMenu(name="Delete Message", callback=self.delete_message_context),
            app_commands.ContextMenu(name="Report Message", callback=self.report_message_context),
            app_commands.ContextMenu(name="Timeout Message Author", callback=self.timeout_message_author_context),
        ]
        for menu in self.context_menus:
            self.bot.tree.add_command(menu)

    async def cog_unload(self):
        for menu in self.context_menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    # ---------- User context menus ----------

    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_user_context(self, interaction: discord.Interaction,
                                member: discord.Member):
        await interaction.response.send_modal(ReasonModal(member, "Warn"))

    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout_user_context(self, interaction: discord.Interaction,
                                   member: discord.Member):
        await interaction.response.send_modal(TimeoutModal(member))

    @app_commands.checks.has_permissions(kick_members=True)
    async def kick_user_context(self, interaction: discord.Interaction,
                                member: discord.Member):
        await interaction.response.send_modal(ReasonModal(member, "Kick"))

    @app_commands.checks.has_permissions(ban_members=True)
    async def ban_user_context(self, interaction: discord.Interaction,
                               member: discord.Member):
        await interaction.response.send_modal(ReasonModal(member, "Ban"))

    @app_commands.checks.has_permissions(moderate_members=True)
    async def mod_history_context(self, interaction: discord.Interaction,
                                  member: discord.Member):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT case_id, action, reason, created_at FROM mod_cases "
                "WHERE guild_id = ? AND target_id = ? ORDER BY case_id DESC LIMIT 10",
                (str(interaction.guild.id), str(member.id)),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🛡️ Mod History", "neutral", f"{member.mention} has no recorded cases."),
                ephemeral=True,
            )
            return

        embed = base_embed(f"🛡️ History for {member.display_name}", "primary")
        for case_id, action, reason, created_at in rows:
            try:
                timestamp = int(datetime.datetime.fromisoformat(created_at).timestamp())
                when = f"<t:{timestamp}:R>"
            except ValueError:
                when = created_at
            embed.add_field(
                name=f"#{case_id} · {action}",
                value=truncate(f"{when}\n{reason or 'No reason recorded'}", 250),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Message context menus ----------

    @app_commands.checks.has_permissions(manage_messages=True)
    async def delete_message_context(self, interaction: discord.Interaction,
                                     message: discord.Message):
        if not isinstance(message.author, discord.Member) or message.author.bot:
            await interaction.response.send_message(
                "That message does not have a valid member author.",
                ephemeral=True,
            )
            return
        # Prevent the raw delete logger from posting a second event; this action
        # creates a dedicated mod case below.
        mark_automod_deleted(message.id)
        try:
            await message.delete(reason=f"Deleted by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't delete that message.",
                ephemeral=True,
            )
            return

        case_id = await _modlog(
            interaction.guild,
            "Message Delete",
            interaction.user,
            message.author,
            "Deleted through message context menu",
            extra=f"Source: {message.jump_url}",
        )
        await interaction.response.send_message(
            embed=ok_embed(f"Message deleted. Case #{case_id}."),
            ephemeral=True,
        )

    async def report_message_context(self, interaction: discord.Interaction,
                                     message: discord.Message):
        embed = base_embed("🚩 Message Report", "warning")
        embed.add_field(name="Reporter", value=interaction.user.mention, inline=True)
        embed.add_field(
            name="Author",
            value=message.author.mention if message.author else "Unknown",
            inline=True,
        )
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Content", value=truncate(message.content or "[No text]"), inline=False)
        embed.add_field(name="Jump", value=f"[Open Message]({message.jump_url})", inline=False)
        await log_event(interaction.guild, "message_report", embed)
        await interaction.response.send_message(
            "Message reported to staff.",
            ephemeral=True,
        )

    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout_message_author_context(self, interaction: discord.Interaction,
                                             message: discord.Message):
        if not isinstance(message.author, discord.Member) or message.author.bot:
            await interaction.response.send_message(
                "That message does not have a valid member author.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TimeoutModal(message.author, message))

    # ---------- Editable cases ----------

    case = app_commands.Group(name="case", description="View and manage moderation cases.")

    @case.command(name="view", description="View a moderation case.")
    @app_commands.autocomplete(case=case_autocomplete)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case_view(self, interaction: discord.Interaction, case: str):
        case_data = await fetch_case(str(interaction.guild.id), int(case))
        if case_data is None:
            await interaction.response.send_message("Case not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_case_embed(case_data, interaction.guild),
            ephemeral=True,
        )

    @case.command(name="edit", description="Edit a moderation case reason.")
    @app_commands.autocomplete(case=case_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def case_edit(
        self,
        interaction: discord.Interaction,
        case: str,
        reason: app_commands.Range[str, 1, 900],
    ):
        updated = await edit_case_reason(interaction, int(case), reason)
        if not updated:
            await interaction.response.send_message("Case not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=ok_embed(f"Case #{case} updated."),
            ephemeral=True,
        )

    @case.command(name="delete", description="Delete a moderation case.")
    @app_commands.autocomplete(case=case_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def case_delete(self, interaction: discord.Interaction, case: str):
        case_data = await fetch_case(str(interaction.guild.id), int(case))
        if case_data is None:
            await interaction.response.send_message("Case not found.", ephemeral=True)
            return

        if case_data["log_channel_id"] and case_data["log_message_id"]:
            channel = interaction.guild.get_channel(int(case_data["log_channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(case_data["log_message_id"]))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM mod_cases WHERE guild_id = ? AND case_id = ?",
                (str(interaction.guild.id), case_data["case_id"]),
            )
            await db.commit()

        embed = base_embed(f"🗑️ Case #{case_data['case_id']} Deleted", "warning")
        embed.add_field(name="Action", value=case_data["action"], inline=True)
        embed.add_field(name="Target", value=target_mention(interaction.guild, case_data["target_id"]), inline=True)
        await log_event(interaction.guild, "mod_case_update", embed)
        await interaction.response.send_message(
            embed=ok_embed(f"Case #{case_data['case_id']} deleted."),
            ephemeral=True,
        )

    @app_commands.command(name="reason", description="Quickly edit a moderation case reason.")
    @app_commands.autocomplete(case=case_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reason_command(
        self,
        interaction: discord.Interaction,
        case: str,
        reason: app_commands.Range[str, 1, 900],
    ):
        updated = await edit_case_reason(interaction, int(case), reason)
        if not updated:
            await interaction.response.send_message("Case not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=ok_embed(f"Case #{case} updated."),
            ephemeral=True,
        )

    # ---------- Moderator statistics ----------

    @app_commands.command(name="modstats", description="View server or member moderation statistics.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def modstats(self, interaction: discord.Interaction,
                       member: Optional[discord.Member] = None):
        guild_id = str(interaction.guild.id)
        params = [guild_id]
        actor_clause = ""
        if member is not None:
            actor_clause = " AND actor_id = ?"
            params.append(str(member.id))

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                f"SELECT action, COUNT(*) FROM mod_cases WHERE guild_id = ?{actor_clause} "
                "GROUP BY action ORDER BY COUNT(*) DESC",
                params,
            ) as cursor:
                action_counts = dict(await cursor.fetchall())
            async with db.execute(
                f"SELECT created_at FROM mod_cases WHERE guild_id = ?{actor_clause}",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            async with db.execute(
                "SELECT actor_id, COUNT(*) FROM mod_cases WHERE guild_id = ? "
                "GROUP BY actor_id ORDER BY COUNT(*) DESC LIMIT 3",
                (guild_id,),
            ) as cursor:
                top_actors = await cursor.fetchall()

        now = time.time()
        last_7 = last_30 = 0
        for (created_at,) in rows:
            try:
                timestamp = datetime.datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                continue
            if now - timestamp <= 7 * 86400:
                last_7 += 1
            if now - timestamp <= 30 * 86400:
                last_30 += 1

        title = f"🛡️ Mod Stats — {member.display_name}" if member else "🛡️ Server Mod Stats"
        embed = base_embed(title, "primary")
        embed.add_field(name="Total Cases", value=str(len(rows)), inline=True)
        embed.add_field(name="Last 7 Days", value=str(last_7), inline=True)
        embed.add_field(name="Last 30 Days", value=str(last_30), inline=True)

        if action_counts:
            lines = [f"**{action}:** {count}" for action, count in action_counts.items()]
            embed.add_field(name="Actions", value=truncate("\n".join(lines), 1024), inline=False)

        if member is None and top_actors:
            lines = []
            for actor_id, count in top_actors:
                actor = interaction.guild.get_member(int(actor_id))
                lines.append(f"{actor.mention if actor else f'<@{actor_id}>'}: {count} cases")
            embed.add_field(name="Top Moderators", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Role maintenance ----------

    rolemaintain = app_commands.Group(
        name="rolemaintain",
        description="Manage automatic member role restoration.",
    )

    @rolemaintain.command(name="enable", description="Enable role snapshots before kicks and bans.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rolemaintain_enable(self, interaction: discord.Interaction, enabled: bool):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO role_maintenance_settings (guild_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
                (str(interaction.guild.id), int(enabled)),
            )
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed(f"Role maintenance is now {'enabled' if enabled else 'disabled'}."),
            ephemeral=True,
        )

    @rolemaintain.command(name="ignore", description="Never restore a specific role.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rolemaintain_ignore(self, interaction: discord.Interaction, role: discord.Role):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR IGNORE INTO role_maintenance_ignored_roles (guild_id, role_id) VALUES (?, ?)",
                (str(interaction.guild.id), str(role.id)),
            )
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed(f"{role.mention} will not be saved or restored."),
            ephemeral=True,
        )

    @rolemaintain.command(name="unignore", description="Allow a role to be restored again.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rolemaintain_unignore(self, interaction: discord.Interaction, role: discord.Role):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM role_maintenance_ignored_roles WHERE guild_id = ? AND role_id = ?",
                (str(interaction.guild.id), str(role.id)),
            )
            await db.commit()
        await interaction.response.send_message(
            embed=ok_embed(f"{role.mention} can be saved and restored again."),
            ephemeral=True,
        )

    @rolemaintain.command(name="status", description="View role-maintenance settings and pending snapshots.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rolemaintain_status(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        enabled = await get_role_maintenance_enabled(guild_id)
        ignored = await get_ignored_maintenance_roles(guild_id)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM role_snapshots WHERE guild_id = ? AND active = 1",
                (guild_id,),
            ) as cursor:
                (pending,) = await cursor.fetchone()

        embed = base_embed("🧰 Role Maintenance", "primary")
        embed.add_field(name="Enabled", value="Yes" if enabled else "No", inline=True)
        embed.add_field(name="Pending Snapshots", value=str(pending), inline=True)
        embed.add_field(name="Ignored Roles", value=str(len(ignored)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rolemaintain.command(name="restore", description="Manually restore a member's saved roles.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rolemaintain_restore(self, interaction: discord.Interaction,
                                   member: discord.Member):
        restored, skipped = await restore_snapshot_roles(
            interaction.guild,
            member,
            interaction.user,
        )
        await interaction.response.send_message(
            embed=ok_embed(
                f"Restored {restored} role(s) for {member.mention}. "
                f"Skipped {skipped}."
            ),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if not await get_role_maintenance_enabled(str(guild.id)):
            return
        snapshot = await fetch_role_snapshot(str(guild.id), str(member.id))
        if snapshot is None or not snapshot["active"]:
            return
        await restore_snapshot_roles(guild, member)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModWorkflows(bot))
