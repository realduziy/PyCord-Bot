import datetime
import re

import discord
from discord.ext import commands
from discord.ui import Button, Modal, Select, TextInput

from cog_utils import (
    TicketConfig,
    get_automod_exempt,
    log_event,
    mark_automod_deleted,
    set_automod_exempt,
    set_guild_setting,
)
from ui_common import (
    EMOJI,
    PanelView,
    base_embed,
    err_embed,
    ok_embed,
    parse_snowflake,
    truncate,
    yn,
)
from cogs._hub import BackToHubButton

INVITE_RE = re.compile(r"(?:discord\.gg/|discord(?:app)?\.com/invite/)([a-zA-Z0-9-]+)")
LINK_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# Anti-spam: N messages within T seconds
SPAM_LIMIT = 5
SPAM_WINDOW = 5.0
_message_history: dict[int, list[float]] = {}


def prune_spam_history():
    """Drop stale per-user message histories so the dict can't grow forever."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for user_id in list(_message_history):
        history = [t for t in _message_history[user_id] if t >= now - SPAM_WINDOW]
        if history:
            _message_history[user_id] = history
        else:
            del _message_history[user_id]


def _caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:  # too short to judge
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters)


def _detect_infraction(cfg: TicketConfig, content: str) -> str | None:
    """Return the name of the module the message violates, or None."""
    lowered = content.lower()

    if cfg.automod_blocked_words and any(
            re.search(rf"\b{re.escape(w.lower())}\b", lowered) for w in cfg.automod_blocked_words):
        return "Blocked word"

    if cfg.automod_block_invites and INVITE_RE.search(content):
        return "Discord invite"

    if cfg.automod_block_links and LINK_RE.search(content):
        return "External link"

    if cfg.automod_max_mentions and len(content.split("<@")) - 1 > cfg.automod_max_mentions:
        return "Mass mention"

    if cfg.automod_caps_percent and _caps_ratio(content) * 100 >= cfg.automod_caps_percent:
        return "Excessive caps"

    return None


def _is_spam(author_id: int) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    history = _message_history.setdefault(author_id, [])
    history.append(now)
    cutoff = now - SPAM_WINDOW
    while history and history[0] < cutoff:
        history.pop(0)
    return len(history) > SPAM_LIMIT


# --- Status embed: the same layout every time, panel or not ---
async def automod_status_embed(guild: discord.Guild) -> discord.Embed:
    guild_id = str(guild.id)
    cfg = await TicketConfig.fetch(guild_id)
    embed = base_embed(f"{EMOJI['shield']} Auto-Moderation", "primary")
    embed.add_field(name="Enabled", value=yn(cfg.automod_enabled), inline=True)
    embed.add_field(name="Block invites",
                    value=yn(cfg.automod_block_invites), inline=True)
    embed.add_field(name="Block links",
                    value=yn(cfg.automod_block_links), inline=True)
    embed.add_field(name="Staff bypass",
                    value=yn(cfg.automod_staff_bypass), inline=True)
    embed.add_field(
        name="Caps limit",
        value=f"{cfg.automod_caps_percent}%" if cfg.automod_caps_percent else "off",
        inline=True)
    embed.add_field(
        name="Max mentions",
        value=str(cfg.automod_max_mentions) if cfg.automod_max_mentions else "off",
        inline=True)
    exempt_ids = await get_automod_exempt(guild_id)
    exempt_names = []
    for cid in exempt_ids:
        ch = guild.get_channel(int(cid))
        exempt_names.append(ch.mention if ch else f"`{cid}` (deleted)")
    embed.add_field(name=f"Exempt channels ({len(exempt_ids)})",
                    value="\n".join(exempt_names) or "none", inline=False)
    embed.add_field(name=f"Blocked words ({len(cfg.automod_blocked_words)})",
                    value=truncate(", ".join(f"`{w}`" for w in cfg.automod_blocked_words)),
                    inline=False)
    return embed


# --- Modals for the value-taking settings (shared flow: validate -> save -> refresh) ---
class _AutoModSettingModal(Modal):
    def __init__(self, panel: "AutoModPanelView", title: str, label: str,
                 placeholder: str):
        super().__init__(title=title, timeout=180)
        self.panel = panel
        self.input = TextInput(label=label, placeholder=placeholder,
                               max_length=100, required=True)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        raise NotImplementedError

    async def _finish(self, interaction: discord.Interaction, message: str,
                      error: bool = False):
        """Standard modal outro: answer + refresh the panel message."""
        if error:
            await interaction.response.send_message(embed=err_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=ok_embed(message), ephemeral=True)
        await self.panel.refresh(interaction)


class CapsModal(_AutoModSettingModal):
    def __init__(self, panel):
        super().__init__(panel, "Caps Limit",
                         "Percent (1-100) or 'off'",
                         "e.g. 70 — or 'off' to disable")

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input.value.strip().lower()
        if value == "off":
            await set_guild_setting(str(interaction.guild.id), "automod_caps_percent", 0)
            await self._finish(interaction, "Caps filter turned **off**.")
        elif value.isdigit() and 1 <= int(value) <= 100:
            await set_guild_setting(str(interaction.guild.id), "automod_caps_percent", int(value))
            await self._finish(interaction,
                               f"Messages with {value}%+ caps will be removed.")
        else:
            await self._finish(interaction,
                               "Give a percentage between 1 and 100, or `off`.",
                               error=True)


class MentionsModal(_AutoModSettingModal):
    def __init__(self, panel):
        super().__init__(panel, "Mention Limit",
                         "Max mentions (1-50) or 'off'",
                         "e.g. 5 — or 'off' to disable")

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input.value.strip().lower()
        if value == "off":
            await set_guild_setting(str(interaction.guild.id), "automod_max_mentions", 0)
            await self._finish(interaction, "Mention limit turned **off**.")
        elif value.isdigit() and 1 <= int(value) <= 50:
            await set_guild_setting(str(interaction.guild.id), "automod_max_mentions", int(value))
            await self._finish(interaction,
                               f"Messages mentioning more than {value} users will be removed.")
        else:
            await self._finish(interaction,
                               "Give a number between 1 and 50, or `off`.",
                               error=True)


class WordsModal(_AutoModSettingModal):
    def __init__(self, panel, add: bool):
        super().__init__(panel, "Add Blocked Words" if add else "Remove Blocked Words",
                         "Word(s), comma separated",
                         "word1, word2, word3")
        self.add = add

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        cfg = await TicketConfig.fetch(guild_id)
        raw_words = [w.strip().lower() for w in self.input.value.split(",") if w.strip()]
        if not raw_words:
            await self._finish(interaction, "No valid words given.", error=True)
            return
        words = list(cfg.automod_blocked_words)
        if self.add:
            added = [w for w in raw_words if w not in words]
            words.extend(added)
            skipped = [w for w in raw_words if w in cfg.automod_blocked_words]
            await cfg.save_automod_words(words)
            parts = []
            if added:
                parts.append("Added: " + ", ".join(f"`{w}`" for w in added))
            if skipped:
                parts.append("(already on the list: " + ", ".join(f"`{w}`" for w in skipped) + ")")
            await self._finish(interaction, "\n".join(parts))
        else:
            removed = [w for w in raw_words if w in words]
            missing = [w for w in raw_words if w not in words]
            if removed:
                for w in removed:
                    words.remove(w)
                await cfg.save_automod_words(words)
            parts = []
            if removed:
                parts.append("Removed: " + ", ".join(f"`{w}`" for w in removed))
            if missing:
                parts.append("(not on the list: " + ", ".join(f"`{w}`" for w in missing) + ")")
            await self._finish(interaction, "\n".join(parts))


class ExemptModal(_AutoModSettingModal):
    def __init__(self, panel, add: bool):
        super().__init__(panel, "Exempt Channel" if add else "Un-exempt Channel",
                         "Channel (mention or ID)",
                         "#memes or 123456789")
        self.add = add

    async def on_submit(self, interaction: discord.Interaction):
        raw_id = parse_snowflake(self.input.value)
        channel = interaction.guild.get_channel(raw_id) if raw_id else None
        if channel is None:
            await self._finish(
                interaction,
                "Couldn't find that channel — mention it (`#general`) or paste its ID.",
                error=True)
            return
        await set_automod_exempt(str(interaction.guild.id), str(channel.id), self.add)
        if self.add:
            await self._finish(interaction,
                               f"{channel.mention} is now **exempt** from auto-moderation.")
        else:
            await self._finish(interaction,
                               f"{channel.mention} is no longer exempt — auto-mod applies there again.")


# --- The panel ---
class AutoModPanelView(PanelView):
    """Interactive auto-mod configuration — same pattern as every other panel."""

    def __init__(self):
        super().__init__()
        self._build_select()
        self.add_item(BackToHubButton(row=2))

    def _build_select(self):
        select = Select(placeholder="Adjust a setting\u2026", min_values=1, max_values=1,
                        options=[
                            discord.SelectOption(label="Caps limit (percent, or off)",
                                                 value="caps", emoji="\U0001f4c8"),
                            discord.SelectOption(label="Max mentions (number, or off)",
                                                 value="mentions", emoji="\U0001f465"),
                            discord.SelectOption(label="Add blocked word(s)",
                                                 value="word_add", emoji="\U0001f6ab"),
                            discord.SelectOption(label="Remove blocked word(s)",
                                                 value="word_remove", emoji="\u2796"),
                            discord.SelectOption(label="Exempt a channel",
                                                 value="exempt_add", emoji="\U0001f515"),
                            discord.SelectOption(label="Un-exempt a channel",
                                                 value="exempt_remove", emoji="\U0001f514"),
                        ])
        select.callback = self._select_callback
        self.setting_select = select
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        choice = self.setting_select.values[0]
        modals = {
            "caps": lambda: CapsModal(self),
            "mentions": lambda: MentionsModal(self),
            "word_add": lambda: WordsModal(self, add=True),
            "word_remove": lambda: WordsModal(self, add=False),
            "exempt_add": lambda: ExemptModal(self, add=True),
            "exempt_remove": lambda: ExemptModal(self, add=False),
        }
        await interaction.response.send_modal(modals[choice]())

    async def refresh(self, interaction: discord.Interaction):
        """Rewrite the panel message with the current status."""
        try:
            # Ephemeral panel messages can't be edited via the channel
            # endpoint — use the interaction's original response instead
            await interaction.edit_original_response(
                embed=await automod_status_embed(interaction.guild), view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @discord.ui.button(label="Toggle Auto-Mod", style=discord.ButtonStyle.primary,
                       emoji=EMOJI["shield"], custom_id="am_toggle_btn", row=0)
    async def toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        await set_guild_setting(str(interaction.guild.id), "automod_enabled",
                                0 if cfg.automod_enabled else 1)
        state = "disabled" if cfg.automod_enabled else "enabled"
        await interaction.response.send_message(embed=ok_embed(f"Auto-moderation **{state}**."),
                                                ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Invites", style=discord.ButtonStyle.secondary,
                       emoji="\U0001f4e9", custom_id="am_invites_btn", row=0)
    async def invites(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        new = 0 if cfg.automod_block_invites else 1
        await set_guild_setting(str(interaction.guild.id), "automod_block_invites", new)
        state = "allowed again" if cfg.automod_block_invites else "now removed"
        await interaction.response.send_message(
            embed=ok_embed(f"Discord invites {state}."), ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Links", style=discord.ButtonStyle.secondary,
                       emoji="\U0001f517", custom_id="am_links_btn", row=0)
    async def links(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        new = 0 if cfg.automod_block_links else 1
        await set_guild_setting(str(interaction.guild.id), "automod_block_links", new)
        state = "allowed again" if cfg.automod_block_links else "now removed"
        await interaction.response.send_message(
            embed=ok_embed(f"External links {state}."), ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Staff Bypass", style=discord.ButtonStyle.secondary,
                       emoji="\U0001f468\u200d\u2708\ufe0f", custom_id="am_bypass_btn", row=0)
    async def bypass(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        new = 0 if cfg.automod_staff_bypass else 1
        await set_guild_setting(str(interaction.guild.id), "automod_staff_bypass", new)
        if cfg.automod_staff_bypass:
            msg = ("Staff bypass **off** — everyone is moderated, including mods and admins. "
                   "(Use with care!)")
        else:
            msg = ("Staff bypass **on** — users with `Manage Messages` are ignored by auto-mod.")
        await interaction.response.send_message(embed=ok_embed(msg), ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.success,
                       emoji=EMOJI["refresh"], custom_id="am_refresh_btn", row=0)
    async def refresh_btn(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(
            embed=await automod_status_embed(interaction.guild), view=self)


class AutoMod(commands.Cog):
    """Configurable auto-moderation: blocked words, invites, links, caps, mentions, spam."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return

        guild_id = str(message.guild.id)
        cfg = await TicketConfig.fetch(guild_id)
        if not cfg.automod_enabled:
            return
        # Exempt channels (e.g. meme/advertising channels) skip auto-mod entirely
        if str(message.channel.id) in await get_automod_exempt(guild_id):
            return

        # Staff bypass: users with manage_messages skip auto-mod unless the
        # server turned the bypass off via the auto-mod panel
        if cfg.automod_staff_bypass and isinstance(message.author, discord.Member) and \
                message.author.guild_permissions.manage_messages:
            return
        # Don't police ticket channels
        if message.channel.category and message.channel.category.name == "Tickets":
            return

        infraction = _detect_infraction(cfg, message.content)
        if infraction is None and _is_spam(message.author.id):
            infraction = "Message spam"

        if infraction is None:
            return

        try:
            await message.delete()
            mark_automod_deleted(message.id)
        except (discord.Forbidden, discord.HTTPException):
            return

        await message.channel.send(
            f"{message.author.mention} your message was removed by auto-moderation "
            f"({infraction.lower()}). Please review the server rules.",
            delete_after=8,
        )

        embed = discord.Embed(title="\U0001f6e1\ufe0f Auto-Moderation", color=0xed4245)
        embed.add_field(name="User", value=f"{message.author} (`{message.author.id}`)", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Module triggered", value=infraction, inline=True)
        embed.add_field(name="Content", value=truncate(message.content), inline=False)
        embed.set_footer(text=f"Message deleted in #{message.channel}")
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await log_event(message.guild, "automod", embed)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
