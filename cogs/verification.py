import datetime
import io
import random
import string
import time

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from PIL import Image, ImageDraw, ImageFont

from cog_utils import (
    TicketConfig,
    ensure_guild_config,
    get_verification_stats,
    log_event,
    record_verification_attempt,
    set_guild_setting,
)

CAPTCHA_TTL = 300  # seconds before a captcha code expires
CAPTCHA_LENGTH = 5
MODE_LABELS = {"captcha": "\U0001f9e9 Captcha", "click": "\U0001f446 One-click"}

# user_id -> (code, expiry_timestamp); codes are single-use
_captcha_codes: dict[int, tuple[str, float]] = {}

# Consecutive failed attempts per user before we raise a flag (in-memory)
_failed_attempts: dict[int, int] = {}
MAX_FAILED_ATTEMPTS = 3


# --- Captcha image generation ---
def _generate_captcha() -> tuple[str, io.BytesIO]:
    """Returns (code, PNG image buffer). Noisy, slightly-warped character image."""
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=CAPTCHA_LENGTH))
    width, height = 220, 90
    image = Image.new("RGB", (width, height), (30, 33, 36))
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype("arial.ttf", 44)
    except OSError:
        font = ImageFont.load_default()

    # Random background noise lines
    for _ in range(6):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=(random.randint(40, 90), random.randint(40, 90), random.randint(40, 90)),
            width=2,
        )

    # Characters with random colors/offsets/rotation
    for i, char in enumerate(code):
        x = 18 + i * 40 + random.randint(-4, 4)
        y = random.randint(18, 34)
        color = (random.randint(120, 255), random.randint(120, 255), random.randint(120, 255))
        if font.getbbox(char)[3] > 40:  # truetype font available
            char_img = Image.new("RGBA", (50, 60), (0, 0, 0, 0))
            char_draw = ImageDraw.Draw(char_img)
            char_draw.text((5, 5), char, font=font, fill=color)
            char_img = char_img.rotate(random.randint(-25, 25), expand=True, resample=Image.BICUBIC)
            image.paste(char_img, (x, y), char_img)
        else:  # fallback bitmap font: plain draw
            draw.text((x, y + 10), char, font=font, fill=color)

    # Foreground noise dots
    for _ in range(120):
        draw.point((random.randint(0, width - 1), random.randint(0, height - 1)),
                   fill=(random.randint(60, 160),) * 3)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return code, buffer


def _new_captcha(user_id: int) -> io.BytesIO:
    code, buffer = _generate_captcha()
    _captcha_codes[user_id] = (code, time.time() + CAPTCHA_TTL)
    return buffer


