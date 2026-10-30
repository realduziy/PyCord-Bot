import datetime
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import DB_FILE, log_event
from ui_common import base_embed, ok_embed, truncate


STATUS_META = {
    "open": ("🟢", "Open"),
    "considering": ("🟡", "Under Review"),
    "approved": ("🟣", "Approved"),
    "denied": ("🔴", "Denied"),
    "implemented": ("🟢", "Implemented"),
}


async def get_suggestion_channel_id(guild_id: str) -> str | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT channel_id FROM suggestion_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def set_suggestion_channel_id(guild_id: str, channel_id: str | None):
    async with aiosqlite.connect(DB_FILE) as db:
        if channel_id is None:
            await db.execute(
                "DELETE FROM suggestion_settings WHERE guild_id = ?",
                (guild_id,),
            )
        else:
            await db.execute(
                "INSERT INTO suggestion_settings (guild_id, channel_id) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id",
                (guild_id, channel_id),
            )
        await db.commit()


async def fetch_suggestion(suggestion_id: int) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT * FROM suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return None

    keys = [
        "suggestion_id", "guild_id", "channel_id", "message_id", "author_id",
        "author_name", "title", "content", "anonymous", "status", "created_at",
        "decided_by", "decided_at",
    ]
    return dict(zip(keys, row))


async def fetch_vote_counts(suggestion_id: int) -> tuple[int, int]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT vote, COUNT(*) FROM suggestion_votes "
            "WHERE suggestion_id = ? GROUP BY vote",
            (suggestion_id,),
        ) as cursor:
            counts = {vote: total for vote, total in await cursor.fetchall()}

    return counts.get(1, 0), counts.get(-1, 0)


async def build_suggestion_embed(suggestion: dict) -> discord.Embed:
    emoji, status_label = STATUS_META.get(
        suggestion["status"],
        ("⚪", suggestion["status"].title()),
    )
    upvotes, downvotes = await fetch_vote_counts(suggestion["suggestion_id"])
    score = upvotes - downvotes

    embed = base_embed(
        f"{emoji} {suggestion['title']}",
        "primary" if suggestion["status"] == "open" else "neutral",
        truncate(suggestion["content"], 3500),
    )
    embed.add_field(name="Status", value=status_label, inline=True)
    embed.add_field(name="Score", value=str(score), inline=True)
    embed.add_field(name="Votes", value=f"👍 {upvotes} · 👎 {downvotes}", inline=True)

    if suggestion["anonymous"]:
        embed.add_field(name="Submitted by", value="Anonymous", inline=True)
    else:
        embed.add_field(
            name="Submitted by",
            value=f"<@{suggestion['author_id']}>",
            inline=True,
        )

    embed.add_field(
        name="Created",
        value=f"<t:{int(datetime.datetime.fromisoformat(suggestion['created_at']).timestamp())}:R>",
        inline=True,
    )
    embed.set_footer(text=f"Suggestion #{suggestion['suggestion_id']}")
    return embed


class SuggestionVoteButton(discord.ui.Button):
    def __init__(self, suggestion_id: int, vote: int, *, disabled: bool = False):
        super().__init__(
            label="Upvote" if vote == 1 else "Downvote",
            emoji="👍" if vote == 1 else "👎",
            style=discord.ButtonStyle.success if vote == 1 else discord.ButtonStyle.danger,
            custom_id=f"suggestion_vote:{suggestion_id}:{vote}",
            row=0,
            disabled=disabled,
        )
        self.suggestion_id = suggestion_id
        self.vote = vote

    async def callback(self, interaction: discord.Interaction):
        suggestion = await fetch_suggestion(self.suggestion_id)
        if suggestion is None:
            await interaction.response.send_message("That suggestion no longer exists.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT vote FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
                (self.suggestion_id, str(interaction.user.id)),
            ) as cursor:
                existing = await cursor.fetchone()

            if existing and existing[0] == self.vote:
                await db.execute(
                    "DELETE FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
                    (self.suggestion_id, str(interaction.user.id)),
                )
                action = "Vote removed"
            else:
                await db.execute(
                    "INSERT INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?, ?, ?) "
                    "ON CONFLICT(suggestion_id, user_id) DO UPDATE SET vote = excluded.vote",
                    (self.suggestion_id, str(interaction.user.id), self.vote),
                )
                action = "Vote updated"

            await db.commit()

        await interaction.response.edit_message(
            embed=await build_suggestion_embed(suggestion),
            view=SuggestionView(
                self.suggestion_id,
                suggestion["status"],
            ),
        )

        # Keep the response uncluttered while still acknowledging privately.
        await interaction.followup.send(action, ephemeral=True)


