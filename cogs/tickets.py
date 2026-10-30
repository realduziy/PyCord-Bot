import asyncio
import datetime
import io
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, Modal, Select, TextInput, View

from cog_utils import (
    DB_FILE,
    TicketConfig,
    ensure_guild_config,
    get_blacklisted_users,
    increment_claim,
    increment_closed,
    is_blacklisted,
    log_event,
    set_blacklisted,
)

INACTIVITY_DAYS = 3  # auto-close tickets after this many days without messages
TRANSCRIPT_MESSAGE_LIMIT = 5000  # protects memory usage on exceptionally large tickets

# In-memory cache of open ticket channel IDs so on_message doesn't hit the DB
# for every single message in every guild. Kept in sync on open/close.
_open_ticket_channels: set[int] = set()

NO_STAFF_EMBED = discord.Embed(
    description="\u23f3 No support staff are currently online. Your ticket will be answered as soon as someone is available \u2014 please be patient!",
    color=0xfee75c
)

CATEGORY_TITLES = {
    "general": "\u2753 General Support",
    "report": "\U0001f6e1\ufe0f Report a User",
    "suggestion": "\U0001f4a1 Suggestion",
}


def can_manage_tickets(user: discord.Member, support_role: discord.Role | None) -> bool:
    """Support staff check shared by Claim and Close."""
    return user.guild_permissions.manage_channels or (support_role is not None and support_role in user.roles)


async def is_support_check(interaction: discord.Interaction) -> bool:
    """app_commands.check predicate for staff-only ticket commands."""
    cfg = await TicketConfig.fetch(str(interaction.guild.id))
    role = interaction.guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None
    return can_manage_tickets(interaction.user, role)


def ticket_panel_embed() -> discord.Embed:
    """The public 'open a ticket' panel — one builder used everywhere."""
    return discord.Embed(
        title="\U0001f4e9 Support & Help Desk",
        description=(
            "Select an option from the menu below to open a private ticket.\n\n"
            "**Available Categories:**\n"
            "\u2753 **General Support** \u2013 Help with questions or server features\n"
            "\U0001f6e1\ufe0f **Report a User** \u2013 Report rule violations or player behavior\n"
            "\U0001f4a1 **Suggestions** \u2013 Share ideas and feedback for the server"
        ),
        color=0x5865f2
    ).set_footer(text="Please refrain from opening tickets without a reason.")


def ticket_log_embed(title: str, color: int) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


async def build_transcript(channel: discord.TextChannel) -> tuple[str, int]:
    """Build the plain-text message log: one '[date] author: content' per line.

    Returns (text, message_count) — the count excludes the truncation notice.
    """
    rows = []
    transcript_truncated = False

    async for message in channel.history(limit=TRANSCRIPT_MESSAGE_LIMIT + 1,
                                         oldest_first=True):
        if len(rows) >= TRANSCRIPT_MESSAGE_LIMIT:
            transcript_truncated = True
            break

        if message.author == channel.guild.me:
            continue
        content = message.content or ""
        if message.attachments:
            content = (content + " " if content else "") + " ".join(a.url for a in message.attachments)
        if not content:
            content = "[No Text Content]"
        rows.append(
            f'[{message.created_at.strftime("%Y-%m-%d %H:%M")}] '
            f'{message.author}: {content}'
        )

    message_count = len(rows)

    if transcript_truncated:
        rows.append(
            f'[Transcript] System: Transcript truncated after '
            f'{TRANSCRIPT_MESSAGE_LIMIT:,} messages.'
        )

    return "\n".join(rows), message_count


