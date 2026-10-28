"""The /setup hub — one front door for every settings panel.

Lives in its own module so every panel (auto-mod, verification, logging)
can import the hub for its "Back" button without a circular import.

Note: the leading underscore keeps main.py's auto-loader from treating
this helper module as an extension.
"""

import discord
from discord.ui import Button, Modal, Select, TextInput, View

from cog_utils import (
    TicketConfig,
    ensure_guild_config,
    get_log_ignored,
    set_guild_setting,
)
from ui_common import (
    EMOJI,
    PanelView,
    base_embed,
    deny,
    err_embed,
    ok_embed,
    parse_snowflake,
    truncate,
)
from cogs.voice import DEFAULT_TEMPLATE, _add_hub, _list_hubs


# --- Server Settings (what /set used to do, now a panel) ---
# (column, label, emoji, kind, description)
SETTABLE_SETTINGS = [
    ("log_channel", "Log channel", "\U0001f4cb", "channel",
     "Where audit/moderation logs are posted"),
    ("join_channel", "Join channel", "\U0001f7e2", "channel",
     "Channel for member join messages"),
    ("leave_channel", "Leave channel", "\U0001f534", "channel",
     "Channel for member leave messages"),
    ("ticket_panel_channel", "Ticket panel channel", "\U0001f4e9", "channel",
     "Where the ticket creation panel is posted"),
    ("transcripts_channel", "Transcripts channel", "\U0001f4c4", "channel",
     "Where closed-ticket transcripts are saved"),
    ("verify_channel", "Verify channel", "\U0001f39f\ufe0f", "channel",
     "Where the verification panel is posted"),
    ("ticket_category", "Ticket category", "\U0001f4e1", "category",
     "Category new ticket channels are created in"),
    ("ticket_support_role", "Ticket support role", "\U0001f6e1\ufe0f", "role",
     "Role that can view, claim and close tickets"),
    ("verified_role", "Verified role", "\u2705", "role",
     "Role given when a member passes verification"),
    ("join_message", "Join message", "\u270f\ufe0f", "message",
     "Sent when a member joins - {user} {member_count}"),
    ("leave_message", "Leave message", "\U0001f4dd", "message",
     "Sent when a member leaves - {user} {member_count}"),
]
SETTINGS_BY_KEY = {column: (label, emoji, kind)
                   for column, label, emoji, kind, _ in SETTABLE_SETTINGS}


def _format_message(template: str, member: discord.Member) -> str:
    """Placeholder formatting for join/leave messages (shared with events.py)."""
    return (template
            .replace("{user}", member.mention)
            .replace("{member_count}", str(member.guild.member_count))
            .replace("{count}", str(member.guild.member_count)))


def _current_value_text(guild: discord.Guild, column: str, kind: str, value) -> str:
    if not value:
        return f"{EMOJI['no']} Not set"
    if kind == "role":
        obj = guild.get_role(value)
        return f"{EMOJI['ok']} {obj.mention}" if obj else f"{EMOJI['warn']} Set, but no longer exists"
    if kind in ("channel", "category"):
        obj = guild.get_channel(value)
        if not obj:
            return f"{EMOJI['warn']} Set, but no longer exists"
        return f"{EMOJI['ok']} {obj.mention}" if kind == "channel" \
            else f"{EMOJI['ok']} {obj.name} (category)"
    return truncate(value)


async def settings_panel_embed(guild: discord.Guild) -> discord.Embed:
    cfg = await TicketConfig.fetch(str(guild.id))
    embed = base_embed("\U0001f6e0\ufe0f Server Settings", "primary",
                       "Pick a setting below to change it.")
    for column, label, emoji, kind, _ in SETTABLE_SETTINGS:
        embed.add_field(name=f"{emoji} {label}",
                        value=_current_value_text(guild, column, kind, getattr(cfg, column)),
                        inline=True)
    return embed


