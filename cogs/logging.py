import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, ChannelSelect, Select

from cog_utils import (
    LOG_CATEGORIES,
    LOG_EVENT_CATEGORY,
    LOG_EVENTS,
    TicketConfig,
    consume_automod_deleted,
    get_channel_overrides,
    get_log_ignored,
    is_log_debug_enabled,
    is_log_event_enabled,
    log_event,
    set_channel_category_overrides,
    set_guild_setting,
    set_log_event,
    set_log_ignored,
)
from ui_common import EMOJI, PanelView, base_embed
from cogs._hub import BackToHubButton

# Console trace for message events — makes it obvious whether the bot is
# receiving deletes/edits at all (check the console when testing logging).
audit_log = logging.getLogger("duziy-bot.audit")

_EVENT_EMOJI = {
    "mod": "\U0001f6e1\ufe0f", "automod": "\U0001f6e1\ufe0f", "tickets": "\U0001f3ab",
    "verification": "\U0001f39f\ufe0f", "members": "\U0001f465", "channels": "\U0001f4e1",
    "roles": "\U0001f3ad", "messages": "\u270f\ufe0f", "server": "\U0001f3db\ufe0f",
    "lockdown": "\U0001f512", "voice": "\U0001f50a", "threads": "\U0001f9f5",
    "events": "\U0001f4c5", "stage": "\U0001f3a4", "applications": "\U0001f50c",
    "emojis": "\U0001f600", "stickers": "\U0001f5bc\ufe0f", "soundboard": "\U0001f514",
    "polls": "\U0001f4ca",
}


def _base(title: str, color: str) -> discord.Embed:
    # Single embed factory like every other cog — color names, not raw ints
    return base_embed(title, color)


def _content(value, limit=1024):
    if not value:
        return "*no text content*"
    return value[:limit]


def _channel_label(channel) -> str:
    kind = {discord.TextChannel: "Text", discord.VoiceChannel: "Voice",
            discord.CategoryChannel: "Category", discord.StageChannel: "Stage"}.get(
                type(channel), "Channel")
    return f"{kind} channel"


def _category_name(channel) -> str:
    return channel.category.name if getattr(channel, "category", None) else "None"


def _category_options():
    return [discord.SelectOption(label=name[:100], value=key,
                                 emoji=_EVENT_EMOJI.get(key, EMOJI["logs"]))
            for key, (name, _) in LOG_CATEGORIES.items()]


async def _status_embed(guild: discord.Guild) -> discord.Embed:
    """Overview: one line per category (all-on / partial / all-off)."""
    guild_id = str(guild.id)
    embed = base_embed(f"{EMOJI['logs']} Log Event Status", "primary")
    lines = []
    for key, (name, subs) in LOG_CATEGORIES.items():
        states = [await is_log_event_enabled(guild_id, k) for k in subs]
        if all(states):
            mark = EMOJI["ok"]
        elif not any(states):
            mark = EMOJI["no"]
        else:
            mark = EMOJI["warn"]  # partially enabled
        lines.append(f"{mark} {name}")
    embed.description = "\n".join(lines)
    ignored_ids = await get_log_ignored(guild_id)
    if ignored_ids:
        mentions = [f"<#{i}>" for i in ignored_ids]
        embed.add_field(name="Fully ignored channels", value=" ".join(mentions),
                        inline=False)
    else:
        embed.add_field(name="Fully ignored channels",
                        value="*none — everything is logged*", inline=False)
    embed.add_field(
        name="Fine-tuning",
        value="\u2022 Pick a **category** below to toggle its individual events "
              "(Created / Edited / Deleted / \u2026)\n"
              "\u2022 **Channel Overrides**: silence one category in one channel "
              "while the rest keeps logging\n"
              "\u2022 Channel picker: exclude channels from logging **entirely**",
        inline=False)
    return embed


async def _category_embed(guild: discord.Guild, category: str) -> discord.Embed:
    """Detail view: every sub-event of one category with its state."""
    name, subs = LOG_CATEGORIES[category]
    guild_id = str(guild.id)
    lines = []
    for key, label in subs.items():
        mark = EMOJI["ok"] if await is_log_event_enabled(guild_id, key) else EMOJI["no"]
        lines.append(f"{mark} {label}")
    embed = base_embed(f"{EMOJI['logs']} {name}", "primary")
    embed.description = "\n".join(lines)
    embed.add_field(name="How to use",
                    value="Pick the events to log from the menu below \u2014 it "
                          "saves automatically. Picking **none** turns the whole "
                          "category off.", inline=False)
    return embed


def _override_embed(channel, suppressed: list) -> discord.Embed:
    """What's silenced in one channel, grouped by category for readability."""
    mention = getattr(channel, "mention", f"#{channel.name}")
    embed = base_embed(f"{EMOJI['logs']} Overrides for {channel.name}", "primary")
    embed.description = (
        f"Events picked below are **silenced in {mention}** \u2014 every other "
        "event keeps logging normally there. This stacks with the global "
        "toggles: an event turned off globally never logs anywhere.")
    if suppressed:
        groups = {}
        for event in suppressed:
            groups.setdefault(LOG_EVENT_CATEGORY[event], []).append(LOG_EVENTS[event])
        value = "\n".join(f"**{LOG_CATEGORIES[cat][0]}:** " + ", ".join(labels)
                          for cat, labels in groups.items())
        embed.add_field(name="Currently silenced here", value=value[:1024],
                        inline=False)
    return embed