def build_transcript_embed(channel: discord.TextChannel, ticket_info: aiosqlite.Row,
                           reason: str, closer: str, message_count: int) -> discord.Embed:
    """Summary embed sent above the transcript file."""
    embed = discord.Embed(
        title=f"🎫 Ticket Transcript — #{channel.name}",
        color=0x5865f2,
    )
    embed.add_field(
        name="Opened by",
        value=f"{ticket_info['user_name']} (<@{ticket_info['user_id']}>)",
        inline=False,
    )
    embed.add_field(
        name="Claimed by",
        value=f"<@{ticket_info['claimed_by']}>" if ticket_info["claimed_by"] else "Unclaimed",
        inline=False,
    )
    embed.add_field(name="Closed by", value=closer, inline=False)
    embed.add_field(name="Reason", value=reason or "No reason provided.", inline=False)
    embed.add_field(name="Created", value=ticket_info["created_at"], inline=False)
    embed.add_field(name="Messages", value=str(message_count), inline=False)
    return embed


async def perform_close(channel: discord.TextChannel, reason: str, closer: str,
                        interaction: discord.Interaction | None = None) -> None:
    """Shared close flow: transcript embed + .txt -> transcripts channel + opener DM, stats, mod log, delete row + channel."""
    guild = channel.guild
    guild_id = str(guild.id)
    _open_ticket_channels.discard(channel.id)

    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tickets WHERE channel_id = ?", (str(channel.id),)) as cursor:
            ticket_info = await cursor.fetchone()

    if not ticket_info:
        if interaction and interaction.response.is_done():
            await interaction.followup.send("This channel is not an active ticket in the database.",
                                            ephemeral=True)
        return

    cfg = await TicketConfig.fetch(guild_id)
    transcripts_channel = guild.get_channel(cfg.transcripts_channel) if cfg.transcripts_channel else None

    transcript_text, message_count = await build_transcript(channel)
    transcript_embed = build_transcript_embed(channel, ticket_info, reason,
                                              closer, message_count)
    filename = f"transcript-{channel.name}.txt"

    if transcripts_channel:
        try:
            await transcripts_channel.send(embed=transcript_embed)
            await transcripts_channel.send(
                file=discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=filename)
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    try:
        opener = guild.get_member(int(ticket_info["user_id"]))
        if opener:
            await opener.send(embed=transcript_embed)
            await opener.send(
                file=discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=filename)
            )
    except (discord.Forbidden, discord.NotFound, ValueError):
        pass

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM tickets WHERE channel_id = ?", (str(channel.id),))
        await db.commit()
    await increment_closed(guild_id)

    log_embed = ticket_log_embed("\U0001f512 Ticket Closed", 0xed4245)
    log_embed.add_field(name="Channel", value=channel.name, inline=True)
    log_embed.add_field(name="Closed by", value=closer, inline=True)
    log_embed.add_field(name="Reason", value=reason, inline=True)
    log_embed.add_field(name="Opened by", value=f"{ticket_info['user_name']} (<@{ticket_info['user_id']}>)", inline=True)
    await log_event(guild, "ticket_close", log_embed)

    if interaction:
        await interaction.followup.send("Ticket closed \u2014 transcript saved. Deleting channel in 3 seconds...",
                                        ephemeral=True)
    await asyncio.sleep(3)
    try:
        await channel.delete()
    except discord.NotFound:
        pass


