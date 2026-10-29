import asyncio
import datetime
import io
import random
import string
import time

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Modal, Select, TextInput, View
from PIL import Image, ImageDraw, ImageFont

from cog_utils import (
    TicketConfig,
    ensure_guild_config,
    get_verification_stats,
    log_event,
    record_verification_attempt,
    set_guild_setting,
)
from ui_common import (
    EMOJI,
    PanelView,
    base_embed,
    ok_embed,
    yn,
)
from cogs._hub import BackToHubButton

CAPTCHA_TTL = 300  # seconds before a captcha code expires
FAILED_ATTEMPT_TTL = 15 * 60  # reset consecutive failures after 15 minutes
CAPTCHA_LENGTH = 5
MODE_LABELS = {"captcha": "\U0001f9e9 Captcha", "click": "\U0001f446 One-click"}

# (guild_id, user_id) -> (code, expiry_timestamp); codes are single-use.
# Guild ID is part of the key so verification in one server never overwrites
# verification in another.
_captcha_codes: dict[tuple[int, int], tuple[str, float]] = {}

# (guild_id, user_id) -> (consecutive failures, expiry_timestamp)
_failed_attempts: dict[tuple[int, int], tuple[int, float]] = {}
MAX_FAILED_ATTEMPTS = 3


def _captcha_key(guild_id: int, user_id: int) -> tuple[int, int]:
    return guild_id, user_id