def _overrides_start_embed() -> discord.Embed:
    embed = base_embed(f"{EMOJI['logs']} Channel Overrides", "primary")
    embed.description = (
        "Silence **specific events in one channel** while everything else "
        "keeps logging \u2014 e.g. \u201cin #general, don't log Message Edited "
        "but keep Message Deleted\u201d.\n\n"
        "**1.** Pick a channel \u00b7 **2.** Pick an event group \u00b7 "
        "**3.** Tick the events to silence there \u2014 it saves automatically.\n\n"
        "Threads follow their parent channel. (To exclude a channel from "
        "logging **entirely**, use the channel picker on the overview panel.)")
    return embed


class _BackToLogHomeButton(Button):
    """Back to the log panel entry view (state re-fetched fresh from the DB)."""

    def __init__(self, row: int = 1):
        super().__init__(label="Back to Overview", style=discord.ButtonStyle.secondary,
                         emoji="\u25c0\ufe0f", row=row)

    async def callback(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        ignored = await get_log_ignored(guild_id)
        channels = [c for i in ignored
                    if (c := interaction.guild.get_channel(int(i))) is not None]
        cfg = await TicketConfig.fetch(guild_id)
        await interaction.response.edit_message(
            embed=await _status_embed(interaction.guild),
            view=LogPanelView(channels, debug_enabled=cfg.log_debug))


class LogPanelView(PanelView):
    """Entry panel: drill into a category, fully exclude channels, or open
    per-channel overrides. Same behavior family as the other config panels."""

    def __init__(self, ignored_channels: list | None = None, debug_enabled: bool = False):
        super().__init__()
        self._debug_enabled = debug_enabled
        # reflect the current state on the button (decorator label is static)
        self._debug_toggle.label = f"Debug Console Logs: {'On' if debug_enabled else 'Off'}"
        self._debug_toggle.style = (discord.ButtonStyle.success if debug_enabled
                                    else discord.ButtonStyle.secondary)
        cat_select = Select(placeholder="Pick a category to toggle its events\u2026",
                            min_values=1, max_values=1,
                            options=_category_options(),
                            custom_id="log_cat_select")
        cat_select.callback = self._open_category
        self.cat_select = cat_select
        self.add_item(cat_select)
        channel_select = ChannelSelect(
            placeholder="Fully exclude channels from logging (auto-saves)\u2026",
            min_values=0, max_values=25,
            default_values=ignored_channels or [],
            row=1,
        )
        channel_select.callback = self._set_ignored
        self.channel_select = channel_select
        self.add_item(channel_select)
        self.add_item(BackToHubButton(row=2))

    @discord.ui.button(label="Channel Overrides\u2026", style=discord.ButtonStyle.primary,
                       emoji="\U0001f39f\ufe0f", custom_id="log_overrides_btn", row=2)
    async def _overrides(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_message(embed=_overrides_start_embed(),
                                                view=ChannelOverrideStartView(),
                                                ephemeral=True)

    async def _open_category(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        if not self.cat_select.values:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        category = self.cat_select.values[0]
        guild_id = str(interaction.guild.id)
        _, subs = LOG_CATEGORIES[category]
        enabled_keys = [k for k in subs
                        if await is_log_event_enabled(guild_id, k)]
        await interaction.response.edit_message(
            embed=await _category_embed(interaction.guild, category),
            view=CategoryEventsView(category, enabled_keys))

    @discord.ui.button(label="Debug Console Logs: Off",
                       style=discord.ButtonStyle.secondary,
                       emoji="\U0001f41e", custom_id="log_debug_btn", row=2)
    async def _debug_toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        new_state = not self._debug_enabled
        self._debug_enabled = new_state
        await set_guild_setting(guild_id, "log_debug", int(new_state))
        button.label = f"Debug Console Logs: {'On' if new_state else 'Off'}"
        button.style = (discord.ButtonStyle.success if new_state
                        else discord.ButtonStyle.secondary)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"{EMOJI['ok'] if new_state else EMOJI['no']} Console debug traces "
            f"{'enabled' if new_state else 'disabled'} \u2014 this only affects the "
            "bot's console window, not the Discord log channel.",
            ephemeral=True)

    async def _set_ignored(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        current = set(await get_log_ignored(guild_id))
        selected = {str(c.id) for c in self.channel_select.values}
        for cid in current - selected:
            await set_log_ignored(guild_id, cid, False)
        for cid in selected - current:
            await set_log_ignored(guild_id, cid, True)
        await interaction.response.edit_message(embed=await _status_embed(interaction.guild),
                                                view=self)
        added = len(selected - current)
        removed = len(current - selected)
        parts = []
        if added:
            parts.append(f"{added} ignored")
        if removed:
            parts.append(f"{removed} un-ignored")
        await interaction.followup.send(
            f"{EMOJI['ok']} Ignored channels updated ({', '.join(parts) or 'no change'}). "
            "Threads inside an ignored channel are ignored too.",
            ephemeral=True)


class CategoryEventsView(PanelView):
    """Drill-in: toggle the individual events of one category."""

    def __init__(self, category: str, enabled_keys: list):
        super().__init__()
        self.category = category
        name, subs = LOG_CATEGORIES[category]
        self._sub_keys = list(subs)
        enabled_keys = set(enabled_keys)
        options = [discord.SelectOption(label=label[:100], value=key,
                                        default=key in enabled_keys)
                   for key, label in subs.items()]
        select = Select(placeholder=f"Events to log \u00b7 {name} (auto-saves)\u2026",
                        min_values=0, max_values=len(options), options=options,
                        custom_id="log_subevents_select")
        select.callback = self._save
        self.select = select
        self.add_item(select)
        self.add_item(_BackToLogHomeButton(row=1))

    async def _save(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        chosen = set(self.select.values)
        for key in self._sub_keys:
            await set_log_event(guild_id, key, key in chosen)
        fresh = CategoryEventsView(self.category,
                                   [k for k in self._sub_keys if k in chosen])
        await interaction.response.edit_message(
            embed=await _category_embed(interaction.guild, self.category),
            view=fresh)
        off = [LOG_EVENTS[k] for k in self._sub_keys if k not in chosen]
        await interaction.followup.send(
            f"{EMOJI['ok']} **{LOG_CATEGORIES[self.category][0]}** updated \u2014 "
            f"{len(chosen)} on, {len(off)} off."
            + (f" Off: {', '.join(off)}" if off else ""),
            ephemeral=True)


class ChannelOverrideStartView(PanelView):
    """Overrides, step 1: pick the channel to fine-tune."""

    def __init__(self):
        super().__init__()
        select = ChannelSelect(placeholder="Step 1 \u00b7 Pick a channel\u2026",
                               min_values=1, max_values=1)
        select.callback = self._pick
        self.select = select
        self.add_item(select)

    async def _pick(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        channel = self.select.values[0]
        suppressed = await get_channel_overrides(str(interaction.guild.id),
                                                 str(channel.id))
        await interaction.response.edit_message(
            embed=_override_embed(channel, suppressed),
            view=OverrideCategoryPickView(channel))


class OverrideCategoryPickView(PanelView):
    """Overrides, step 2: pick the event group to fine-tune in this channel."""

    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        select = Select(placeholder="Step 2 \u00b7 Pick an event group\u2026",
                        min_values=1, max_values=1, options=_category_options(),
                        custom_id="log_override_cat")
        select.callback = self._pick
        self.select = select
        self.add_item(select)
        back = Button(label="Pick a different channel",
                      style=discord.ButtonStyle.secondary,
                      emoji="\u25c0\ufe0f", row=1)
        back.callback = self._back
        self.add_item(back)

    async def _pick(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        category = self.select.values[0]
        guild_id = str(interaction.guild.id)
        suppressed = await get_channel_overrides(guild_id, str(self.channel.id))
        in_cat = [e for e in suppressed if LOG_EVENT_CATEGORY.get(e) == category]
        await interaction.response.edit_message(
            embed=_override_embed(self.channel, suppressed),
            view=ChannelOverrideEditView(self.channel, category, in_cat))

    async def _back(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(embed=_overrides_start_embed(),
                                                view=ChannelOverrideStartView())


class ChannelOverrideEditView(PanelView):
    """Overrides, step 3: choose which events of the group to silence here."""

    def __init__(self, channel, category: str, silenced_events: list):
        super().__init__()
        self.channel = channel
        self.category = category
        name, subs = LOG_CATEGORIES[category]
        silenced = set(silenced_events)
        options = [discord.SelectOption(label=label[:100], value=key,
                                        default=key in silenced)
                   for key, label in subs.items()]
        select = Select(
            placeholder=f"Step 3 \u00b7 Silence in {channel.name} \u00b7 {name} "
                        "(auto-saves)\u2026",
            min_values=0, max_values=len(options), options=options,
            custom_id="log_override_events")
        select.callback = self._save
        self.select = select
        self.add_item(select)
        back = Button(label="Back to event groups", style=discord.ButtonStyle.secondary,
                      emoji="\u25c0\ufe0f", row=1)
        back.callback = self._back
        self.add_item(back)
        done = Button(label="Done", style=discord.ButtonStyle.success,
                      emoji=EMOJI["ok"], row=1)
        done.callback = self._done
        self.add_item(done)

    async def _save(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        channel_id = str(self.channel.id)
        chosen = list(self.select.values)
        await set_channel_category_overrides(guild_id, channel_id,
                                             self.category, chosen)
        suppressed = await get_channel_overrides(guild_id, channel_id)
        await interaction.response.edit_message(
            embed=_override_embed(self.channel, suppressed),
            view=ChannelOverrideEditView(self.channel, self.category, chosen))
        n = len(chosen)
        await interaction.followup.send(
            f"{EMOJI['ok']} In {self.channel.mention}: {n} event{'s' if n != 1 else ''} "
            f"in **{LOG_CATEGORIES[self.category][0]}** silenced \u2014 everything "
            "else logs normally.", ephemeral=True)

    async def _back(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        suppressed = await get_channel_overrides(str(interaction.guild.id),
                                                 str(self.channel.id))
        await interaction.response.edit_message(
            embed=_override_embed(self.channel, suppressed),
            view=OverrideCategoryPickView(self.channel))

    async def _done(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


class Logging(commands.Cog):
    """Full server audit logging — everything is toggleable via /logsettings."""

    def __init__(self, bot):
        self.bot = bot

    async def _is_log_channel(self, channel) -> bool:
        cfg = await TicketConfig.fetch(str(channel.guild.id))
        return cfg.log_channel == channel.id

    # --- Commands ---
    @app_commands.command(name="logstatus", description="Show which log events are currently enabled.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logstatus(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await _status_embed(interaction.guild), ephemeral=True)

    logignore = app_commands.Group(name="logignore", description="Exclude channels from logging.")

    @logignore.command(name="add", description="Stop logging activity in a channel.")
    @app_commands.describe(channel="The channel to exclude (categories work too)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logignore_add(self, interaction: discord.Interaction,
                            channel: discord.abc.GuildChannel):
        await set_log_ignored(str(interaction.guild.id), str(channel.id), True)
        await interaction.response.send_message(
            f"{EMOJI['ok']} Activity in {channel.mention} will no longer be logged. "
            "Use `/logignore remove` to undo, or pick channels in the /setup \u2192 "
            "**Log Settings** panel.", ephemeral=True)

    @logignore.command(name="remove", description="Resume logging activity in a channel.")
    @app_commands.describe(channel="The channel to log again")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logignore_remove(self, interaction: discord.Interaction,
                               channel: discord.abc.GuildChannel):
        await set_log_ignored(str(interaction.guild.id), str(channel.id), False)
        await interaction.response.send_message(
            f"{EMOJI['ok']} Activity in {channel.mention} will be logged again.",
            ephemeral=True)

    @logignore.command(name="list", description="Show which channels are excluded from logging.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logignore_list(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await _status_embed(interaction.guild), ephemeral=True)

    @app_commands.command(name="logcategory", description="Enable or disable an entire log category at once.")
    @app_commands.describe(category="Category key or name, e.g. 'messages' or 'Messages'",
                           enabled="On or off for every event in the category")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logcategory(self, interaction: discord.Interaction,
                          category: str, enabled: bool):
        key = category.strip().lower().replace(" ", "_")
        match = next((k for k, (name, _) in LOG_CATEGORIES.items()
                      if key == k or key == name.lower()), None)
        if match is None:
            valid = ", ".join(f"`{k}`" for k in LOG_CATEGORIES)
            await interaction.response.send_message(
                f"{EMOJI['no']} Unknown category `{category}`. "
                f"Valid categories: {valid}", ephemeral=True)
            return
        guild_id = str(interaction.guild.id)
        for sub in LOG_CATEGORIES[match][1]:
            await set_log_event(guild_id, sub, enabled)
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"{EMOJI['ok'] if enabled else EMOJI['no']} "
            f"**{LOG_CATEGORIES[match][0]}** fully {state} "
            f"({len(LOG_CATEGORIES[match][1])} events).", ephemeral=True)

    # --- Messages (raw events: fire even when the message isn't in the cache) ---
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        message = payload.cached_message
        if await is_log_debug_enabled(str(guild.id)):
            audit_log.info("DELETE captured: #%s | cached=%s | id=%s",
                           channel, message is not None, payload.message_id)
        if message is not None:
            if message.author.bot:
                return
            if consume_automod_deleted(message.id):
                return  # auto-mod already logged this deletion
            if not message.content and not message.attachments:
                return
            embed = _base("\U0001f5d1\ufe0f Message Deleted", "danger")
            embed.add_field(name="Author", value=f"{message.author} (`{message.author.id}`)",
                            inline=True)
            embed.add_field(name="Channel", value=channel.mention, inline=True)
            embed.add_field(name="Content", value=_content(message.content), inline=False)
            if message.attachments:
                embed.add_field(name="Attachments",
                                value="\n".join(a.filename for a in message.attachments)[:1024],
                                inline=False)
        else:
            if consume_automod_deleted(payload.message_id):
                return
            # Discord doesn't send content for old messages — log what we can
            embed = _base("\U0001f5d1\ufe0f Message Deleted", "danger")
            embed.add_field(name="Channel", value=channel.mention, inline=True)
            embed.add_field(name="Author", value="Unknown (not in cache)", inline=True)
            embed.add_field(name="Content",
                            value="*Message was sent before the bot's cache window — "
                                  "content unavailable.*", inline=False)
        await log_event(guild, "message_delete", embed, channel=channel)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None or not payload.message_ids:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        if await is_log_debug_enabled(str(guild.id)):
            audit_log.info("PURGE captured: #%s | count=%s", channel, len(payload.message_ids))
        user_messages = [m for m in payload.cached_messages
                         if not m.author.bot and not consume_automod_deleted(m.id)]
        embed = _base("\U0001f9f9 Messages Purged", "danger")
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Count", value=len(payload.message_ids), inline=True)
        if user_messages:
            sample = "\n".join(f"**{m.author}:** {_content(m.content, 200)}"
                               for m in user_messages[:5])
            embed.add_field(name="Sample", value=sample[:1024], inline=False)
        else:
            embed.add_field(name="Sample",
                            value="*Messages were outside the bot's cache window — "
                                  "content unavailable.*", inline=False)
        await log_event(guild, "message_purge", embed, channel=channel)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        # The raw payload always includes the author and the NEW content
        author = payload.data.get("author") or {}
        if author.get("bot"):
            return
        after_content = payload.data.get("content") or ""
        before_message = payload.cached_message
        before_content = before_message.content if before_message else None
        if before_content == after_content:
            return  # embed/link-preview-only update
        if await is_log_debug_enabled(str(guild.id)):
            audit_log.info("EDIT captured: #%s | cached_before=%s",
                           channel, before_message is not None)
        author_value = (f"<@{author['id']}> (`{author['id']}`)"
                        if author.get("id") else "Unknown")
        embed = _base("\u270f\ufe0f Message Edited", "warning")
        embed.add_field(name="Author", value=author_value, inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Jump",
                        value=f"[Go to message](https://discord.com/channels/"
                              f"{guild.id}/{channel.id}/{payload.message_id})", inline=True)
        embed.add_field(name="Before",
                        value=_content(before_content) if before_content
                        else "*not in cache — unavailable*", inline=False)
        embed.add_field(name="After", value=_content(after_content), inline=False)
        await log_event(guild, "message_edit", embed, channel=channel)

    # --- Channels ---
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = _base("\U0001f7e2 Channel Created", "success")
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=_channel_label(channel), inline=True)
        embed.add_field(name="Category", value=_category_name(channel), inline=True)
        await log_event(channel.guild, "channel_create", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = _base("\U0001f534 Channel Deleted", "danger")
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=_channel_label(channel), inline=True)
        embed.add_field(name="Category", value=_category_name(channel), inline=True)
        await log_event(channel.guild, "channel_delete", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel,
                                      after: discord.abc.GuildChannel):
        # Group changes by type so each gets its OWN toggle:
        # rename / topic / settings (slowmode, NSFW, category) / permissions
        diffs: dict[str, list] = {}
        if before.name != after.name:
            diffs.setdefault("rename", []).append(
                f"Name: `{before.name}` \u2192 `{after.name}`")
        if _category_name(before) != _category_name(after):
            diffs.setdefault("settings", []).append(
                f"Category: `{_category_name(before)}` \u2192 `{_category_name(after)}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            if hasattr(before, "topic") and before.topic != after.topic:
                diffs.setdefault("topic", []).append("Topic changed")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            if hasattr(before, "slowmode_delay"):
                diffs.setdefault("settings", []).append(
                    f"Slowmode: {before.slowmode_delay}s \u2192 {after.slowmode_delay}s")
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            if hasattr(before, "nsfw"):
                diffs.setdefault("settings", []).append(
                    f"NSFW: {'on' if after.nsfw else 'off'}")
        if before.overwrites != after.overwrites:
            diffs.setdefault("perms", []).append("Permission overwrites changed")
        if not diffs:
            return
        titles = {
            "rename": "\U0001f7e1 Channel Renamed",
            "topic": "\U0001f7e1 Channel Topic Changed",
            "settings": "\U0001f7e1 Channel Settings Changed",
            "perms": "\U0001f7e1 Channel Permissions Changed",
        }
        keys = {
            "rename": "channel_rename",
            "topic": "channel_topic",
            "settings": "channel_settings",
            "perms": "channel_perms",
        }
        for kind, texts in diffs.items():
            embed = _base(titles[kind], "warning")
            embed.add_field(name="Channel", value=f"#{after.name}", inline=False)
            embed.add_field(name="Changes", value="\n".join(texts)[:1024], inline=False)
            await log_event(after.guild, keys[kind], embed)

    # --- Roles ---
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = _base("\U0001f7e2 Role Created", "success")
        embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(name="Color", value=str(role.color) if role.color else "Default", inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        await log_event(role.guild, "role_create", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = _base("\U0001f534 Role Deleted", "danger")
        embed.add_field(name="Role", value=role.name, inline=True)
        embed.add_field(name="Had Members", value=str(len(role.members)), inline=True)
        embed.add_field(name="Color", value=str(role.color) if role.color else "Default", inline=True)
        await log_event(role.guild, "role_delete", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        # Group changes by type so each gets its OWN toggle
        diffs: dict[str, list] = {}
        if before.name != after.name:
            diffs.setdefault("rename", []).append(
                f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.color != after.color:
            diffs.setdefault("settings", []).append(
                f"Color: `{before.color}` \u2192 `{after.color}`")
        if before.hoist != after.hoist:
            diffs.setdefault("settings", []).append(
                f"Hoisted: {before.hoist} \u2192 {after.hoist}")
        if before.mentionable != after.mentionable:
            diffs.setdefault("settings", []).append(
                f"Mentionable: {before.mentionable} \u2192 {after.mentionable}")
        if before.permissions != after.permissions:
            diffs.setdefault("perms", []).append("Permissions changed")
        if not diffs:
            return
        titles = {
            "rename": "\U0001f7e1 Role Renamed",
            "settings": "\U0001f7e1 Role Settings Changed",
            "perms": "\U0001f7e1 Role Permissions Changed",
        }
        keys = {
            "rename": "role_rename",
            "settings": "role_settings",
            "perms": "role_perms",
        }
        for kind, texts in diffs.items():
            embed = _base(titles[kind], "warning")
            embed.add_field(name="Role", value=after.mention, inline=False)
            embed.add_field(name="Changes", value="\n".join(texts)[:1024], inline=False)
            await log_event(after.guild, keys[kind], embed)

    # --- Punishments (catches manual bans/timeouts done outside the bot) ---
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        embed = _base("\U0001f528 Member Banned", "danger")
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=True)
        await log_event(guild, "mod_ban", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = _base("\U0001f513 Member Unbanned", "success")
        embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=True)
        await log_event(guild, "mod_unban", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Timeout changes
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                until = after.timed_out_until.strftime("%Y-%m-%d %H:%M UTC")
                embed = _base("\u23f0 Member Timed Out", "danger")
                embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
                embed.add_field(name="Until", value=until, inline=True)
                await log_event(after.guild, "mod_timeout", embed)
            else:
                embed = _base("\U0001f513 Timeout Removed", "success")
                embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
                await log_event(after.guild, "mod_timeout", embed)
        # Nickname changes
        if before.nick != after.nick:
            embed = _base("\U0001f4dd Nickname Changed", "warning")
            embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
            embed.add_field(name="Before", value=before.nick or "*none*", inline=True)
            embed.add_field(name="After", value=after.nick or "*none*", inline=True)
            await log_event(after.guild, "member_nick", embed)
        # Role add/remove
        before_roles = set(before.roles) - {before.guild.default_role}
        after_roles = set(after.roles) - {after.guild.default_role}
        added = after_roles - before_roles
        removed = before_roles - after_roles
        if added or removed:
            embed = _base("\U0001f3ad Roles Changed", "warning")
            embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
            if added:
                embed.add_field(name="Added",
                                value=", ".join(r.mention for r in added)[:1024], inline=False)
            if removed:
                embed.add_field(name="Removed",
                                value=", ".join(r.name for r in removed)[:1024], inline=False)
            await log_event(after.guild, "member_roles", embed)

    # --- Invites & server ---
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        embed = _base("\U0001f4e9 Invite Created", "success")
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        embed.add_field(name="Inviter",
                        value=f"{invite.inviter} (`{invite.inviter.id}`)"
                        if invite.inviter else "Unknown", inline=True)
        await log_event(invite.guild, "invite_create", embed, channel=invite.channel)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild is None:
            return
        embed = _base("\U0001f5d1\ufe0f Invite Deleted", "danger")
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        embed.add_field(name="Channel",
                        value=invite.channel.mention if invite.channel else "Unknown",
                        inline=True)
        await log_event(invite.guild, "invite_delete", embed, channel=invite.channel)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.afk_channel != after.afk_channel:
            changes.append("AFK channel changed")
        if before.verification_level != after.verification_level:
            changes.append(f"Verification level: {before.verification_level} "
                           f"\u2192 {after.verification_level}")
        if not changes:
            return
        embed = _base("\U0001f3db\ufe0f Server Updated", "warning")
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after, "server_update", embed)

    # --- Threads ---
    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        embed = _base("\U0001f9f5 Thread Created", "success")
        embed.add_field(name="Thread", value=thread.mention, inline=True)
        embed.add_field(name="In",
                        value=thread.parent.mention if thread.parent else "Unknown",
                        inline=True)
        embed.add_field(name="By", value=f"<@{thread.owner_id}>", inline=True)
        await log_event(thread.guild, "thread_create", embed, channel=thread)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        embed = _base("\U0001f5d1\ufe0f Thread Deleted", "danger")
        embed.add_field(name="Thread", value=f"#{thread.name}", inline=True)
        embed.add_field(name="In",
                        value=thread.parent.mention if thread.parent else "Unknown",
                        inline=True)
        await log_event(thread.guild, "thread_delete", embed, channel=thread)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.archived != after.archived:
            changes.append("Archived" if after.archived else "Un-archived")
        if before.locked != after.locked:
            changes.append("Locked" if after.locked else "Unlocked")
        if before.slowmode_delay != after.slowmode_delay:
            changes.append(f"Slowmode: {before.slowmode_delay}s \u2192 "
                           f"{after.slowmode_delay}s")
        if not changes:
            return
        embed = _base("\U0001f7e1 Thread Updated", "warning")
        embed.add_field(name="Thread", value=after.mention, inline=False)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after.guild, "thread_update", embed, channel=after)

    # --- Voice (joins / leaves / moves — mute/deafen changes are too noisy) ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        if before.channel == after.channel or member.bot:
            return
        if after.channel is not None and before.channel is None:
            embed = _base("\U0001f7e2 Voice Joined", "success")
            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
            embed.add_field(name="Channel", value=after.channel.mention, inline=True)
            await log_event(member.guild, "voice_join", embed, channel=after.channel)
        elif before.channel is not None and after.channel is None:
            embed = _base("\U0001f534 Voice Left", "danger")
            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
            embed.add_field(name="Channel", value=before.channel.mention, inline=True)
            await log_event(member.guild, "voice_leave", embed, channel=before.channel)
        else:
            embed = _base("\U0001f7e1 Voice Moved", "warning")
            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
            embed.add_field(name="From", value=before.channel.mention, inline=True)
            embed.add_field(name="To", value=after.channel.mention, inline=True)
            await log_event(member.guild, "voice_move", embed, channel=after.channel)

    # --- Emojis & Stickers ---
    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild,
                                     before: list, after: list):
        before_map = {e.id: e for e in before}
        after_map = {e.id: e for e in after}
        added = [e for i, e in after_map.items() if i not in before_map]
        removed = [e for i, e in before_map.items() if i not in after_map]
        renamed = [f"`{before_map[i].name}` \u2192 `{after_map[i].name}`"
                   for i in after_map
                   if i in before_map and before_map[i].name != after_map[i].name]
        if not (added or removed or renamed):
            return
        embed = _base("\U0001f600 Emojis Updated", "warning")
        if added:
            embed.add_field(name="Added",
                            value=" ".join(str(e) for e in added)[:1024], inline=False)
        if removed:
            embed.add_field(name="Removed",
                            value=", ".join(f":{e.name}:" for e in removed)[:1024],
                            inline=False)
        if renamed:
            embed.add_field(name="Renamed", value="\n".join(renamed)[:1024],
                            inline=False)
        await log_event(guild, "emoji_update", embed)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild,
                                       before: list, after: list):
        before_map = {s.id: s for s in before}
        after_map = {s.id: s for s in after}
        added = [s for i, s in after_map.items() if i not in before_map]
        removed = [s for i, s in before_map.items() if i not in after_map]
        renamed = [f"`{before_map[i].name}` \u2192 `{after_map[i].name}`"
                   for i in after_map
                   if i in before_map and before_map[i].name != after_map[i].name]
        if not (added or removed or renamed):
            return
        embed = _base("\U0001f5bc\ufe0f Stickers Updated", "warning")
        if added:
            embed.add_field(name="Added",
                            value=", ".join(f"**{s.name}**" for s in added)[:1024],
                            inline=False)
        if removed:
            embed.add_field(name="Removed",
                            value=", ".join(f"**{s.name}**" for s in removed)[:1024],
                            inline=False)
        if renamed:
            embed.add_field(name="Renamed", value="\n".join(renamed)[:1024],
                            inline=False)
        await log_event(guild, "sticker_update", embed)

    # --- Soundboard ---
    @commands.Cog.listener()
    async def on_soundboard_sound_create(self, sound: discord.SoundboardSound):
        if sound.guild is None:
            return
        embed = _base("\U0001f514 Soundboard Sound Added", "success")
        embed.add_field(name="Sound", value=sound.name, inline=True)
        if sound.user:
            embed.add_field(name="By",
                            value=f"{sound.user} (`{sound.user.id}`)", inline=True)
        await log_event(sound.guild, "soundboard_create", embed)

    @commands.Cog.listener()
    async def on_soundboard_sound_delete(self, sound: discord.SoundboardSound):
        if sound.guild is None:
            return
        embed = _base("\U0001f514 Soundboard Sound Removed", "danger")
        embed.add_field(name="Sound", value=sound.name, inline=True)
        await log_event(sound.guild, "soundboard_delete", embed)

    @commands.Cog.listener()
    async def on_soundboard_sound_update(self, before: discord.SoundboardSound,
                                         after: discord.SoundboardSound):
        if after.guild is None:
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.volume != after.volume:
            changes.append(f"Volume: {before.volume} \u2192 {after.volume}")
        if not changes:
            return
        embed = _base("\U0001f514 Soundboard Sound Updated", "warning")
        embed.add_field(name="Sound", value=after.name, inline=True)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after.guild, "soundboard_update", embed)

    # --- Stage ---
    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage: discord.StageInstance):
        channel = getattr(stage, "channel", None)
        embed = _base("\U0001f3a4 Stage Started", "success")
        embed.add_field(name="Channel",
                        value=channel.mention if channel else "Unknown", inline=True)
        embed.add_field(name="Topic", value=_content(stage.topic, 200), inline=True)
        await log_event(stage.guild, "stage_create", embed, channel=channel)

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage: discord.StageInstance):
        channel = getattr(stage, "channel", None)
        embed = _base("\U0001f3a4 Stage Ended", "danger")
        embed.add_field(name="Channel",
                        value=channel.mention if channel else "Unknown", inline=True)
        embed.add_field(name="Topic", value=_content(stage.topic, 200), inline=True)
        await log_event(stage.guild, "stage_delete", embed, channel=channel)

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before: discord.StageInstance,
                                       after: discord.StageInstance):
        changes = []
        if before.topic != after.topic:
            changes.append("Topic changed")
        if before.privacy_level != after.privacy_level:
            changes.append(f"Privacy: {before.privacy_level} \u2192 {after.privacy_level}")
        if not changes:
            return
        channel = getattr(after, "channel", None)
        embed = _base("\U0001f3a4 Stage Updated", "warning")
        embed.add_field(name="Channel",
                        value=channel.mention if channel else "Unknown", inline=True)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after.guild, "stage_update", embed, channel=channel)

    # --- Scheduled events ---
    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent):
        embed = _base("\U0001f4c5 Event Created", "success")
        embed.add_field(name="Event", value=event.name, inline=True)
        embed.add_field(name="Location",
                        value=event.channel.mention if event.channel else "*external*",
                        inline=True)
        embed.add_field(name="Starts",
                        value=event.start_time.strftime("%Y-%m-%d %H:%M UTC"),
                        inline=True)
        if event.creator:
            embed.add_field(name="By",
                            value=f"{event.creator} (`{event.creator.id}`)", inline=True)
        await log_event(event.guild, "event_create", embed)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent):
        embed = _base("\U0001f4c5 Event Deleted", "danger")
        embed.add_field(name="Event", value=event.name, inline=True)
        embed.add_field(name="Was Scheduled",
                        value=event.start_time.strftime("%Y-%m-%d %H:%M UTC"),
                        inline=True)
        await log_event(event.guild, "event_delete", embed)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent,
                                        after: discord.ScheduledEvent):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` \u2192 `{after.name}`")
        if before.channel != after.channel:
            changes.append("Location changed")
        if before.start_time != after.start_time:
            changes.append(f"Starts: {before.start_time.strftime('%Y-%m-%d %H:%M')} "
                           f"\u2192 {after.start_time.strftime('%Y-%m-%d %H:%M')}")
        if before.status != after.status:
            changes.append(f"Status: {before.status} \u2192 {after.status}")
        if not changes:
            return
        embed = _base("\U0001f4c5 Event Updated", "warning")
        embed.add_field(name="Event", value=after.name, inline=True)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await log_event(after.guild, "event_update", embed)

    # --- Applications & Webhooks ---
    @commands.Cog.listener()
    async def on_guild_integrations_update(self, guild: discord.Guild):
        embed = _base("\U0001f50c Integrations Updated", "warning")
        embed.add_field(name="What",
                        value="A bot or integration was added, removed or updated "
                              "in this server.", inline=False)
        await log_event(guild, "integration_update", embed)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        embed = _base("\U0001f50c Webhooks Updated", "warning")
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="What",
                        value="A webhook was created, edited or deleted here.",
                        inline=True)
        await log_event(channel.guild, "webhook_update", embed)

    # --- Polls (raw events: fire even when the poll message isn't cached) ---
    async def _log_poll_vote(self, payload, added: bool):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None or await self._is_log_channel(channel):
            return
        user = guild.get_member(payload.user_id)
        who = (f"{user} (`{payload.user_id}`)"
               if user else f"<@{payload.user_id}> (`{payload.user_id}`)")
        embed = _base("\U0001f4ca Poll Vote Added" if added
                      else "\U0001f4ca Poll Vote Removed",
                      "success" if added else "danger")
        embed.add_field(name="User", value=who, inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Answer", value=f"#{payload.answer_id}", inline=True)
        embed.add_field(name="Jump",
                        value=f"[Go to poll](https://discord.com/channels/"
                              f"{guild.id}/{channel.id}/{payload.message_id})",
                        inline=False)
        await log_event(guild, "poll_vote_add" if added else "poll_vote_remove",
                        embed, channel=channel)

    @commands.Cog.listener()
    async def on_raw_poll_vote_add(self, payload):
        await self._log_poll_vote(payload, True)

    @commands.Cog.listener()
    async def on_raw_poll_vote_remove(self, payload):
        await self._log_poll_vote(payload, False)

    # --- User profile changes (username / display name / avatar) ---
    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if before.bot:
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Username: `{before.name}` \u2192 `{after.name}`")
        if before.global_name != after.global_name:
            changes.append(f"Display name: `{before.global_name}` "
                           f"\u2192 `{after.global_name}`")
        if before.avatar != after.avatar:
            changes.append("Avatar updated")
        if not changes:
            return
        embed = _base("\U0001f464 Profile Updated", "warning")
        embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=True)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        # one log per guild the user shares with the bot
        for guild in self.bot.guilds:
            if guild.get_member(after.id) is not None:
                await log_event(guild, "member_profile", embed)


async def setup(bot):
    await bot.add_cog(Logging(bot))