# --- UI Views for Tickets ---
class TicketCategorySelect(Select):
    def __init__(self):
        select_options = [
            discord.SelectOption(
                label="General Support",
                description="Questions or general help with server features",
                emoji="\u2753",
                value="general"
            ),
            discord.SelectOption(
                label="Report a User",
                description="Report a player breaking server rules or causing issues",
                emoji="\U0001f6e1\ufe0f",
                value="report"
            ),
            discord.SelectOption(
                label="Suggestions",
                description="Submit an idea, feedback, or suggestion for the server",
                emoji="\U0001f4a1",
                value="suggestion"
            ),
        ]
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            custom_id="ticket_category_select",
            options=select_options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Reset the select menu back to its placeholder immediately, so it
        # never stays stuck regardless of which path the callback takes
        try:
            await interaction.message.edit(view=TicketPanelView())
        except (discord.NotFound, discord.Forbidden):
            pass

        guild = interaction.guild
        guild_id = str(guild.id)
        selected_category = self.values[0]

        await ensure_guild_config(guild_id)

        if await is_blacklisted(guild_id, str(interaction.user.id)):
            await interaction.followup.send("You are blacklisted from opening tickets.", ephemeral=True)
            return

        cfg = await TicketConfig.fetch(guild_id)

        if not cfg.ticket_category:
            await interaction.followup.send(
                "Ticket category is not configured in this server. Staff should run /setup.", ephemeral=True)
            return

        category = guild.get_channel(cfg.ticket_category)
        support_role = guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None

        if not category:
            await interaction.followup.send("Configured ticket category channel was not found.", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True)

        channel_name = f"{selected_category}-{interaction.user.name}"
        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I couldn't create the ticket channel. Check my channel permissions and try again.",
                ephemeral=True,
            )
            return

        # Use an immediate transaction so two simultaneous menu submissions
        # cannot both pass the "one open ticket" check. If another ticket is
        # registered first, discard the channel we just created.
        registered = False
        db = await aiosqlite.connect(DB_FILE)
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND user_id = ?",
                (guild_id, str(interaction.user.id)),
            ) as cursor:
                (open_count,) = await cursor.fetchone()

            if open_count >= 1:
                await db.rollback()
                await interaction.followup.send(
                    "You already have an open ticket. Please close it before opening a new one.",
                    ephemeral=True,
                )
            else:
                await db.execute(
                    "INSERT INTO tickets (channel_id, guild_id, user_id, user_name, created_at, claimed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(ticket_channel.id),
                        guild_id,
                        str(interaction.user.id),
                        str(interaction.user),
                        str(ticket_channel.created_at),
                        None,
                    ),
                )
                await db.commit()
                registered = True
        except aiosqlite.Error:
            await db.rollback()
            await interaction.followup.send(
                "The ticket was created, but I couldn't register it. Please ask staff to check the database.",
                ephemeral=True,
            )
        finally:
            await db.close()

        if not registered:
            try:
                await ticket_channel.delete(reason="Duplicate ticket registration prevented")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return

        _open_ticket_channels.add(ticket_channel.id)

        title = CATEGORY_TITLES.get(selected_category, "Support")

        embed = discord.Embed(
            title=f"Ticket: {title}",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "Please detail your request below. "
                "Staff will review your message shortly."
            ),
            color=0x2b2d31
        )
        embed.add_field(name="Opened By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Category", value=title, inline=True)
        embed.add_field(name="Claimed By", value="*Unclaimed*", inline=True)
        embed.set_footer(text="Use the controls below to manage or close this ticket.")

        mention_text = support_role.mention if support_role else ""
        await ticket_channel.send(content=f"{interaction.user.mention} {mention_text}", embed=embed,
                                  view=TicketControlsView())
        await interaction.followup.send(f"Your ticket has been created: {ticket_channel.mention}", ephemeral=True)

        open_log = ticket_log_embed("\U0001f4e9 Ticket Opened", 0x57f287)
        open_log.add_field(name="User", value=f"{interaction.user} (<@{interaction.user.id}>)", inline=True)
        open_log.add_field(name="Category", value=title, inline=True)
        open_log.add_field(name="Channel", value=ticket_channel.mention, inline=True)
        await log_event(guild, "ticket_open", open_log)

        if support_role:
            try:
                staff_online = any(
                    m.status != discord.Status.offline and not m.bot
                    for m in support_role.members
                )
            except Exception:
                staff_online = True
            if not staff_online:
                await ticket_channel.send(embed=NO_STAFF_EMBED)


class TicketPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())


