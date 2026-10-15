import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import (
    TicketConfig,
    ensure_guild_config,
    get_automod_exempt,
    log_event,
    mark_automod_deleted,
    set_automod_exempt,
    set_guild_setting,
)

INVITE_RE = re.compile(r"(?:discord\.gg/|discord(?:app)?\.com/invite/)([a-zA-Z0-9-]+)")
LINK_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# Anti-spam: N messages within T seconds
SPAM_LIMIT = 5
SPAM_WINDOW = 5.0
_message_history: dict[int, list[float]] = {}


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
        # server turned the bypass off via /automod
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

        notice = await message.channel.send(
            f"{message.author.mention} your message was removed by auto-moderation "
            f"({infraction.lower()}). Please review the server rules.",
            delete_after=8,
        )

        embed = discord.Embed(title="\U0001f6e1\ufe0f Auto-Moderation", color=0xed4245)
        embed.add_field(name="User", value=f"{message.author} (`{message.author.id}`)", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Module triggered", value=infraction, inline=True)
        content = message.content
        embed.add_field(name="Content",
                        value=content[:1024] if content else "*no text content*", inline=False)
        embed.set_footer(text=f"Message deleted in #{message.channel}")
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await log_event(message.guild, "automod", embed)

    # --- Configuration command ---
    @app_commands.command(name="automod", description="Configure the auto-moderation system.")
    @app_commands.describe(
        action="What to change",
        value="Number, on/off, or word(s) — for words you can batch with commas (word1,word2). Not needed for Status/Enable/Disable/Word List",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Status", value="status"),
        app_commands.Choice(name="Enable all", value="enable"),
        app_commands.Choice(name="Disable all", value="disable"),
        app_commands.Choice(name="Caps limit (percent, or off)", value="caps"),
        app_commands.Choice(name="Max mentions (number, or off)", value="mentions"),
        app_commands.Choice(name="Block invites (on/off)", value="invites"),
        app_commands.Choice(name="Block links (on/off)", value="links"),
        app_commands.Choice(name="Staff bypass (on/off)", value="staff_bypass"),
        app_commands.Choice(name="Exempt channel add", value="exempt_add"),
        app_commands.Choice(name="Exempt channel remove", value="exempt_remove"),
        app_commands.Choice(name="Exempt channel list", value="exempt_list"),
        app_commands.Choice(name="Word add (comma separated ok)", value="word_add"),
        app_commands.Choice(name="Word remove (comma separated ok)", value="word_remove"),
        app_commands.Choice(name="Word list", value="word_list"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod(self, interaction: discord.Interaction,
                      action: app_commands.Choice[str],
                      value: str = None):
        guild_id = str(interaction.guild.id)
        await ensure_guild_config(guild_id)
        cfg = await TicketConfig.fetch(guild_id)

        if action.value == "status":
            embed = discord.Embed(title="\U0001f6e1\ufe0f Auto-Moderation Status",
                                  color=0x5865f2)
            embed.add_field(name="Enabled", value="\u2705" if cfg.automod_enabled else "\u274c", inline=True)
            embed.add_field(name="Caps limit",
                            value=f"{cfg.automod_caps_percent}%" if cfg.automod_caps_percent else "off",
                            inline=True)
            embed.add_field(name="Max mentions",
                            value=str(cfg.automod_max_mentions) if cfg.automod_max_mentions else "off",
                            inline=True)
            embed.add_field(name="Block invites",
                            value="\u2705" if cfg.automod_block_invites else "\u274c", inline=True)
            embed.add_field(name="Block links",
                            value="\u2705" if cfg.automod_block_links else "\u274c", inline=True)
            embed.add_field(name="Staff bypass",
                            value="\u2705" if cfg.automod_staff_bypass else "\u274c", inline=True)
            exempt_ids = await get_automod_exempt(guild_id)
            embed.add_field(name="Exempt channels", value=str(len(exempt_ids)), inline=True)
            words = ", ".join(cfg.automod_blocked_words) or "none"
            embed.add_field(name=f"Blocked words ({len(cfg.automod_blocked_words)})",
                            value=words[:1024], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if action.value == "enable":
            await set_guild_setting(guild_id, "automod_enabled", 1)
            await set_guild_setting(guild_id, "automod_block_invites", 1)
            await interaction.response.send_message(
                "\u2705 Auto-moderation **enabled** (invites blocked by default — "
                "tune the rest with `/automod`).", ephemeral=True)
            return

        if action.value == "disable":
            await set_guild_setting(guild_id, "automod_enabled", 0)
            await interaction.response.send_message("\u274c Auto-moderation **disabled**.", ephemeral=True)
            return

        if value is None:
            await interaction.response.send_message(
                f"The **{action.name}** action needs a value.", ephemeral=True)
            return

        value = value.strip()
        lowered = value.lower()

        if action.value == "caps":
            if lowered == "off":
                await set_guild_setting(guild_id, "automod_caps_percent", 0)
                await interaction.response.send_message("\u2705 Caps filter turned **off**.", ephemeral=True)
            elif lowered.isdigit() and 1 <= int(lowered) <= 100:
                await set_guild_setting(guild_id, "automod_caps_percent", int(lowered))
                await interaction.response.send_message(
                    f"\u2705 Messages with {lowered}%+ caps will be removed.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "Give a percentage between 1 and 100, or `off`.", ephemeral=True)
            return

        if action.value == "mentions":
            if lowered == "off":
                await set_guild_setting(guild_id, "automod_max_mentions", 0)
                await interaction.response.send_message("\u2705 Mention limit turned **off**.", ephemeral=True)
            elif lowered.isdigit() and 1 <= int(lowered) <= 50:
                await set_guild_setting(guild_id, "automod_max_mentions", int(lowered))
                await interaction.response.send_message(
                    f"\u2705 Messages mentioning more than {lowered} users will be removed.",
                    ephemeral=True)
            else:
                await interaction.response.send_message(
                    "Give a number between 1 and 50, or `off`.", ephemeral=True)
            return

        if action.value in ("invites", "links"):
            if lowered not in ("on", "off"):
                await interaction.response.send_message("Use `on` or `off`.", ephemeral=True)
                return
            column = "automod_block_invites" if action.value == "invites" else "automod_block_links"
            await set_guild_setting(guild_id, column, 1 if lowered == "on" else 0)
            state = "will now be **removed**" if lowered == "on" else "are **allowed** again"
            thing = "Discord invites" if action.value == "invites" else "External links"
            await interaction.response.send_message(f"\u2705 {thing} {state}.", ephemeral=True)
            return

        if action.value in ("exempt_add", "exempt_remove"):
            if not value:
                await interaction.response.send_message(
                    "Give a channel — mention it or paste its ID.", ephemeral=True)
                return
            import re
            match = re.match(r"^(?:<#(\d+)>|(\d+))$", value.strip())
            raw_id = int(match.group(1) or match.group(2)) if match else None
            channel = interaction.guild.get_channel(raw_id) if raw_id else None
            if channel is None:
                await interaction.response.send_message(
                    "Couldn't find that channel — mention it or paste its ID.",
                    ephemeral=True)
                return
            if action.value == "exempt_add":
                await set_automod_exempt(guild_id, str(channel.id), True)
                await interaction.response.send_message(
                    f"\u2705 {channel.mention} is now **exempt** from auto-moderation.",
                    ephemeral=True)
            else:
                await set_automod_exempt(guild_id, str(channel.id), False)
                await interaction.response.send_message(
                    f"\u2705 {channel.mention} is no longer exempt — auto-mod applies there again.",
                    ephemeral=True)
            return

        if action.value == "exempt_list":
            ids = await get_automod_exempt(guild_id)
            if not ids:
                await interaction.response.send_message(
                    "No exempt channels. Add one with `/automod` > **Exempt channel add**.",
                    ephemeral=True)
                return
            mentions = [interaction.guild.get_channel(int(cid)) for cid in ids]
            lines = [c.mention if c else f"`{cid}` (deleted)" for c, cid in zip(mentions, ids)]
            embed = discord.Embed(title="\U0001f6e1\ufe0f Auto-Mod Exempt Channels",
                                  description="\n".join(lines), color=0x5865f2)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if action.value == "staff_bypass":
            if lowered not in ("on", "off"):
                await interaction.response.send_message("Use `on` or `off`.", ephemeral=True)
                return
            await set_guild_setting(guild_id, "automod_staff_bypass", 1 if lowered == "on" else 0)
            if lowered == "on":
                msg = ("\u2705 Staff bypass **on** — users with `Manage Messages` "
                       "or admin permissions are ignored by auto-mod.")
            else:
                msg = ("\u274c Staff bypass **off** — everyone is moderated, including "
                       "mods and admins. (Use with care!)")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if action.value in ("word_add", "word_remove"):
            # Accept a single word or a comma-separated batch: word1,word2,word3
            raw_words = [w.strip().lower() for w in value.split(",")]
            words = list(cfg.automod_blocked_words)
            if action.value == "word_add":
                added, skipped = [], []
                for word in raw_words:
                    if not word:
                        continue
                    if word in words:
                        skipped.append(word)
                    else:
                        words.append(word)
                        added.append(word)
                await cfg.save_automod_words(words)
                parts = []
                if added:
                    parts.append("\u2705 Added: " + ", ".join(f"`{w}`" for w in added))
                if skipped:
                    parts.append("(already on the list: " + ", ".join(f"`{w}`" for w in skipped) + ")")
                if not parts:
                    await interaction.response.send_message(
                        "No valid words given.", ephemeral=True)
                    return
                await interaction.response.send_message("\n".join(parts), ephemeral=True)
            else:
                removed, missing = [], []
                for word in raw_words:
                    if not word:
                        continue
                    if word in words:
                        words.remove(word)
                        removed.append(word)
                    else:
                        missing.append(word)
                if removed:
                    await cfg.save_automod_words(words)
                parts = []
                if removed:
                    parts.append("\u2705 Removed: " + ", ".join(f"`{w}`" for w in removed))
                if missing:
                    parts.append("(not on the list: " + ", ".join(f"`{w}`" for w in missing) + ")")
                if not parts:
                    await interaction.response.send_message(
                        "No valid words given.", ephemeral=True)
                    return
                await interaction.response.send_message("\n".join(parts), ephemeral=True)
            return

        if action.value == "word_list":
            words = cfg.automod_blocked_words
            if not words:
                await interaction.response.send_message("No blocked words configured.", ephemeral=True)
            else:
                embed = discord.Embed(title="\U0001f6ab Blocked Words",
                                      description=", ".join(f"`{w}`" for w in words[:100]),
                                      color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))