def _verification_log(guild: discord.Guild, title: str, color: int,
                      member: discord.Member, detail: str = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Account created",
                    value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    if detail:
        embed.add_field(name="Details", value=detail, inline=False)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


async def _grant_verified_role(guild: discord.Guild, member: discord.Member) -> str | None:
    """Assign the verified role. Returns an error message, or None on success."""
    cfg = await TicketConfig.fetch(str(guild.id))
    verified_role = guild.get_role(cfg.verified_role) if cfg.verified_role else None
    if not verified_role:
        return ("No verified role is configured — set one with `/set` > "
                "**Verified role**, or run `/setup auto`.")
    if verified_role in member.roles:
        return None  # already has it, nothing to do
    try:
        await member.add_roles(verified_role, reason="Passed verification")
    except (discord.Forbidden, discord.HTTPException):
        return ("\u274c I couldn't assign the verified role — check my role position "
                "and the **Manage Roles** permission.")
    return None


async def _finish_success(interaction: discord.Interaction, member: discord.Member,
                          manual_by: discord.Member = None):
    """Shared success path: role + DB + log."""
    guild_id = str(interaction.guild.id)
    await record_verification_attempt(guild_id, str(member.id), True)
    _failed_attempts.pop(member.id, None)
    title = "\u2705 Manually Verified" if manual_by else "\u2705 Verification Passed"
    detail = f"Verified by {manual_by} (`{manual_by.id}`)" if manual_by else None
    await log_event(interaction.guild, "verification", _verification_log(
        interaction.guild, title, 0x57f287 if not manual_by else 0x5865f2, member, detail))


async def _finish_failure(guild: discord.Guild, member: discord.Member,
                          detail_prefix: str = None):
    """Shared failure path: count, log, return the current consecutive-fail count."""
    fails = _failed_attempts.get(member.id, 0) + 1
    _failed_attempts[member.id] = fails
    detail = detail_prefix or ""
    if fails >= MAX_FAILED_ATTEMPTS:
        detail += (f"\u26a0\ufe0f **{fails} consecutive failures** — user may be a bot "
                   "or failing the check repeatedly.")
    else:
        detail += f"Consecutive failures: **{fails}**"
    await log_event(guild, "verification", _verification_log(
        guild, "\u274c Verification Failed", 0xed4245, member, detail.strip()))
    return fails


# --- UI ---
class CaptchaCodeModal(Modal):
    code = TextInput(
        label="Enter the code shown in the image",
        placeholder="e.g. A7X2K",
        min_length=CAPTCHA_LENGTH,
        max_length=CAPTCHA_LENGTH,
        required=True,
    )

    def __init__(self):
        super().__init__(title="Enter Captcha Code", timeout=180)

    async def on_submit(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        guild_id = str(guild.id)

        stored = _captcha_codes.pop(member.id, None)
        if stored is None or time.time() > stored[1]:
            await interaction.response.send_message(
                "\u23f0 That captcha has expired — press **Verify** again for a new one.",
                ephemeral=True)
            return

        expected, _ = stored
        if self.code.value.strip().upper() == expected:
            error = await _grant_verified_role(guild, member)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await record_verification_attempt(guild_id, str(member.id), True)
            _failed_attempts.pop(member.id, None)
            await interaction.response.send_message(
                "\u2705 Correct! You have been verified. Welcome to the server.", ephemeral=True)
            await log_event(guild, "verification", _verification_log(
                guild, "\u2705 Verification Passed", 0x57f287, member))
        else:
            await record_verification_attempt(guild_id, str(member.id), False)
            fails = await _finish_failure(guild, member, "Wrong captcha code entered. ")
            await interaction.response.send_message(
                f"\u274c That's not the right code ({fails}/{MAX_FAILED_ATTEMPTS} "
                "consecutive failures). Press **Verify** to try a new captcha.",
                ephemeral=True)


class CaptchaChallengeView(View):
    """Ephemeral view holding the captcha image + the button to submit a code."""

    def __init__(self):
        super().__init__(timeout=CAPTCHA_TTL)

    @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.primary,
                       emoji="\u2328\ufe0f", custom_id="captcha_submit_btn")
    async def submit(self, interaction: discord.Interaction, button: Button):
        stored = _captcha_codes.get(interaction.user.id)
        if stored is None or time.time() > stored[1]:
            await interaction.response.send_message(
                "\u23f0 That captcha has expired — press **Verify** again for a new one.",
                ephemeral=True)
            return
        await interaction.response.send_modal(CaptchaCodeModal())

    async def on_timeout(self):
        # Codes expire with the view; entries are cleaned lazily on next use
        for uid, (_, expiry) in list(_captcha_codes.items()):
            if expiry <= time.time():
                _captcha_codes.pop(uid, None)


class VerifyPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green,
                       emoji="\u2705", custom_id="verify_btn")
    async def verify(self, interaction: discord.Interaction, button: Button):
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        if not cfg.verification_enabled:
            await interaction.response.send_message(
                "Verification is not enabled on this server.", ephemeral=True)
            return

        member = interaction.user
        verified_role = (interaction.guild.get_role(cfg.verified_role)
                         if cfg.verified_role else None)
        if verified_role and verified_role in member.roles:
            await interaction.response.send_message(
                "You're already verified.", ephemeral=True)
            return

        if cfg.verify_mode == "click":
            # One-click mode: no challenge, straight to verified
            error = await _grant_verified_role(interaction.guild, member)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await interaction.response.send_message(
                "\u2705 You have been verified! Welcome to the server.", ephemeral=True)
            await _finish_success(interaction, member)
            return

        # Captcha mode: fresh image + ephemeral code-entry view
        buffer = _new_captcha(member.id)
        embed = discord.Embed(
            title="\U0001f9e9 Verification Captcha",
            description=(
                "Type the characters shown in the image below, then press "
                "**Enter Code** and submit them.\n\n"
                f"• Codes expire after {CAPTCHA_TTL // 60} minutes\n"
                "• Capitalization doesn't matter"
            ),
            color=0x57f287,
        )
        embed.set_image(url="attachment://captcha.png")
        file = discord.File(buffer, filename="captcha.png")
        await interaction.response.send_message(embed=embed, file=file,
                                                view=CaptchaChallengeView(), ephemeral=True)