class CloseReasonModal(Modal):
    reason = TextInput(
        label="Reason for closing",
        style=discord.TextStyle.paragraph,
        placeholder="Issue resolved, user banned, etc.",
        default="No reason provided.",
        required=False,
        max_length=500
    )

    def __init__(self):
        super().__init__(title="Close Ticket")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Remove the control buttons so nobody can click them during the close
        if interaction.message:
            try:
                await interaction.message.edit(view=None)
            except (discord.NotFound, discord.Forbidden):
                pass
        await perform_close(
            interaction.channel,
            reason=self.reason.value or "No reason provided.",
            closer=str(interaction.user),
            interaction=interaction
        )


class TicketControlsView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.green, emoji="\u270b", custom_id="claim_ticket_btn")
    async def claim_ticket(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        guild_id = str(guild.id)
        channel = interaction.channel

        cfg = await TicketConfig.fetch(guild_id)
        support_role = guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None

        if not can_manage_tickets(interaction.user, support_role):
            await interaction.followup.send("Only support staff can claim tickets.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT claimed_by FROM tickets WHERE channel_id = ?", (str(channel.id),)) as cursor:
                ticket_info = await cursor.fetchone()

        if not ticket_info:
            await interaction.followup.send("This ticket is not active in the database.", ephemeral=True)
            return

        if ticket_info["claimed_by"]:
            await interaction.followup.send(
                f"This ticket has already been claimed by <@{ticket_info['claimed_by']}>.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE tickets SET claimed_by = ? WHERE channel_id = ?",
                             (str(interaction.user.id), str(channel.id)))
            await db.commit()
        await increment_claim(guild_id, str(interaction.user.id))

        await channel.set_permissions(interaction.user, view_channel=True, send_messages=True, manage_messages=True)

        embed = discord.Embed(
            description=f"\U0001f64b\u200d\u2642\ufe0f Ticket has been claimed by {interaction.user.mention}.",
            color=0x57f287
        )
        await channel.send(embed=embed)
        await interaction.followup.send("You have successfully claimed this ticket.", ephemeral=True)

        claim_log = ticket_log_embed("\u270b Ticket Claimed", 0x5865f2)
        claim_log.add_field(name="Channel", value=channel.name, inline=True)
        claim_log.add_field(name="Claimed by", value=f"{interaction.user} (<@{interaction.user.id}>)", inline=True)
        await log_event(guild, "ticket_claim", claim_log)

    @discord.ui.button(label="Request Close", style=discord.ButtonStyle.secondary,
                       emoji="\U0001f64b\u200d\u2642\ufe0f", custom_id="request_close_btn")
    async def request_close(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        guild_id = str(guild.id)

        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id FROM tickets WHERE channel_id = ?", (str(channel.id),)) as cursor:
                ticket_info = await cursor.fetchone()

        if not ticket_info:
            await interaction.followup.send("This ticket is not active in the database.", ephemeral=True)
            return

        if str(interaction.user.id) != ticket_info["user_id"]:
            await interaction.followup.send("Only the user who created this ticket can request to close it.",
                                            ephemeral=True)
            return

        cfg = await TicketConfig.fetch(guild_id)
        support_role = guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None
        mention_text = support_role.mention if support_role else "Staff"

        embed = discord.Embed(
            title="\U0001f514 Request to Close Ticket",
            description=(
                f"{interaction.user.mention} has requested to close this ticket as they no longer need assistance.\n\n"
                f"{mention_text}, please review and click **Close Ticket** below to finalize."
            ),
            color=0xfee75c
        )
        await channel.send(content=mention_text, embed=embed)
        await interaction.followup.send("Your close request has been sent to the staff team.", ephemeral=True)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, emoji="\U0001f512", custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        support_role = interaction.guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None

        if not can_manage_tickets(interaction.user, support_role):
            await interaction.response.send_message("Only support staff can close tickets.", ephemeral=True)
            return

        await interaction.response.send_modal(CloseReasonModal())


async def post_ticket_panel(guild: discord.Guild, fallback: discord.abc.GuildChannel):
    """Send the ticket panel to the configured panel channel (or `fallback`).

    Shared by /sendticketpanel and the /setup hub so both behave identically.
    Returns the channel the panel was sent to.
    """
    cfg = await TicketConfig.fetch(str(guild.id))
    panel_channel = guild.get_channel(cfg.ticket_panel_channel) if cfg.ticket_panel_channel else None
    if panel_channel is None:
        panel_channel = fallback
    await panel_channel.send(embed=ticket_panel_embed(), view=TicketPanelView())
    return panel_channel


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_close_loop.start()

    async def cog_load(self):
        # Populate the in-memory ticket channel cache from the DB on startup
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT channel_id FROM tickets") as cursor:
                rows = await cursor.fetchall()
        _open_ticket_channels.update(int(row[0]) for row in rows)

    def cog_unload(self):
        self.auto_close_loop.cancel()

    # --- Auto-claim: first support message in an unclaimed ticket claims it ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        if message.channel.id not in _open_ticket_channels:
            return

        guild = message.guild
        guild_id = str(guild.id)

        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT t.claimed_by, g.ticket_support_role "
                "FROM tickets t JOIN guild_settings g ON t.guild_id = g.guild_id "
                "WHERE t.channel_id = ?", (str(message.channel.id),)
            ) as cursor:
                row = await cursor.fetchone()

        if not row or row["claimed_by"]:
            return

        member = message.author
        support_role = guild.get_role(row["ticket_support_role"]) if row["ticket_support_role"] else None
        if not isinstance(member, discord.Member) or not can_manage_tickets(member, support_role):
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE tickets SET claimed_by = ? WHERE channel_id = ?",
                             (str(member.id), str(message.channel.id)))
            await db.commit()
        await increment_claim(guild_id, str(member.id))

        embed = discord.Embed(
            description=f"\u270b Ticket auto-claimed by {member.mention} (first staff reply).",
            color=0x57f287
        )
        await message.channel.send(embed=embed)

    # --- Inactivity auto-close ---
    @tasks.loop(hours=1)
    async def auto_close_loop(self):
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=INACTIVITY_DAYS)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT channel_id, guild_id, created_at FROM tickets") as cursor:
                rows = await cursor.fetchall()

        for channel_id, guild_id, created_at in rows:
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue
            channel = guild.get_channel(int(channel_id))
            if not channel:
                _open_ticket_channels.discard(int(channel_id))
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM tickets WHERE channel_id = ?", (channel_id,))
                    await db.commit()
                continue

            last_active = None
            async for msg in channel.history(limit=1):
                last_active = msg.created_at
            if last_active is None:
                try:
                    last_active = datetime.datetime.fromisoformat(created_at)
                    if last_active.tzinfo is None:
                        last_active = last_active.replace(tzinfo=datetime.timezone.utc)
                except (ValueError, TypeError):
                    continue

            if last_active < cutoff:
                await perform_close(
                    channel,
                    reason=f"Auto-closed due to {INACTIVITY_DAYS} days of inactivity.",
                    closer="Auto-close (inactivity)"
                )

    @auto_close_loop.before_loop
    async def before_auto_close(self):
        await self.bot.wait_until_ready()

    # --- Commands ---
    @app_commands.command(name="closeticket", description="Close a ticket channel (defaults to the current channel).")
    @app_commands.describe(channel="The ticket channel to close (defaults to this channel)", reason="Reason for closing")
    @app_commands.check(is_support_check)
    async def closeticket(self, interaction: discord.Interaction,
                          channel: Optional[discord.TextChannel] = None,
                          reason: str = "No reason provided."):
        target = channel or interaction.channel
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT 1 FROM tickets WHERE channel_id = ?", (str(target.id),)) as cursor:
                exists = await cursor.fetchone()

        if not exists:
            await interaction.response.send_message(f"{target.mention} is not an active ticket.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await perform_close(target, reason, str(interaction.user), interaction=interaction)

    @app_commands.command(name="ticketadd", description="Add a user to this ticket channel.")
    @app_commands.check(is_support_check)
    async def ticket_add(self, interaction: discord.Interaction, user: discord.User):
        if interaction.channel.id not in _open_ticket_channels:
            await interaction.response.send_message("This channel isn't an active ticket.", ephemeral=True)
            return
        member = interaction.guild.get_member(user.id)
        if member is None:
            await interaction.response.send_message(f"{user} is not in this server.", ephemeral=True)
            return
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, attach_files=True)
        await interaction.response.send_message(f"Added {member.mention} to this ticket.")

    @app_commands.command(name="ticketremove", description="Remove a user from this ticket channel.")
    @app_commands.check(is_support_check)
    async def ticket_remove(self, interaction: discord.Interaction, user: discord.User):
        if interaction.channel.id not in _open_ticket_channels:
            await interaction.response.send_message("This channel isn't an active ticket.", ephemeral=True)
            return
        member = interaction.guild.get_member(user.id)
        if member is None:
            await interaction.response.send_message(f"{user} is not in this server.", ephemeral=True)
            return
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(f"Removed {member.mention} from this ticket.")

    @app_commands.command(name="ticketstats", description="Show ticket statistics for this server.")
    async def ticketstats(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ?", (guild_id,)) as cursor:
                (open_count,) = await cursor.fetchone()
            async with db.execute("SELECT closed_count FROM ticket_metrics WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                closed_count = row[0] if row else 0
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, claim_count FROM ticket_claims WHERE guild_id = ? "
                "ORDER BY claim_count DESC LIMIT 5",
                (guild_id,)
            ) as cursor:
                top_claimers = await cursor.fetchall()

        embed = discord.Embed(title="\U0001f4c8 Ticket Statistics", color=0x5865f2)
        embed.add_field(name="Open tickets", value=open_count, inline=True)
        embed.add_field(name="Total closed", value=closed_count, inline=True)
        if top_claimers:
            ranking = "\n".join(
                f"**{i + 1}.** <@{r['user_id']}> \u2014 {r['claim_count']} claims"
                for i, r in enumerate(top_claimers)
            )
            embed.add_field(name="Top claimers", value=ranking, inline=False)
        else:
            embed.add_field(name="Top claimers", value="No claims yet.", inline=False)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticketblacklist", description="Block, unblock, or list users blocked from opening tickets.")
    @app_commands.describe(action="Add, Remove, or List", user="The user to blacklist or unblacklist (not needed for List)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List", value="list"),
    ])
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticketblacklist(self, interaction: discord.Interaction,
                              action: app_commands.Choice[str], user: Optional[discord.User] = None):
        guild_id = str(interaction.guild.id)

        if action.value == "list":
            user_ids = await get_blacklisted_users(guild_id)
            if not user_ids:
                await interaction.response.send_message("No one is blacklisted from tickets.", ephemeral=True)
                return
            mentions = "\n".join(f"\u2022 <@{uid}> (`{uid}`)" for uid in user_ids[:25])
            embed = discord.Embed(title="\U0001f6ab Ticket Blacklist", description=mentions, color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if user is None:
            await interaction.response.send_message("Provide a user to add or remove.", ephemeral=True)
            return

        blocked = action.value == "add"
        await set_blacklisted(guild_id, str(user.id), blocked)
        if blocked:
            await interaction.response.send_message(f"\u274c {user.mention} is now blacklisted from opening tickets.",
                                                    ephemeral=True)
        else:
            await interaction.response.send_message(f"\u2705 {user.mention} can open tickets again.", ephemeral=True)

    @app_commands.command(name="sendticketpanel", description="Sends the formatted ticket creation panel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def sendticketpanel(self, interaction: discord.Interaction):
        await ensure_guild_config(str(interaction.guild.id))
        # post_ticket_panel falls back to this channel when none is configured
        # or when the configured channel has been deleted
        channel = await post_ticket_panel(interaction.guild, interaction.channel)
        await interaction.response.send_message(f"Ticket panel sent to {channel.mention}!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