async def target_picker_embed(guild: discord.Guild, column: str) -> discord.Embed:
    label, emoji, kind = SETTINGS_BY_KEY[column]
    cfg = await TicketConfig.fetch(str(guild.id))
    current = _current_value_text(guild, column, kind, getattr(cfg, column))
    return base_embed(f"{emoji} Set {label}", "primary",
                      f"Current: {current}\n\nPick a new {kind} below — "
                      "or clear the setting entirely.")


class MessageSettingModal(Modal):
    """Text editor for join/leave messages. Submitting empty text clears it."""

    def __init__(self, column: str, current: str):
        label, _, _ = SETTINGS_BY_KEY[column]
        super().__init__(title=f"Set {label}", timeout=180)
        self.column = column
        self.input = TextInput(
            label="Message text",
            style=discord.TextStyle.paragraph,
            default=current or None,
            placeholder="Welcome {user}! You are member #{member_count}.",
            max_length=1000,
            required=False,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        label, _, _ = SETTINGS_BY_KEY[self.column]
        value = self.input.value.strip()
        if value:
            await set_guild_setting(guild_id, self.column, value)
            preview = _format_message(value, interaction.user)
            await interaction.response.send_message(
                embed=ok_embed(f"**{label}** updated.\nPreview: {preview}\n"
                               "Placeholders: `{user}`, `{member_count}` (also `{count}`)"),
                ephemeral=True)
        else:
            await set_guild_setting(guild_id, self.column, None)
            await interaction.response.send_message(
                embed=ok_embed(f"**{label}** cleared — the default will be used."),
                ephemeral=True)
        await interaction.edit_original_response(
            embed=await settings_panel_embed(interaction.guild),
            view=SettingsPanelView(interaction.guild))


class ManualTargetModal(Modal):
    """Fallback for when the dropdown can't list everything (25-option cap):
    type a mention, raw ID, or exact name instead."""

    def __init__(self, column: str, kind: str):
        label = SETTINGS_BY_KEY[column][0]
        super().__init__(title=f"Set {label} manually", timeout=180)
        self.column = column
        self.kind = kind
        self.input = TextInput(
            label=f"{kind.capitalize()} — mention, ID, or exact name",
            placeholder="@Support Team  or  123456789  or  Support Team",
            max_length=100,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        label = SETTINGS_BY_KEY[self.column][0]
        raw = self.input.value.strip()

        obj = None
        raw_id = parse_snowflake(raw)
        if raw_id is not None:
            obj = guild.get_role(raw_id) if self.kind == "role" \
                else guild.get_channel(raw_id)
        if obj is None:
            name = raw.lower()
            pool = (guild.roles if self.kind == "role"
                    else guild.categories if self.kind == "category"
                    else guild.text_channels)
            obj = discord.utils.find(lambda o: o.name.lower() == name, pool)

        if obj is None:
            await interaction.response.send_message(
                embed=err_embed(f"Couldn't find a {self.kind} matching `{raw}`. "
                                "Double-check the mention, ID, or exact name."),
                ephemeral=True)
            return
        if self.kind == "channel" and not isinstance(obj, discord.TextChannel):
            await interaction.response.send_message(
                embed=err_embed("That isn't a text channel — this setting needs one."),
                ephemeral=True)
            return
        if self.kind == "category" and not isinstance(obj, discord.CategoryChannel):
            await interaction.response.send_message(
                embed=err_embed("That isn't a category."), ephemeral=True)
            return

        await set_guild_setting(str(guild.id), self.column, obj.id)
        mention = obj.mention if self.kind != "category" else f"**{obj.name}**"
        await interaction.response.send_message(
            embed=ok_embed(f"**{label}** set to {mention}."), ephemeral=True)
        await interaction.edit_original_response(
            embed=await settings_panel_embed(guild),
            view=SettingsPanelView(guild))


class BackToSettingsButton(Button):
    """⬅️ Back — returns a target picker to the Server Settings panel."""

    def __init__(self, row: int = 2):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary,
                         emoji="\u2b05\ufe0f", custom_id="back_to_settings_btn", row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=await settings_panel_embed(interaction.guild),
            view=SettingsPanelView(interaction.guild))


class SettingsPanelView(PanelView):
    """Step 1: pick which setting to change."""

    def __init__(self, guild: discord.Guild):
        super().__init__()
        select = Select(
            placeholder="Pick a setting to change\u2026",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=label, value=column, emoji=emoji,
                                          description=desc[:100])
                     for column, label, emoji, kind, desc in SETTABLE_SETTINGS],
        )
        select.callback = self._pick
        self.pick_select = select
        self.add_item(select)
        self.add_item(BackToHubButton(row=2))

    async def _pick(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        column = self.pick_select.values[0]
        kind = SETTINGS_BY_KEY[column][2]
        if kind == "message":
            cfg = await TicketConfig.fetch(str(interaction.guild.id))
            await interaction.response.send_modal(
                MessageSettingModal(column, getattr(cfg, column) or ""))
        else:
            await interaction.response.edit_message(
                embed=await target_picker_embed(interaction.guild, column),
                view=TargetPickerView(interaction.guild, column, kind))


class TargetPickerView(PanelView):
    """Step 2: pick the channel/role/category — no typing mentions or IDs."""

    def __init__(self, guild: discord.Guild, column: str, kind: str):
        super().__init__()
        self.column = column
        self.kind = kind
        label = SETTINGS_BY_KEY[column][0]
        options = [discord.SelectOption(
            label="\U0001f6ab Clear this setting",
            value="__clear__",
            description="Unset it — the default behavior will be used")]
        if kind == "channel":
            pool = [(c.name, str(c.id)) for c in guild.text_channels]
        elif kind == "category":
            pool = [(c.name, str(c.id)) for c in guild.categories]
        else:
            pool = [(r.name, str(r.id)) for r in guild.roles if not r.is_default()]
        options += [discord.SelectOption(label=name[:100], value=value)
                    for name, value in pool[:24]]
        select = Select(placeholder=f"Pick a {kind} for {label}\u2026",
                        min_values=1, max_values=1, options=options[:25])
        select.callback = self._pick_target
        self.target_select = select
        self.add_item(select)
        self.add_item(BackToSettingsButton(row=2))

    @discord.ui.button(label="Enter manually", style=discord.ButtonStyle.secondary,
                       emoji="\u2328\ufe0f", custom_id="target_manual_btn", row=1)
    async def manual_entry(self, interaction: discord.Interaction, button: Button):
        """Dropdowns cap at 25 options — offer a typed fallback."""
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(
            ManualTargetModal(self.column, self.kind))

    async def _pick_target(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        guild_id = str(interaction.guild.id)
        label = SETTINGS_BY_KEY[self.column][0]
        choice = self.target_select.values[0]
        if choice == "__clear__":
            await set_guild_setting(guild_id, self.column, None)
            await interaction.response.send_message(
                embed=ok_embed(f"**{label}** cleared."), ephemeral=True)
        else:
            await set_guild_setting(guild_id, self.column, int(choice))
            obj = (interaction.guild.get_role(int(choice)) if self.kind == "role"
                   else interaction.guild.get_channel(int(choice)))
            mention = obj.mention if self.kind != "category" else f"**{obj.name}**"
            await interaction.response.send_message(
                embed=ok_embed(f"**{label}** set to {mention}."), ephemeral=True)
        await interaction.edit_original_response(
            embed=await settings_panel_embed(interaction.guild),
            view=SettingsPanelView(interaction.guild))


def _status(guild: discord.Guild, value, kind="channel"):
    if not value:
        return f"{EMOJI['no']} Not set"
    if kind == "role":
        obj = guild.get_role(value)
        return f"{EMOJI['ok']} {obj.mention}" if obj else f"{EMOJI['warn']} Set, but no longer exists"
    if kind == "onoff":
        return f"{EMOJI['ok']} Enabled" if value else f"{EMOJI['no']} Disabled"
    obj = guild.get_channel(value)
    if not obj:
        return f"{EMOJI['warn']} Set, but no longer exists"
    if kind == "category":
        if isinstance(obj, discord.CategoryChannel):
            return f"{EMOJI['ok']} {obj.name} (category)"
        return f"{EMOJI['warn']} Set, but not a category"
    return f"{EMOJI['ok']} {obj.mention}"


async def build_setup_hub_embed(guild: discord.Guild) -> discord.Embed:
    cfg = await TicketConfig.fetch(str(guild.id))
    embed = base_embed(f"{EMOJI['bolt']} Bot Setup", "primary")
    embed.add_field(name="Log channel", value=_status(guild, cfg.log_channel), inline=True)
    embed.add_field(name="Ticket category", value=_status(guild, cfg.ticket_category, "category"), inline=True)
    embed.add_field(name="Support role", value=_status(guild, cfg.ticket_support_role, "role"), inline=True)
    embed.add_field(name="Panel channel", value=_status(guild, cfg.ticket_panel_channel), inline=True)
    embed.add_field(name="Transcripts channel", value=_status(guild, cfg.transcripts_channel), inline=True)
    embed.add_field(name="Verified role", value=_status(guild, cfg.verified_role, "role"), inline=True)
    verify_value = _status(guild, cfg.verification_enabled, "onoff")
    if cfg.verification_enabled:
        mode = "Captcha" if cfg.verify_mode == "captcha" else "One-click"
        verify_value += f" ({mode})"
    embed.add_field(name="Verification", value=verify_value, inline=True)
    embed.add_field(name="Auto-Mod", value=_status(guild, cfg.automod_enabled, "onoff"), inline=True)
    hubs = await _list_hubs(str(guild.id))
    hub_channels = [c for h in hubs
                    if (c := guild.get_channel(int(h["channel_id"]))) is not None]
    embed.add_field(name="Voice hub",
                    value=f"{EMOJI['ok']} {hub_channels[0].mention} (+{len(hub_channels) - 1} more)"
                    if hub_channels else f"{EMOJI['no']} Not set",
                    inline=True)
    embed.add_field(name="Join channel", value=_status(guild, cfg.join_channel), inline=True)
    embed.add_field(name="Leave channel", value=_status(guild, cfg.leave_channel), inline=True)
    ignored = await get_log_ignored(str(guild.id))
    embed.add_field(name="Ignored channels",
                    value=f"{len(ignored)} excluded from logging" if ignored else "None",
                    inline=True)
    embed.add_field(name="Join message", value=truncate(cfg.join_message), inline=False)
    embed.add_field(name="Leave message", value=truncate(cfg.leave_message), inline=False)
    embed.add_field(
        name="Next steps",
        value="Use the buttons below — each panel covers one area, **Server Settings** "
              f"handles channels/roles/messages, and **Quick Setup** creates everything "
              "for you automatically.",
        inline=False,
    )
    return embed


class QuickSetupView(View):
    """Confirmation buttons for quick setup — frozen on timeout like all panels."""

    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes, set it up", style=discord.ButtonStyle.green,
                       emoji=EMOJI["ok"], custom_id="qs_confirm_btn")
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await deny(interaction, "Administrator")
            return
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="\u23f3 Setting everything up...", view=self)
        try:
            summary = await run_quick_setup(interaction.guild)
            await interaction.edit_original_response(content=summary, view=self)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.edit_original_response(
                content="\u274c I don't have permission to create channels/roles. "
                        "Give me the **Manage Channels** and **Manage Roles** permissions and try again.",
                view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red,
                       emoji=EMOJI["no"], custom_id="qs_cancel_btn")
    async def cancel(self, interaction: discord.Interaction, button: Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Quick setup cancelled.", view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        self.stop()


class SetupHubView(View):
    """Every button opens the matching settings panel — the same panel each
    standalone command shows. Use the panels' Back buttons to return here."""

    def __init__(self):
        super().__init__(timeout=300)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="Auto-Mod", style=discord.ButtonStyle.primary,
                       emoji=EMOJI["shield"], custom_id="hub_automod_btn", row=0)
    async def automod_panel(self, interaction: discord.Interaction, button: Button):
        from cogs.automod import AutoModPanelView, automod_status_embed
        if not interaction.user.guild_permissions.manage_guild:
            await deny(interaction)
            return
        await ensure_guild_config(str(interaction.guild.id))
        await interaction.response.send_message(
            embed=await automod_status_embed(interaction.guild),
            view=AutoModPanelView(), ephemeral=True)

    @discord.ui.button(label="Verification", style=discord.ButtonStyle.primary,
                       emoji=EMOJI["verify"], custom_id="hub_verify_btn", row=0)
    async def verification_panel(self, interaction: discord.Interaction, button: Button):
        from cogs.verification import VerificationPanelView, verification_status_embed
        if not interaction.user.guild_permissions.manage_guild:
            await deny(interaction)
            return
        await ensure_guild_config(str(interaction.guild.id))
        await interaction.response.send_message(
            embed=await verification_status_embed(interaction.guild),
            view=VerificationPanelView(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Log Settings", style=discord.ButtonStyle.primary,
                       emoji=EMOJI["logs"], custom_id="hub_logs_btn", row=0)
    async def log_panel(self, interaction: discord.Interaction, button: Button):
        from cogs.logging import LogPanelView, _status_embed
        from cog_utils import get_log_ignored
        if not interaction.user.guild_permissions.manage_guild:
            await deny(interaction)
            return
        # pre-select the channels currently excluded from logging
        ignored = await get_log_ignored(str(interaction.guild.id))
        ignored_channels = [c for i in ignored
                            if (c := interaction.guild.get_channel(int(i))) is not None]
        cfg = await TicketConfig.fetch(str(interaction.guild.id))
        await interaction.response.send_message(
            embed=await _status_embed(interaction.guild),
            view=LogPanelView(ignored_channels, debug_enabled=cfg.log_debug),
            ephemeral=True)

    @discord.ui.button(label="Server Settings", style=discord.ButtonStyle.primary,
                       emoji="\U0001f6e0\ufe0f", custom_id="hub_settings_btn", row=0)
    async def server_settings(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_guild:
            await deny(interaction)
            return
        await ensure_guild_config(str(interaction.guild.id))
        await interaction.response.send_message(
            embed=await settings_panel_embed(interaction.guild),
            view=SettingsPanelView(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Quick Setup", style=discord.ButtonStyle.success,
                       emoji=EMOJI["bolt"], custom_id="hub_quick_btn")
    async def quick_setup(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await deny(interaction, "Administrator")
            return
        await interaction.response.send_message(QUICK_SETUP_PROMPT,
                                                view=QuickSetupView(), ephemeral=True)


class BackToHubButton(Button):
    """⬅️ Back — returns any panel to the /setup hub (same message, in-place)."""

    def __init__(self, row: int = 4):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary,
                         emoji="\u2b05\ufe0f", custom_id="back_to_hub_btn", row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=await build_setup_hub_embed(interaction.guild),
            view=SetupHubView())


QUICK_SETUP_PROMPT = (
    "\U0001f6a8 **Quick Setup** will create and configure:\n"
    "\u2022 \U0001f4cb `#bot-logs` channel (logging)\n"
    "\u2022 \U0001f4c4 `#transcripts` channel (ticket transcripts)\n"
    "\u2022 \U0001f39f\ufe0f `#verify` channel (verification panel)\n"
    "\u2022 \U0001f4e9 `Tickets` category + \U0001f6e1\ufe0f `Support Team` role\n"
    "\u2022 \u2705 `Verified` role (assigned on verification)\n"
    "\u2022 \U0001f50a `\u2795 Join to Create` voice hub (temp voice channels)\n"
    "Existing settings you already configured are left untouched. Continue?"
)
# Panels get posted with /sendticketpanel and /sendverifypanel afterwards.


async def run_quick_setup(guild: discord.Guild) -> str:
    """Create/reuse the standard channels, roles, category and voice hub; persist the config."""
    guild_id = str(guild.id)
    await ensure_guild_config(guild_id)
    cfg = await TicketConfig.fetch(guild_id)
    created, reused = [], []

    def overwrites():
        perms = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        bot_perms = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        return {guild.default_role: perms, guild.me: bot_perms}

    async def get_or_create_channel(name, **kwargs):
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            reused.append(f"#{name}")
            return existing
        ch = await guild.create_text_channel(name, **kwargs)
        created.append(f"#{name}")
        return ch

    async def get_or_create_role(name, **kwargs):
        existing = discord.utils.get(guild.roles, name=name)
        if existing:
            reused.append(f"@{name}")
            return existing
        role = await guild.create_role(name=name, **kwargs)
        created.append(f"@{name}")
        return role

    # Channels & roles (only fill in what's missing)
    log_ch = guild.get_channel(cfg.log_channel) if cfg.log_channel else None
    if not log_ch:
        log_ch = await get_or_create_channel("bot-logs", overwrites=overwrites())

    transcripts_ch = guild.get_channel(cfg.transcripts_channel) if cfg.transcripts_channel else None
    if not transcripts_ch:
        transcripts_ch = await get_or_create_channel("transcripts", overwrites=overwrites())

    verify_ch = guild.get_channel(cfg.verify_channel) if cfg.verify_channel else None
    if not verify_ch:
        verify_ch = await get_or_create_channel("verify")

    support_role = guild.get_role(cfg.ticket_support_role) if cfg.ticket_support_role else None
    if not support_role:
        support_role = await get_or_create_role("Support Team", color=discord.Color.blue())

    verified_role = guild.get_role(cfg.verified_role) if cfg.verified_role else None
    if not verified_role:
        verified_role = await get_or_create_role("Verified", color=discord.Color.green())

    category = guild.get_channel(cfg.ticket_category) if cfg.ticket_category else None
    if not (category and isinstance(category, discord.CategoryChannel)):
        category = await guild.create_category("Tickets")
        created.append("Tickets (category)")

    # Voice hub: create only if the server has none
    hubs = await _list_hubs(guild_id)
    existing_hubs = [c for h in hubs
                     if (c := guild.get_channel(int(h["channel_id"]))) is not None]
    if not existing_hubs:
        hub_channel = await guild.create_voice_channel(
            "\u2795 Join to Create", reason="Quick setup voice hub")
        await _add_hub(guild_id, str(hub_channel.id), DEFAULT_TEMPLATE)
        created.append("\u2795 Join to Create (voice hub)")

    # Persist everything
    updates = {
        "log_channel": log_ch.id,
        "transcripts_channel": transcripts_ch.id,
        "verify_channel": verify_ch.id,
        "ticket_support_role": support_role.id,
        "verified_role": verified_role.id,
        "ticket_category": category.id,
        "verification_enabled": 1,
    }
    for column, value in updates.items():
        await set_guild_setting(guild_id, column, value)

    lines = [f"{EMOJI['ok']} **Quick setup complete!**"]
    if created:
        lines.append("Created: " + ", ".join(created))
    if reused:
        lines.append("Reused: " + ", ".join(reused))
    lines.append("\nNext: run `/sendticketpanel` and `/sendverifypanel` to post the panels.")
    return "\n".join(lines)