# --- Cog ---
class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="verification", description="Configure the verification system.")
    @app_commands.describe(
        action="What to change",
        value="Required for Mode: `captcha` or `click`",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Status", value="status"),
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Set role (mention it in value)", value="role"),
        app_commands.Choice(name="Mode", value="mode"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def verification(self, interaction: discord.Interaction,
                           action: app_commands.Choice[str], value: str = None):
        guild_id = str(interaction.guild.id)
        await ensure_guild_config(guild_id)
        cfg = await TicketConfig.fetch(guild_id)

        if action.value == "status":
            role = interaction.guild.get_role(cfg.verified_role) if cfg.verified_role else None
            embed = discord.Embed(title="\U0001f39f\ufe0f Verification Status", color=0x5865f2)
            embed.add_field(name="Enabled",
                            value="\u2705 Yes" if cfg.verification_enabled else "\u274c No",
                            inline=True)
            embed.add_field(name="Mode", value=MODE_LABELS.get(cfg.verify_mode, cfg.verify_mode),
                            inline=True)
            embed.add_field(name="Verified role",
                            value=role.mention if role else "\u274c Not set", inline=True)
            embed.add_field(name="Panel channel",
                            value=interaction.guild.get_channel(cfg.verify_channel).mention
                            if cfg.verify_channel and interaction.guild.get_channel(cfg.verify_channel)
                            else "\u274c Not set", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if action.value == "enable":
            await set_guild_setting(guild_id, "verification_enabled", 1)
            role = interaction.guild.get_role(cfg.verified_role) if cfg.verified_role else None
            note = ""
            if not role:
                note = ("\n\n\u26a0\ufe0f No verified role is set — configure one with "
                        "`/set` > **Verified role**, or run `/setup auto`.")
            await interaction.response.send_message(
                "\u2705 Verification is now **enabled**." + note, ephemeral=True)
            return

        if action.value == "disable":
            await set_guild_setting(guild_id, "verification_enabled", 0)
            await interaction.response.send_message(
                "\u274c Verification is now **disabled**.", ephemeral=True)
            return

        if action.value == "role":
            if not value:
                await interaction.response.send_message(
                    "Mention the role, e.g. `@Verified` or its ID.", ephemeral=True)
                return
            import re
            match = re.match(r"^(?:<@&(\d+)>|(\d+))$", value.strip())
            raw_id = int(match.group(1) or match.group(2)) if match else None
            role = interaction.guild.get_role(raw_id) if raw_id else None
            if role is None:
                name_match = discord.utils.get(interaction.guild.roles, name=value.strip().lstrip("@"))
                role = name_match
            if role is None or role.is_default():
                await interaction.response.send_message(
                    "Couldn't find that role — mention it (`@Verified`) or paste its ID.",
                    ephemeral=True)
                return
            await set_guild_setting(guild_id, "verified_role", role.id)
            await interaction.response.send_message(
                f"\u2705 Verified role set to {role.mention}. Anyone who passes verification "
                f"will now receive it.", ephemeral=True)
            return

        if action.value == "mode":
            if not value or value.lower() not in ("captcha", "click"):
                await interaction.response.send_message(
                    "Use `captcha` (users type a code from an image) or `click` "
                    "(users just press the button).", ephemeral=True)
                return
            mode = value.lower()
            await set_guild_setting(guild_id, "verify_mode", mode)
            await interaction.response.send_message(
                f"\u2705 Verification mode set to **{MODE_LABELS[mode]}**.", ephemeral=True)
            return

    @app_commands.command(name="sendverifypanel", description="Send the verification panel (goes to the configured verify channel).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def send_verify_panel(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        await ensure_guild_config(guild_id)
        cfg = await TicketConfig.fetch(guild_id)

        channel = (interaction.guild.get_channel(cfg.verify_channel)
                   if cfg.verify_channel else interaction.channel)
        if channel is None:
            await interaction.response.send_message(
                "Configured verify channel was not found.", ephemeral=True)
            return

        if cfg.verify_mode == "click":
            instructions = "Press the button below to verify yourself instantly."
        else:
            instructions = "Press the button below and enter the captcha code to verify yourself."

        embed = discord.Embed(
            title="\U0001f39f\ufe0f Server Verification",
            description=(
                f"Welcome! To gain access to the rest of the server, {instructions}\n\n"
                "This helps us keep the server safe from bots and raids."
            ),
            color=0x57f287,
        )
        embed.set_footer(text="If the button doesn't work, ask a staff member for help.")
        await channel.send(embed=embed, view=VerifyPanelView())
        await interaction.response.send_message(f"Verification panel sent to {channel.mention}!",
                                                ephemeral=True)

    @app_commands.command(name="verifyuser", description="Manually verify a member (staff shortcut).")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def verify_user(self, interaction: discord.Interaction, member: discord.Member):
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        verified_role = (interaction.guild.get_role(cfg.verified_role)
                         if cfg.verified_role else None)
        if verified_role and verified_role in member.roles:
            await interaction.response.send_message(
                f"{member.mention} is already verified.", ephemeral=True)
            return
        error = await _grant_verified_role(interaction.guild, member)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message(
            f"\u2705 {member.mention} has been verified.", ephemeral=True)
        await _finish_success(interaction, member, manual_by=interaction.user)

    @app_commands.command(name="verificationstats", description="Show verification success/failure statistics.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def verification_stats(self, interaction: discord.Interaction):
        stats = await get_verification_stats(str(interaction.guild.id))
        total = stats["success"] + stats["failure"]
        rate = f"{stats['success'] / total * 100:.0f}%" if total else "n/a"
        embed = discord.Embed(title="\U0001f4ca Verification Statistics", color=0x5865f2)
        embed.add_field(name="\u2705 Passed", value=stats["success"], inline=True)
        embed.add_field(name="\u274c Failed", value=stats["failure"], inline=True)
        embed.add_field(name="Pass rate", value=rate, inline=True)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verification(bot))