def _prune_expired_state(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for key, (_, expiry) in list(_captcha_codes.items()):
        if expiry <= now:
            _captcha_codes.pop(key, None)

    for key, (_, expiry) in list(_failed_attempts.items()):
        if expiry <= now:
            _failed_attempts.pop(key, None)


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


async def _new_captcha(guild_id: int, user_id: int) -> io.BytesIO:
    """Generate off the event loop, then store a guild-scoped challenge."""
    code, buffer = await asyncio.to_thread(_generate_captcha)
    _prune_expired_state()
    _captcha_codes[_captcha_key(guild_id, user_id)] = (
        code,
        time.time() + CAPTCHA_TTL,
    )
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
        return ("No verified role is configured — set one in the **Verification** panel "
                "via `/setup`, or run `/setup-auto`.")
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
    """Shared success path: DB + log (role grant happens before this)."""
    guild_id = str(interaction.guild.id)
    await record_verification_attempt(guild_id, str(member.id), True)
    _failed_attempts.pop(_captcha_key(interaction.guild.id, member.id), None)
    title = "\u2705 Manually Verified" if manual_by else "\u2705 Verification Passed"
    detail = f"Verified by {manual_by} (`{manual_by.id}`)" if manual_by else None
    await log_event(interaction.guild, "verify_pass", _verification_log(
        interaction.guild, title, 0x57f287 if not manual_by else 0x5865f2, member, detail))


async def _finish_failure(guild: discord.Guild, member: discord.Member,
                          detail_prefix: str = None):
    """Shared failure path: count, log, return the current consecutive-fail count."""
    key = _captcha_key(guild.id, member.id)
    now = time.time()
    previous_fails, previous_expiry = _failed_attempts.get(key, (0, now))
    if previous_expiry <= now:
        previous_fails = 0

    fails = previous_fails + 1
    _failed_attempts[key] = (fails, now + FAILED_ATTEMPT_TTL)
    detail = detail_prefix or ""
    if fails >= MAX_FAILED_ATTEMPTS:
        detail += (f"\u26a0\ufe0f **{fails} consecutive failures** — user may be a bot "
                   "or failing the check repeatedly.")
    else:
        detail += f"Consecutive failures: **{fails}**"
    await log_event(guild, "verify_fail", _verification_log(
        guild, "\u274c Verification Failed", 0xed4245, member, detail.strip()))
    return fails


# --- Status embed (shared by /verification and the /setup hub) ---
async def verification_status_embed(guild: discord.Guild) -> discord.Embed:
    cfg = await TicketConfig.fetch(str(guild.id))
    role = guild.get_role(cfg.verified_role) if cfg.verified_role else None
    channel = guild.get_channel(cfg.verify_channel) if cfg.verify_channel else None
    embed = base_embed(f"{EMOJI['verify']} Verification", "primary")
    embed.add_field(name="Enabled", value=yn(cfg.verification_enabled), inline=True)
    embed.add_field(name="Mode",
                    value=MODE_LABELS.get(cfg.verify_mode, cfg.verify_mode), inline=True)
    embed.add_field(name="Verified role",
                    value=role.mention if role else f"{EMOJI['no']} Not set", inline=True)
    embed.add_field(name="Panel channel",
                    value=channel.mention if channel else f"{EMOJI['no']} Not set",
                    inline=True)
    return embed


# --- Public, persistent verify panel (posted with /sendverifypanel) ---
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

        _prune_expired_state()
        stored = _captcha_codes.pop(
            _captcha_key(guild.id, member.id),
            None,
        )
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
            await interaction.response.send_message(
                "\u2705 Correct! You have been verified. Welcome to the server.",
                ephemeral=True)
            await _finish_success(interaction, member)
        else:
            await record_verification_attempt(str(guild.id), str(member.id), False)
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
        _prune_expired_state()
        stored = _captcha_codes.get(
            _captcha_key(interaction.guild.id, interaction.user.id)
        )
        if stored is None or time.time() > stored[1]:
            await interaction.response.send_message(
                "\u23f0 That captcha has expired — press **Verify** again for a new one.",
                ephemeral=True)
            return
        await interaction.response.send_modal(CaptchaCodeModal())

    async def on_timeout(self):
        # Codes expire with the view; entries are cleaned lazily on next use
        _prune_expired_state()


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
        buffer = await _new_captcha(interaction.guild.id, member.id)
        embed = discord.Embed(
            title="\U0001f9e9 Verification Captcha",
            description=(
                "Type the characters shown in the image below, then press "
                "**Enter Code** and submit them.\n\n"
                f"\u2022 Codes expire after {CAPTCHA_TTL // 60} minutes\n"
                "\u2022 Capitalization doesn't matter"
            ),
            color=0x57f287,
        )
        embed.set_image(url="attachment://captcha.png")
        file = discord.File(buffer, filename="captcha.png")
        await interaction.response.send_message(embed=embed, file=file,
                                                view=CaptchaChallengeView(), ephemeral=True)


# --- Config panel: same pattern as the auto-mod and logging panels ---
class VerificationPanelView(PanelView):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self._build_select(guild)
        self.add_item(BackToHubButton(row=2))

    def _build_select(self, guild: discord.Guild):
        options = [
            discord.SelectOption(label="Mode: Captcha (type a code)",
                                 value="mode:captcha", emoji="\U0001f9e9"),
            discord.SelectOption(label="Mode: One-click (no code)",
                                 value="mode:click", emoji="\U0001f446"),
        ]
        # Roles straight in the dropdown — no typing mentions or IDs
        for role in [r for r in reversed(guild.roles)
                     if not r.is_default() and not r.managed][:23]:
            options.append(discord.SelectOption(
                label=f"Verified role: {role.name}"[:100],
                value=f"role:{role.id}", emoji="\U0001f3ad"))
        select = Select(placeholder="Change mode or pick the verified role\u2026",
                        min_values=1, max_values=1, options=options)
        select.callback = self._select_callback
        self.setting_select = select
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        choice = self.setting_select.values[0]
        kind, _, payload = choice.partition(":")

        if kind == "mode":
            await set_guild_setting(guild_id, "verify_mode", payload)
            await interaction.response.send_message(
                embed=ok_embed(f"Verification mode set to **{MODE_LABELS[payload]}**."),
                ephemeral=True)
        else:  # role
            role = interaction.guild.get_role(int(payload))
            if role is None:
                await interaction.response.send_message(
                    embed=ok_embed("That role no longer exists. Pick another role."),
                    ephemeral=True,
                )
            else:
                await set_guild_setting(guild_id, "verified_role", role.id)
                await interaction.response.send_message(
                    embed=ok_embed(
                        f"Verified role set to {role.mention}. Anyone who passes "
                        "verification will now receive it."
                    ),
                    ephemeral=True,
                )
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        try:
            view = VerificationPanelView(interaction.guild)
            await interaction.edit_original_response(
                embed=await verification_status_embed(interaction.guild), view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @discord.ui.button(label="Toggle Verification", style=discord.ButtonStyle.primary,
                       emoji=EMOJI["verify"], custom_id="vf_toggle_btn", row=0)
    async def toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        cfg = await TicketConfig.fetch(guild_id)
        await set_guild_setting(guild_id, "verification_enabled",
                                0 if cfg.verification_enabled else 1)
        if cfg.verification_enabled:
            await interaction.response.send_message(
                embed=ok_embed("Verification **disabled**."), ephemeral=True)
        else:
            role = (interaction.guild.get_role(cfg.verified_role)
                    if cfg.verified_role else None)
            note = "" if role else ("\n\n\u26a0\ufe0f No verified role set — pick one in "
                                    "the dropdown below.")
            await interaction.response.send_message(
                embed=ok_embed("Verification **enabled**." + note), ephemeral=True)
        await self._refresh(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.success,
                       emoji=EMOJI["refresh"], custom_id="vf_refresh_btn", row=0)
    async def refresh_btn(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(
            embed=await verification_status_embed(interaction.guild),
            view=VerificationPanelView(interaction.guild))


# --- Cog ---
class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        try:
            await channel.send(embed=embed, view=VerifyPanelView())
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't send the verification panel. Check the channel permissions and try again.",
                ephemeral=True,
            )
            return

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
        embed = base_embed(f"{EMOJI['verify']} Verification Statistics", "primary")
        embed.add_field(name="\u2705 Passed", value=stats["success"], inline=True)
        embed.add_field(name="\u274c Failed", value=stats["failure"], inline=True)
        embed.add_field(name="Pass rate", value=rate, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verification(bot))