class SuggestionActionButton(discord.ui.Button):
    def __init__(self, suggestion_id: int, action: str, row: int):
        labels = {
            "considering": ("Consider", "🟡", discord.ButtonStyle.secondary),
            "approved": ("Approve", "🟣", discord.ButtonStyle.success),
            "denied": ("Deny", "🔴", discord.ButtonStyle.danger),
            "implemented": ("Implemented", "✅", discord.ButtonStyle.success),
            "delete": ("Delete", "🗑️", discord.ButtonStyle.danger),
        }
        label, emoji, style = labels[action]
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"suggestion_action:{suggestion_id}:{action}",
            row=row,
        )
        self.suggestion_id = suggestion_id
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server permission to update suggestions.",
                ephemeral=True,
            )
            return

        suggestion = await fetch_suggestion(self.suggestion_id)
        if suggestion is None:
            await interaction.response.send_message("That suggestion no longer exists.", ephemeral=True)
            return

        if self.action == "delete":
            channel = interaction.guild.get_channel(int(suggestion["channel_id"]))
            if isinstance(channel, discord.TextChannel) and suggestion["message_id"]:
                try:
                    message = await channel.fetch_message(int(suggestion["message_id"]))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    "DELETE FROM suggestion_votes WHERE suggestion_id = ?",
                    (self.suggestion_id,),
                )
                await db.execute(
                    "DELETE FROM suggestions WHERE suggestion_id = ?",
                    (self.suggestion_id,),
                )
                await db.commit()

            await interaction.response.send_message(
                embed=ok_embed(f"Suggestion #{self.suggestion_id} was deleted."),
                ephemeral=True,
            )
            await log_event(
                interaction.guild,
                "suggestion_update",
                interaction.user,
                None,
                reason=f"Deleted suggestion #{self.suggestion_id}",
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE suggestions SET status = ?, decided_by = ?, decided_at = ? "
                "WHERE suggestion_id = ?",
                (
                    self.action,
                    str(interaction.user.id),
                    str(discord.utils.utcnow()),
                    self.suggestion_id,
                ),
            )
            await db.commit()

        suggestion = await fetch_suggestion(self.suggestion_id)
        await interaction.response.edit_message(
            embed=await build_suggestion_embed(suggestion),
            view=SuggestionView(self.suggestion_id, suggestion["status"]),
        )
        await interaction.followup.send("Suggestion updated.", ephemeral=True)
        await log_event(
            interaction.guild,
            "suggestion_update",
            interaction.user,
            None,
            reason=(
                f"Suggestion #{self.suggestion_id} marked as "
                f"{STATUS_META.get(self.action, ('', self.action))[1]}"
            ),
        )


class SuggestionView(discord.ui.View):
    def __init__(self, suggestion_id: int, status: str):
        # Suggestion messages stay interactive after restarts, including
        # closed suggestions so the delete action remains available to staff.
        super().__init__(timeout=None)

        voting_open = status in {"open", "considering"}
        self.add_item(SuggestionVoteButton(suggestion_id, 1, disabled=not voting_open))
        self.add_item(SuggestionVoteButton(suggestion_id, -1, disabled=not voting_open))

        if status == "open":
            self.add_item(SuggestionActionButton(suggestion_id, "considering", row=1))
        if status in {"open", "considering"}:
            self.add_item(SuggestionActionButton(suggestion_id, "approved", row=1))
            self.add_item(SuggestionActionButton(suggestion_id, "denied", row=1))
        if status in {"open", "considering", "approved"}:
            self.add_item(SuggestionActionButton(suggestion_id, "implemented", row=2))
        self.add_item(SuggestionActionButton(suggestion_id, "delete", row=2))


