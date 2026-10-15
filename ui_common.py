"""Shared UI kit — one look and one behavior for every config panel.

Every interactive setup interface in the bot (auto-mod, verification,
logging, the /setup hub, ...) builds on these helpers so the messages,
colors, emoji and click-flows are all the same everywhere.
"""

import datetime
import re

import discord

# --- Brand palette: every embed in the bot pulls from these ---
BRAND = {
    "primary": 0x5865F2,   # blurple
    "success": 0x57F287,   # green
    "danger": 0xED4245,    # red
    "warning": 0xFEE75C,   # yellow
    "neutral": 0x2B2D31,   # dark
}

# Emoji vocabulary used across all interfaces
EMOJI = {
    "ok": "\u2705",
    "no": "\u274c",
    "warn": "\u26a0\ufe0f",
    "refresh": "\U0001f504",
    "shield": "\U0001f6e1\ufe0f",
    "ticket": "\U0001f3ab",
    "verify": "\U0001f39f\ufe0f",
    "logs": "\U0001f4cb",
    "bolt": "\u26a1",
    "send": "\U0001f4e9",
}


def base_embed(title: str, color: str = "primary",
               description: str | None = None) -> discord.Embed:
    """The one embed factory: brand color + timestamp, always."""
    embed = discord.Embed(title=title, color=BRAND[color], description=description)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def ok_embed(description: str) -> discord.Embed:
    return base_embed(f"{EMOJI['ok']} Done", "success", description)


def err_embed(description: str) -> discord.Embed:
    return base_embed(f"{EMOJI['no']} Something went wrong", "danger", description)


def warn_embed(description: str) -> discord.Embed:
    return base_embed(f"{EMOJI['warn']} Heads up", "warning", description)


def yn(value) -> str:
    """✅ / ❌ for any truthy setting — used in every status embed."""
    return EMOJI["ok"] if value else EMOJI["no"]


def truncate(text: str, limit: int = 1024) -> str:
    if not text:
        return "*none*"
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


_SNOWFLAKE_RE = re.compile(r"^(?:<#(\d+)>|<@!?(\d+)>|<@&(\d+)>|(\d+))$")


def parse_snowflake(value: str) -> int | None:
    """Parse `<#id>` / `<@id>` / `<@&id>` / plain ID into an int id.

    One parser for the whole bot — replaces the four copy-pasted regexes.
    """
    if not value:
        return None
    match = _SNOWFLAKE_RE.match(value.strip())
    if not match:
        return None
    return int(next(g for g in match.groups() if g))


async def deny(interaction: discord.Interaction, needed: str = "Manage Server"):
    """Answer an interaction with the standard 'not allowed' message."""
    if interaction.response.is_done():
        await interaction.followup.send(
            f"{EMOJI['no']} You need the **{needed}** permission to use this.",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            f"{EMOJI['no']} You need the **{needed}** permission to use this.",
            ephemeral=True)


class PanelView(discord.ui.View):
    """Base view for every config panel: shared timeout + permission guard.

    Subclasses set PERM_LABEL and implement their own items; each callback
    starts with `if not await self.guard(interaction): return`.
    """

    PERM_LABEL = "Manage Server"
    PERM = "manage_guild"
    TIMEOUT = 600

    def __init__(self):
        super().__init__(timeout=self.TIMEOUT)

    async def guard(self, interaction: discord.Interaction) -> bool:
        """Permission check for panel clicks. Answers the interaction if denied."""
        user = interaction.user
        allowed = getattr(user.guild_permissions, self.PERM, False)
        if not allowed:
            await deny(interaction, self.PERM_LABEL)
            return False
        return True

    async def on_timeout(self):
        # Freeze every panel the same way when it expires
        for child in self.children:
            child.disabled = True
        self.stop()