async def get_persistent_suggestion_views() -> list[SuggestionView]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT suggestion_id, status FROM suggestions"
        ) as cursor:
            rows = await cursor.fetchall()
    return [SuggestionView(suggestion_id, status) for suggestion_id, status in rows]


class Suggestions(commands.Cog):
    """Persistent community suggestion workflow."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="suggest", description="Submit a server suggestion.")
    async def suggest(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 120],
        content: app_commands.Range[str, 1, 1800],
        anonymous: bool = False,
    ):
        guild_id = str(interaction.guild.id)
        channel_id = await get_suggestion_channel_id(guild_id)
        if channel_id is None:
            await interaction.response.send_message(
                "Suggestions aren't configured yet. Ask staff to run `/suggestion channel`.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The configured suggestion channel no longer exists.",
                ephemeral=True,
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO suggestions (guild_id, channel_id, author_id, author_name, "
                "title, content, anonymous, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id,
                    channel_id,
                    str(interaction.user.id),
                    str(interaction.user),
                    title.strip(),
                    content.strip(),
                    int(anonymous),
                    str(discord.utils.utcnow()),
                ),
            )
            await db.commit()
            suggestion_id = cursor.lastrowid

        suggestion = await fetch_suggestion(suggestion_id)
        view = SuggestionView(suggestion_id, "open")
        self.bot.add_view(view)
        try:
            message = await channel.send(
                embed=await build_suggestion_embed(suggestion),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    "DELETE FROM suggestions WHERE suggestion_id = ?",
                    (suggestion_id,),
                )
                await db.commit()
            await interaction.response.send_message(
                "I couldn't send the suggestion to the configured channel.",
                ephemeral=True,
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE suggestions SET message_id = ? WHERE suggestion_id = ?",
                (str(message.id), suggestion_id),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Suggestion #{suggestion_id} submitted to {channel.mention}."),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "suggestion_open",
            interaction.user,
            channel,
            reason=title.strip(),
            extra=truncate(content.strip(), 500),
        )

    suggestion = app_commands.Group(
        name="suggestion",
        description="Configure and inspect suggestions.",
    )

    @suggestion.command(name="channel", description="Set the suggestion channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def suggestion_channel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if channel is None:
            await set_suggestion_channel_id(str(interaction.guild.id), None)
            await interaction.response.send_message(
                embed=ok_embed("Suggestion channel cleared."),
                ephemeral=True,
            )
            return

        await set_suggestion_channel_id(str(interaction.guild.id), str(channel.id))
        await interaction.response.send_message(
            embed=ok_embed(f"Suggestion channel set to {channel.mention}."),
            ephemeral=True,
        )

    @suggestion.command(name="stats", description="Show suggestion statistics.")
    async def suggestion_stats(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM suggestions WHERE guild_id = ? GROUP BY status",
                (str(interaction.guild.id),),
            ) as cursor:
                status_counts = dict(await cursor.fetchall())
            async with db.execute(
                "SELECT COUNT(*) FROM suggestion_votes sv "
                "JOIN suggestions s ON s.suggestion_id = sv.suggestion_id "
                "WHERE s.guild_id = ?",
                (str(interaction.guild.id),),
            ) as cursor:
                (vote_count,) = await cursor.fetchone()

        embed = base_embed("💡 Suggestion Stats", "primary")
        total = sum(status_counts.values())
        embed.add_field(name="Total", value=str(total), inline=True)
        embed.add_field(name="Total Votes", value=str(vote_count), inline=True)

        for status in ("open", "considering", "approved", "denied", "implemented"):
            emoji, label = STATUS_META[status]
            embed.add_field(
                name=label,
                value=f"{emoji} {status_counts.get(status, 0)}",
                inline=True,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
