import datetime
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import DB_FILE, log_event
from ui_common import base_embed, err_embed, ok_embed, parse_snowflake, truncate


MAX_MENU_ROLES = 25
MENU_TIMEOUT = 300


async def fetch_menu(guild_id: str, menu: str):
    """Fetch a menu by numeric ID or exact name."""
    async with aiosqlite.connect(DB_FILE) as db:
        if menu.strip().isdigit():
            async with db.execute(
                "SELECT * FROM reaction_menus WHERE guild_id = ? AND menu_id = ?",
                (guild_id, int(menu)),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT * FROM reaction_menus WHERE guild_id = ? AND lower(name) = lower(?)",
                (guild_id, menu.strip()),
            ) as cursor:
                row = await cursor.fetchone()

    if row is None:
        return None
    keys = [
        "menu_id", "guild_id", "channel_id", "message_id", "name", "description",
        "style", "mode", "created_by", "created_at",
    ]
    return dict(zip(keys, row))


async def fetch_menu_roles(menu_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT role_id, label, emoji, description, position "
            "FROM reaction_menu_roles WHERE menu_id = ? ORDER BY position, label",
            (menu_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    keys = ["role_id", "label", "emoji", "description", "position"]
    return [dict(zip(keys, row)) for row in rows]


async def menu_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT menu_id, name FROM reaction_menus WHERE guild_id = ? "
            "AND lower(name) LIKE lower(?) ORDER BY name LIMIT 25",
            (str(interaction.guild.id), f"%{current.strip()}%"),
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        app_commands.Choice(name=name, value=str(menu_id))
        for menu_id, name in rows
    ]


class RoleToggleButton(discord.ui.Button):
    def __init__(self, menu_id: int, role: dict, row: int = 0):
        super().__init__(
            label=role["label"][:80],
            emoji=role["emoji"] or None,
            style=discord.ButtonStyle.secondary,
            custom_id=f"reaction_role:{menu_id}:{role['role_id']}",
            row=row,
        )
        self.menu_id = menu_id
        self.role_id = int(role["role_id"])

    async def callback(self, interaction: discord.Interaction):
        await toggle_menu_role(interaction, self.menu_id, self.role_id)


class RoleMenuSelect(discord.ui.Select):
    def __init__(self, menu_id: int, mode: str, roles: list[dict]):
        options = [
            discord.SelectOption(
                label=role["label"][:100],
                value=role["role_id"],
                description=truncate(role["description"] or "Toggle this role", 100),
                emoji=role["emoji"] or None,
            )
            for role in roles[:25]
        ]
        super().__init__(
            placeholder="Select role(s)…",
            min_values=1,
            max_values=1 if mode == "exclusive" else min(len(options), 25),
            options=options,
            custom_id=f"reaction_role_menu:{menu_id}",
            row=0,
        )
        self.menu_id = menu_id
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        await update_menu_roles(
            interaction,
            self.menu_id,
            [int(value) for value in self.values],
            self.mode,
        )


class ReactionMenuView(discord.ui.View):
    def __init__(self, menu: dict, roles: list[dict], *, persistent: bool = False):
        super().__init__(timeout=None if persistent else MENU_TIMEOUT)
        self.menu = menu
        self.roles = roles[:MAX_MENU_ROLES]

        if menu["style"] == "dropdown":
            self.add_item(RoleMenuSelect(menu["menu_id"], menu["mode"], self.roles))
        else:
            for index, role in enumerate(self.roles):
                self.add_item(RoleToggleButton(
                    menu["menu_id"],
                    role,
                    row=index // 5,
                ))


async def build_menu_embed(menu: dict, roles: list[dict]) -> discord.Embed:
    """Posted-menu embed. Deliberately shows ONLY the title, the custom
    description and the mode hint — no auto-generated role list — so the
    description has full control over layout (write your own emoji lines)."""
    mode_text = {
        "toggle": "Click a role to add or remove it.",
        "exclusive": "Selecting a role replaces the other roles from this menu.",
    }[menu["mode"]]

    embed = base_embed(
        f"🎭 {menu['name']}",
        "primary",
        truncate(menu["description"] or "Pick a role below.", 4000),
    )
    embed.add_field(name="How it works", value=mode_text, inline=False)
    return embed


async def build_menu_view(menu_id: int, *, persistent: bool = False) -> ReactionMenuView | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT * FROM reaction_menus WHERE menu_id = ?", (menu_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None

    keys = [
        "menu_id", "guild_id", "channel_id", "message_id", "name", "description",
        "style", "mode", "created_by", "created_at",
    ]
    menu = dict(zip(keys, row))
    roles = await fetch_menu_roles(menu_id)
    return ReactionMenuView(menu, roles, persistent=persistent)


async def refresh_posted_menu(bot: commands.Bot, menu_id: int):
    menu = await fetch_menu_by_id(menu_id)
    if not menu or not menu["message_id"] or not menu["channel_id"]:
        return
    guild = bot.get_guild(int(menu["guild_id"]))
    if guild is None:
        return
    channel = guild.get_channel(int(menu["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(int(menu["message_id"]))
        roles = await fetch_menu_roles(menu_id)
        view = ReactionMenuView(menu, roles, persistent=True)
        # Register the latest set of custom IDs so newly added role buttons or
        # options continue working after the original view times out/restarts.
        bot.add_view(view)
        await message.edit(
            embed=await build_menu_embed(menu, roles),
            view=view,
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def fetch_menu_by_id(menu_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT * FROM reaction_menus WHERE menu_id = ?",
            (menu_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    keys = [
        "menu_id", "guild_id", "channel_id", "message_id", "name", "description",
        "style", "mode", "created_by", "created_at",
    ]
    return dict(zip(keys, row))


async def toggle_menu_role(interaction: discord.Interaction,
                           menu_id: int,
                           role_id: int):
    menu = await fetch_menu_by_id(menu_id)
    if menu is None or interaction.guild.id != int(menu["guild_id"]):
        await interaction.response.send_message("That role menu no longer exists.", ephemeral=True)
        return

    guild = interaction.guild
    member = guild.get_member(interaction.user.id)
    role = guild.get_role(role_id)
    if member is None or role is None:
        await interaction.response.send_message("That role or member no longer exists.", ephemeral=True)
        return

    linked_roles = [int(item["role_id"]) for item in await fetch_menu_roles(menu_id)]

    try:
        if menu["mode"] == "exclusive":
            for linked_id in linked_roles:
                linked_role = guild.get_role(linked_id)
                if linked_role and linked_role in member.roles and linked_role != role:
                    await member.remove_roles(linked_role, reason="Exclusive reaction-role menu")

        if role in member.roles:
            await member.remove_roles(role, reason="Reaction-role menu")
            state = "removed"
        else:
            await member.add_roles(role, reason="Reaction-role menu")
            state = "added"
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(
            "I couldn't update that role. Check my role position and permissions.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=ok_embed(f"{role.mention} was {state}."),
        ephemeral=True,
    )


async def update_menu_roles(interaction: discord.Interaction,
                            menu_id: int,
                            selected_role_ids: list[int],
                            mode: str):
    menu = await fetch_menu_by_id(menu_id)
    if menu is None or interaction.guild.id != int(menu["guild_id"]):
        await interaction.response.send_message("That role menu no longer exists.", ephemeral=True)
        return

    guild = interaction.guild
    member = guild.get_member(interaction.user.id)
    if member is None:
        await interaction.response.send_message("I couldn't find your member data.", ephemeral=True)
        return

    linked_roles = await fetch_menu_roles(menu_id)
    linked_ids = [int(item["role_id"]) for item in linked_roles]
    # A dropdown selection always represents the user's desired role set.
    # Individual buttons handle add/remove toggling.
    desired = set(selected_role_ids)

    try:
        for role_id in linked_ids:
            role = guild.get_role(role_id)
            if role is None:
                continue
            should_have = role_id in desired
            currently_has = role in member.roles
            if should_have and not currently_has:
                await member.add_roles(role, reason="Reaction-role menu")
            elif not should_have and currently_has:
                await member.remove_roles(role, reason="Reaction-role menu")
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(
            "I couldn't update those roles. Check my role position and permissions.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=ok_embed("Your menu roles have been updated."),
        ephemeral=True,
    )


async def get_persistent_views() -> list[ReactionMenuView]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT menu_id FROM reaction_menus") as cursor:
            menu_ids = [row[0] for row in await cursor.fetchall()]

    views = []
    for menu_id in menu_ids:
        view = await build_menu_view(menu_id, persistent=True)
        if view is not None:
            views.append(view)
    return views


# ------------------------- Guided setup wizard -------------------------

STYLE_OPTIONS = {
    "buttons": ("🔘 Buttons", "One clickable button per role."),
    "dropdown": ("🔽 Dropdown", "All roles live inside a single select menu."),
}
MODE_OPTIONS = {
    "toggle": ("🔄 Toggle", "Members can stack multiple roles from this menu."),
    "exclusive": ("☝️ Exclusive", "Picking a role removes the others from this menu."),
}

def _valid_emoji(value: str) -> bool:
    """Loose sanity check only — Discord itself is the real validator at post
    time, and finish_wizard cleans up if a bad emoji slips through."""
    if not value:
        return True
    value = value.strip()
    if value.startswith("<"):
        return discord.PartialEmoji.from_str(value).is_custom_emoji()
    return len(value) <= 32 and not any(ch.isspace() for ch in value)


def _menu_role_error(guild: discord.Guild, role: discord.Role,
                     current: list[dict]) -> Optional[str]:
    """Shared validation for roles added to a menu."""
    if role == guild.default_role or role.managed:
        return "Pick a normal, non-managed role."
    if guild.me is None or guild.me.top_role <= role:
        return "That role is above my highest role, so I can't assign it."
    if any(int(item["role_id"]) == role.id for item in current):
        return "That role is already on this menu."
    if len(current) >= MAX_MENU_ROLES:
        return f"A menu can hold at most {MAX_MENU_ROLES} roles."
    return None


def _find_role(guild: discord.Guild, raw: str) -> Optional[discord.Role]:
    """Resolve a role from a mention, an ID, an exact name, or a unique prefix."""
    parsed = parse_snowflake(raw.strip())
    if parsed is not None:
        return guild.get_role(parsed)
    lowered = raw.strip().lower()
    if not lowered:
        return None
    for role in guild.roles:
        if role.name.lower() == lowered:
            return role
    matches = [role for role in guild.roles if role.name.lower().startswith(lowered)]
    return matches[0] if len(matches) == 1 else None


async def _log_menu_event(guild: discord.Guild, user: discord.Member,
                          title: str, description: str,
                          channel: Optional[discord.abc.GuildChannel] = None):
    """Reaction-menu audit log in the same shape as the moderation logs."""
    embed = discord.Embed(title=title, description=description,
                          color=discord.Color.blurple())
    embed.add_field(name="Moderator", value=user.mention, inline=True)
    if channel is not None:
        embed.add_field(name="Channel",
                        value=getattr(channel, "mention", str(channel)), inline=True)
    await log_event(guild, "reaction_menu_update", embed)


class MenuWizard:
    """In-memory state carried through every step of the builder."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild,
                 name: str, description: Optional[str]):
        self.bot = bot
        self.guild = guild
        self.name = name
        self.description = description
        self.style = "buttons"
        self.mode = "toggle"
        self.roles: list[dict] = []
        self.channel_id: Optional[int] = None

    def preview_menu(self) -> dict:
        return {"name": self.name, "description": self.description, "mode": self.mode}

    def summary_embed(self, step: str) -> discord.Embed:
        embed = base_embed(f"🎭 Menu Wizard — {step}", "primary")
        embed.description = f"**{self.name}**" + (f"\n{self.description}" if self.description else "")
        embed.add_field(name="Style", value=STYLE_OPTIONS[self.style][0], inline=True)
        embed.add_field(name="Mode", value=MODE_OPTIONS[self.mode][0], inline=True)
        if self.roles:
            lines = []
            for item in self.roles:
                emoji = f"{item['emoji']} " if item["emoji"] else ""
                lines.append(f"{emoji}**{item['label']}**")
            embed.add_field(
                name=f"Roles ({len(self.roles)}/{MAX_MENU_ROLES})",
                value=truncate("\n".join(lines), 1024),
                inline=False,
            )
        return embed


class WizardNameModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot):
        super().__init__(title="New Reaction-Role Menu", timeout=300)
        self.bot = bot
        self.name_input = discord.ui.TextInput(
            label="Menu name",
            placeholder="Color roles",
            max_length=80,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (full control of the menu text)",
            placeholder="Write anything here — intro, your own emoji lines, rules…",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.add_item(self.name_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        wizard = MenuWizard(
            self.bot,
            interaction.guild,
            self.name_input.value.strip(),
            self.desc_input.value.strip() or None,
        )
        view = MenuWizardConfigView(wizard)
        await interaction.response.send_message(
            embed=wizard.summary_embed("Look & Behavior"), view=view
        )


class WizardStyleSelect(discord.ui.Select):
    def __init__(self, wizard: MenuWizard):
        self.wizard = wizard
        super().__init__(
            placeholder="How should the menu look?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label, value=key, description=desc,
                    default=key == wizard.style,
                )
                for key, (label, desc) in STYLE_OPTIONS.items()
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not await self.view.guard(interaction):
            return
        self.wizard.style = self.values[0]
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Look & Behavior"),
            view=MenuWizardConfigView(self.wizard),
        )


class WizardModeSelect(discord.ui.Select):
    def __init__(self, wizard: MenuWizard):
        self.wizard = wizard
        super().__init__(
            placeholder="How should roles behave?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label, value=key, description=desc,
                    default=key == wizard.mode,
                )
                for key, (label, desc) in MODE_OPTIONS.items()
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if not await self.view.guard(interaction):
            return
        self.wizard.mode = self.values[0]
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Look & Behavior"),
            view=MenuWizardConfigView(self.wizard),
        )


class MenuWizardConfigView(discord.ui.View):
    def __init__(self, wizard: MenuWizard):
        super().__init__(timeout=600)
        self.wizard = wizard
        self.add_item(WizardStyleSelect(wizard))
        self.add_item(WizardModeSelect(wizard))

    async def guard(self, interaction: discord.Interaction) -> bool:
        allowed = getattr(interaction.user.guild_permissions, "manage_roles", False)
        if not allowed:
            await interaction.response.send_message(
                "❌ You need the **Manage Roles** permission to use this.",
                ephemeral=True,
            )
        return allowed

    @discord.ui.button(label="Continue ➡️", style=discord.ButtonStyle.success, row=2)
    async def continue_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Roles"),
            view=MenuWizardRolesView(self.wizard),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=base_embed("🎭 Menu Wizard", "neutral",
                             "Setup cancelled — nothing was created."),
            view=None,
        )


class WizardRoleSelect(discord.ui.RoleSelect):
    def __init__(self, wizard: MenuWizard):
        super().__init__(
            placeholder="Pick a role to add…",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.wizard = wizard

    async def callback(self, interaction: discord.Interaction):
        if not await self.view.guard(interaction):
            return
        role = self.values[0]
        error = _menu_role_error(interaction.guild, role, self.wizard.roles)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        # Add instantly with sensible defaults; the entry can be polished
        # afterwards via the edit select (emoji, label, description).
        self.wizard.roles.append({
            "role_id": str(role.id),
            "label": role.name,
            "emoji": None,
            "description": None,
        })
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Roles"),
            view=MenuWizardRolesView(self.wizard),
        )


class RoleDetailsModal(discord.ui.Modal):
    """Edit label / emoji / description of an entry already in the wizard."""

    def __init__(self, wizard: MenuWizard, index: int):
        entry = wizard.roles[index]
        role = wizard.guild.get_role(int(entry["role_id"]))
        super().__init__(title=f"Edit {entry['label']}"[:45], timeout=180)
        self.wizard = wizard
        self.index = index
        self.fallback_label = role.name if role else entry["label"]
        self.label_input = discord.ui.TextInput(
            label="Button / option label",
            default=entry["label"][:80],
            placeholder=self.fallback_label[:100],
            max_length=80,
            required=False,
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji (optional)",
            default=entry["emoji"] or "",
            placeholder="🎮 or <:name:id>",
            max_length=100,
            required=False,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (optional)",
            default=entry["description"] or "",
            placeholder="Shown under the option in dropdowns",
            style=discord.TextStyle.paragraph,
            max_length=100,
            required=False,
        )
        self.add_item(self.label_input)
        self.add_item(self.emoji_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        emoji = self.emoji_input.value.strip()
        if not _valid_emoji(emoji):
            await interaction.response.send_message(
                embed=err_embed(
                    "That doesn't look like a valid emoji. Use a unicode emoji or "
                    "a custom emoji from this server."
                ),
                ephemeral=True,
            )
            return
        entry = self.wizard.roles[self.index]
        entry["label"] = self.label_input.value.strip() or self.fallback_label
        entry["emoji"] = emoji or None
        entry["description"] = self.desc_input.value.strip() or None
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Roles"),
            view=MenuWizardRolesView(self.wizard),
        )


class WizardEditSelect(discord.ui.Select):
    """Pick an existing entry to edit (emoji, label, description)."""

    def __init__(self, wizard: MenuWizard):
        self.wizard = wizard
        options = [
            discord.SelectOption(
                label=item["label"][:100],
                value=str(index),
                emoji=item["emoji"] or None,
                description="Edit emoji / label / description",
            )
            for index, item in enumerate(wizard.roles[:25])
        ]
        super().__init__(
            placeholder=f"✏️ Edit an entry ({len(wizard.roles)} added)…",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No roles yet", value="none")],
            row=2,
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not await self.view.guard(interaction):
            return
        if self.values[0] == "none":
            await interaction.response.defer_update()
            return
        await interaction.response.send_modal(
            RoleDetailsModal(self.wizard, int(self.values[0]))
        )


class WizardRemoveSelect(discord.ui.Select):
    def __init__(self, wizard: MenuWizard):
        self.wizard = wizard
        options = [
            discord.SelectOption(
                label=item["label"][:100],
                value=str(index),
                emoji=item["emoji"] or None,
                description="Remove this role",
            )
            for index, item in enumerate(wizard.roles[:25])
        ]
        super().__init__(
            placeholder=f"Remove a role ({len(wizard.roles)} added)…",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No roles yet", value="none")],
            row=1,
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not await self.view.guard(interaction):
            return
        if self.values[0] != "none":
            self.wizard.roles.pop(int(self.values[0]))
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Roles"),
            view=MenuWizardRolesView(self.wizard),
        )


class MenuWizardRolesView(discord.ui.View):
    def __init__(self, wizard: MenuWizard):
        super().__init__(timeout=600)
        self.wizard = wizard
        self.add_item(WizardRoleSelect(wizard))
        self.add_item(WizardRemoveSelect(wizard))
        self.add_item(WizardEditSelect(wizard))

    async def guard(self, interaction: discord.Interaction) -> bool:
        allowed = getattr(interaction.user.guild_permissions, "manage_roles", False)
        if not allowed:
            await interaction.response.send_message(
                "❌ You need the **Manage Roles** permission to use this.",
                ephemeral=True,
            )
        return allowed

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Look & Behavior"),
            view=MenuWizardConfigView(self.wizard),
        )

    @discord.ui.button(label="Continue ➡️", style=discord.ButtonStyle.success, row=3)
    async def continue_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        if not self.wizard.roles:
            await interaction.response.send_message(
                "Add at least one role before continuing.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=await MenuWizardRolesView.build_preview_embed(self.wizard),
            view=MenuWizardChannelView(self.wizard),
        )

    @staticmethod
    async def build_preview_embed(wizard: MenuWizard) -> discord.Embed:
        embed = await build_menu_embed(wizard.preview_menu(), wizard.roles)
        embed.title = "🎭 Menu Wizard — Preview & Post"
        embed.set_footer(text="Pick a channel below, then hit Post.")
        return embed

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=base_embed("🎭 Menu Wizard", "neutral",
                             "Setup cancelled — nothing was created."),
            view=None,
        )


class WizardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: MenuWizard):
        super().__init__(
            placeholder="Choose a channel to post in…",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.wizard = wizard

    async def callback(self, interaction: discord.Interaction):
        if not await self.view.guard(interaction):
            return
        self.wizard.channel_id = self.values[0].id
        channel = interaction.guild.get_channel(self.values[0].id)
        embed = await MenuWizardRolesView.build_preview_embed(self.wizard)
        embed.add_field(
            name="Target channel",
            value=channel.mention if channel else "Unknown channel",
            inline=False,
        )
        await interaction.response.edit_message(embed=embed, view=self.view)


class MenuWizardChannelView(discord.ui.View):
    def __init__(self, wizard: MenuWizard):
        super().__init__(timeout=600)
        self.wizard = wizard
        self.add_item(WizardChannelSelect(wizard))

    async def guard(self, interaction: discord.Interaction) -> bool:
        allowed = getattr(interaction.user.guild_permissions, "manage_roles", False)
        if not allowed:
            await interaction.response.send_message(
                "❌ You need the **Manage Roles** permission to use this.",
                ephemeral=True,
            )
        return allowed

    @discord.ui.button(label="📩 Post menu", style=discord.ButtonStyle.success, row=1)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        if self.wizard.channel_id is None:
            await interaction.response.send_message(
                "Pick a channel first.", ephemeral=True
            )
            return
        await finish_wizard(interaction, self.wizard)

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(
            embed=self.wizard.summary_embed("Roles"),
            view=MenuWizardRolesView(self.wizard),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=base_embed("🎭 Menu Wizard", "neutral",
                             "Setup cancelled — nothing was created."),
            view=None,
        )


async def finish_wizard(interaction: discord.Interaction, wizard: MenuWizard):
    """Create the menu row, post the message, and wire everything up."""
    guild = interaction.guild
    channel = guild.get_channel(wizard.channel_id)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            embed=err_embed("That channel no longer exists. Go back and pick another one."),
            ephemeral=True,
        )
        return

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO reaction_menus (guild_id, name, description, style, mode, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(guild.id),
                    wizard.name,
                    wizard.description,
                    wizard.style,
                    wizard.mode,
                    str(interaction.user.id),
                    str(discord.utils.utcnow()),
                ),
            )
            for position, item in enumerate(wizard.roles, start=1):
                await db.execute(
                    "INSERT INTO reaction_menu_roles "
                    "(menu_id, role_id, label, emoji, description, position) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cursor.lastrowid,
                        item["role_id"],
                        item["label"],
                        item["emoji"],
                        item["description"],
                        position,
                    ),
                )
            await db.commit()
            menu_id = cursor.lastrowid
    except aiosqlite.IntegrityError:
        await interaction.response.send_message(
            embed=err_embed(
                f"A menu named **{wizard.name}** already exists. "
                "Delete it first or pick another name."
            ),
            ephemeral=True,
        )
        return

    menu = await fetch_menu_by_id(menu_id)
    roles = await fetch_menu_roles(menu_id)
    view = ReactionMenuView(menu, roles, persistent=True)
    wizard.bot.add_view(view)

    try:
        message = await channel.send(
            embed=await build_menu_embed(menu, roles),
            view=view,
        )
    except (discord.Forbidden, discord.HTTPException):
        # Don't leave a half-created menu behind if the post fails
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM reaction_menus WHERE menu_id = ?", (menu_id,)
            )
            await db.execute(
                "DELETE FROM reaction_menu_roles WHERE menu_id = ?", (menu_id,)
            )
            await db.commit()
        await interaction.response.send_message(
            embed=err_embed(
                "I couldn't post the menu to that channel. Check that I can send "
                "messages/embeds there and that every emoji is valid."
            ),
            ephemeral=True,
        )
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE reaction_menus SET channel_id = ?, message_id = ? WHERE menu_id = ?",
            (str(channel.id), str(message.id), menu_id),
        )
        await db.commit()

    await _log_menu_event(
        guild, interaction.user,
        "Reaction Menu Created",
        f"Created reaction-role menu **{wizard.name}** via the setup wizard.",
        channel=channel,
    )

    embed = ok_embed(f"Menu **{wizard.name}** is live in {channel.mention}.")
    embed.add_field(name="Jump to it", value=f"[Open the menu]({message.jump_url})")
    await interaction.response.edit_message(embed=embed, view=None)


class BindEmojiModal(discord.ui.Modal):
    """Ask for role + emoji when binding from the message context menu."""

    def __init__(self, message: discord.Message):
        super().__init__(title="Bind Reaction Role", timeout=180)
        self.message = message
        self.role_input = discord.ui.TextInput(
            label="Role",
            placeholder="@Member, 123456789, or exact name",
            max_length=100,
            required=True,
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji",
            placeholder="🎮 or <:name:123456789>",
            max_length=100,
            required=True,
        )
        self.add_item(self.role_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        role = _find_role(interaction.guild, self.role_input.value)
        result = await create_reaction_binding(
            interaction,
            self.message.channel,
            str(self.message.id),
            role.id if role else None,
            self.emoji_input.value.strip(),
        )
        await interaction.response.send_message(embed=result, ephemeral=True)


# ------------------------- Manage existing menus -------------------------

async def _insert_menu_role(menu_id: int, role_id: str, label: str,
                           emoji: Optional[str], description: Optional[str],
                           position: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO reaction_menu_roles (menu_id, role_id, label, emoji, "
            "description, position) VALUES (?, ?, ?, ?, ?, ?)",
            (menu_id, role_id, label, emoji, description, position),
        )
        await db.commit()


async def _update_menu_role(menu_id: int, role_id: str, label: str,
                            emoji: Optional[str], description: Optional[str]):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE reaction_menu_roles SET label = ?, emoji = ?, description = ? "
            "WHERE menu_id = ? AND role_id = ?",
            (label, emoji, description, menu_id, role_id),
        )
        await db.commit()


async def _delete_menu_role(menu_id: int, role_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM reaction_menu_roles WHERE menu_id = ? AND role_id = ?",
            (menu_id, role_id),
        )
        await db.commit()


async def _update_menu_meta(menu_id: int, **fields):
    """Update arbitrary columns on a reaction_menus row."""
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            f"UPDATE reaction_menus SET {assignments} WHERE menu_id = ?",
            (*fields.values(), menu_id),
        )
        await db.commit()


async def _delete_menu(menu_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM reaction_menu_roles WHERE menu_id = ?", (menu_id,)
        )
        await db.execute(
            "DELETE FROM reaction_menus WHERE menu_id = ?", (menu_id,)
        )
        await db.commit()


class MenuManageView(discord.ui.View):
    """Full control panel for one existing menu."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild, menu_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.guild = guild
        self.menu_id = menu_id
        self.add_item(ManageAddRoleSelect())
        self.add_item(ManageRemoveSelect(self))
        self.add_item(ManageEditSelect(self))
        self.add_item(ManageSettingsSelect(self))

    async def refresh_selects(self):
        """Load current DB state into the remove/edit/settings selects."""
        menu = await fetch_menu_by_id(self.menu_id)
        for child in self.children:
            if isinstance(child, (ManageRemoveSelect, ManageEditSelect)):
                await child.refresh_options()
            elif isinstance(child, ManageSettingsSelect) and menu is not None:
                await child.refresh_options(menu)

    async def guard(self, interaction: discord.Interaction) -> bool:
        allowed = getattr(interaction.user.guild_permissions, "manage_roles", False)
        if not allowed:
            await interaction.response.send_message(
                "❌ You need the **Manage Roles** permission to use this.",
                ephemeral=True,
            )
        return allowed

    async def build_embed(self) -> Optional[discord.Embed]:
        menu = await fetch_menu_by_id(self.menu_id)
        if menu is None:
            return None
        roles = await fetch_menu_roles(self.menu_id)
        embed = base_embed(f"🎭 Managing: {menu['name']}", "primary")
        embed.add_field(name="Style", value=STYLE_OPTIONS[menu["style"]][0], inline=True)
        embed.add_field(name="Mode", value=MODE_OPTIONS[menu["mode"]][0], inline=True)
        posted_value = "Not posted yet"
        if menu["message_id"] and menu["channel_id"]:
            channel = self.guild.get_channel(int(menu["channel_id"]))
            if channel is not None:
                jump = (f"https://discord.com/channels/{self.guild.id}/"
                        f"{menu['channel_id']}/{menu['message_id']}")
                posted_value = f"{channel.mention} · [jump]({jump})"
        embed.add_field(name="Posted", value=posted_value, inline=True)
        if roles:
            lines = []
            for item in roles:
                emoji = f"{item['emoji']} " if item["emoji"] else ""
                lines.append(f"{emoji}**{item['label']}**")
            embed.add_field(
                name=f"Roles ({len(roles)}/{MAX_MENU_ROLES})",
                value=truncate("\n".join(lines), 1024),
                inline=False,
            )
        embed.set_footer(
            text="Changes save instantly and update the live menu. "
                 "Name/description, post/move and delete are the buttons below."
        )
        return embed

    async def rerender(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        if embed is None:
            await interaction.response.edit_message(
                embed=base_embed("🎭 Menu Manager", "neutral",
                                 "This menu no longer exists."),
                view=None,
            )
            return
        fresh = MenuManageView(self.bot, self.guild, self.menu_id)
        await fresh.refresh_selects()
        await interaction.response.edit_message(embed=embed, view=fresh)

    @discord.ui.button(label="✏️ Name/Desc", style=discord.ButtonStyle.secondary, row=4)
    async def edit_meta(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        menu = await fetch_menu_by_id(self.menu_id)
        if menu is None:
            await self.rerender(interaction)
            return
        await interaction.response.send_modal(
            ManageNameModal(self.bot, self.guild, self.menu_id, menu)
        )

    @discord.ui.button(label="📩 Post/Move", style=discord.ButtonStyle.primary, row=4)
    async def post_move(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        view = ManagePostView(self.bot, self.guild, self.menu_id)
        await interaction.response.send_message(
            embed=base_embed("📩 Post / Move Menu", "primary",
                             "Pick a channel and I'll post the menu there. "
                             "If it's already posted, the old message is removed."),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=4)
    async def delete_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        menu = await fetch_menu_by_id(self.menu_id)
        if menu is None:
            await self.rerender(interaction)
            return
        await interaction.response.edit_message(
            embed=base_embed("🗑️ Delete Menu?", "danger",
                             f"This permanently deletes **{menu['name']}** and removes "
                             "its message. This cannot be undone."),
            view=ManageDeleteConfirmView(self.bot, self.guild, self.menu_id),
        )


class ManageAddRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="➕ Add a role…", min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: MenuManageView = self.view
        if not await view.guard(interaction):
            return
        role = self.values[0]
        roles = await fetch_menu_roles(view.menu_id)
        error = _menu_role_error(interaction.guild, role, roles)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await _insert_menu_role(
            view.menu_id, str(role.id), role.name, None, None, len(roles) + 1
        )
        await refresh_posted_menu(view.bot, view.menu_id)
        await view.rerender(interaction)


class ManageRemoveSelect(discord.ui.Select):
    def __init__(self, view: MenuManageView):
        self.menu_id = view.menu_id
        # Filled in when the manage view is built, since this needs the DB.
        super().__init__(
            placeholder="➖ Remove a role…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading…", value="none")],
            row=1,
            disabled=True,
        )

    async def refresh_options(self):
        roles = await fetch_menu_roles(self.menu_id)
        self.options = [
            discord.SelectOption(
                label=item["label"][:100],
                value=item["role_id"],
                emoji=item["emoji"] or None,
                description="Remove this role",
            )
            for item in roles[:25]
        ] or [discord.SelectOption(label="No roles on this menu", value="none")]
        self.disabled = not roles
        self.placeholder = f"➖ Remove a role ({len(roles)})…"

    async def callback(self, interaction: discord.Interaction):
        view: MenuManageView = self.view
        if not await view.guard(interaction):
            return
        if self.values[0] == "none":
            await interaction.response.defer_update()
            return
        await _delete_menu_role(view.menu_id, self.values[0])
        await refresh_posted_menu(view.bot, view.menu_id)
        await view.rerender(interaction)


class ManageEditSelect(discord.ui.Select):
    """Pick an entry to edit its emoji/label/description."""

    def __init__(self, view: MenuManageView):
        self.menu_id = view.menu_id
        super().__init__(
            placeholder="✏️ Edit an entry…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading…", value="none")],
            row=2,
            disabled=True,
        )

    async def refresh_options(self):
        roles = await fetch_menu_roles(self.menu_id)
        self.options = [
            discord.SelectOption(
                label=item["label"][:100],
                value=item["role_id"],
                emoji=item["emoji"] or None,
                description="Edit emoji / label / description",
            )
            for item in roles[:25]
        ] or [discord.SelectOption(label="No roles on this menu", value="none")]
        self.disabled = not roles
        self.placeholder = f"✏️ Edit an entry ({len(roles)})…"

    async def callback(self, interaction: discord.Interaction):
        view: MenuManageView = self.view
        if not await view.guard(interaction):
            return
        if self.values[0] == "none":
            await interaction.response.defer_update()
            return
        roles = await fetch_menu_roles(view.menu_id)
        entry = next((r for r in roles if r["role_id"] == self.values[0]), None)
        if entry is None:
            await view.rerender(interaction)
            return
        await interaction.response.send_modal(
            ManageRoleDetailsModal(view.bot, view.guild, view.menu_id, entry)
        )


class ManageSettingsSelect(discord.ui.Select):
    """Change style and mode from one select."""

    def __init__(self, view: MenuManageView):
        self.menu_id = view.menu_id
        super().__init__(
            placeholder="⚙️ Change style or mode…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading…", value="none")],
            row=3,
        )

    async def refresh_options(self, menu: dict):
        # Emoji marks the current value — `default=` can't be used here because
        # two options (one style + one mode) would exceed max_values=1.
        self.options = [
            discord.SelectOption(
                label=f"Style: {label}", value=f"style:{key}",
                description=desc,
                emoji="\u2705" if menu["style"] == key else None,
            )
            for key, (label, desc) in STYLE_OPTIONS.items()
        ] + [
            discord.SelectOption(
                label=f"Mode: {label}", value=f"mode:{key}",
                description=desc,
                emoji="\u2705" if menu["mode"] == key else None,
            )
            for key, (label, desc) in MODE_OPTIONS.items()
        ]

    async def callback(self, interaction: discord.Interaction):
        view: MenuManageView = self.view
        if not await view.guard(interaction):
            return
        kind, _, value = self.values[0].partition(":")
        if kind not in ("style", "mode"):
            await interaction.response.defer_update()
            return
        await _update_menu_meta(view.menu_id, **{kind: value})
        await refresh_posted_menu(view.bot, view.menu_id)
        await view.rerender(interaction)


class ManageNameModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, guild: discord.Guild,
                 menu_id: int, menu: dict):
        super().__init__(title=f"Edit {menu['name']}"[:45], timeout=180)
        self.bot = bot
        self.guild = guild
        self.menu_id = menu_id
        self.name_input = discord.ui.TextInput(
            label="Menu name",
            default=menu["name"][:80],
            max_length=80,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description",
            default=(menu["description"] or "")[:1000],
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.add_item(self.name_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await _update_menu_meta(
                self.menu_id,
                name=self.name_input.value.strip(),
                description=self.desc_input.value.strip() or None,
            )
        except aiosqlite.IntegrityError:
            await interaction.response.send_message(
                embed=err_embed("A menu with that name already exists."),
                ephemeral=True,
            )
            return
        await refresh_posted_menu(self.bot, self.menu_id)
        view = MenuManageView(self.bot, self.guild, self.menu_id)
        await view.refresh_selects()
        await interaction.response.edit_message(
            embed=await view.build_embed(), view=view
        )


class ManageRoleDetailsModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, guild: discord.Guild,
                 menu_id: int, entry: dict):
        super().__init__(title=f"Edit {entry['label']}"[:45], timeout=180)
        self.bot = bot
        self.guild = guild
        self.menu_id = menu_id
        self.role_id = entry["role_id"]
        role = guild.get_role(int(entry["role_id"]))
        self.fallback_label = role.name if role else entry["label"]
        self.label_input = discord.ui.TextInput(
            label="Button / option label",
            default=entry["label"][:80],
            placeholder=self.fallback_label[:100],
            max_length=80,
            required=False,
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji (optional)",
            default=entry["emoji"] or "",
            placeholder="🎮 or <:name:id>",
            max_length=100,
            required=False,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (optional)",
            default=entry["description"] or "",
            placeholder="Shown under the option in dropdowns",
            style=discord.TextStyle.paragraph,
            max_length=100,
            required=False,
        )
        self.add_item(self.label_input)
        self.add_item(self.emoji_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        emoji = self.emoji_input.value.strip()
        if not _valid_emoji(emoji):
            await interaction.response.send_message(
                embed=err_embed(
                    "That doesn't look like a valid emoji. Use a unicode emoji or "
                    "a custom emoji from this server."
                ),
                ephemeral=True,
            )
            return
        await _update_menu_role(
            self.menu_id,
            self.role_id,
            self.label_input.value.strip() or self.fallback_label,
            emoji or None,
            self.desc_input.value.strip() or None,
        )
        await refresh_posted_menu(self.bot, self.menu_id)
        view = MenuManageView(self.bot, self.guild, self.menu_id)
        await view.refresh_selects()
        await interaction.response.edit_message(
            embed=await view.build_embed(), view=view
        )


class ManagePostChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: "ManagePostView"):
        super().__init__(
            placeholder="Choose a channel…",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.post_view = view

    async def callback(self, interaction: discord.Interaction):
        self.post_view.channel_id = self.values[0].id
        await interaction.response.defer_update()


class ManagePostView(discord.ui.View):
    """Pick a channel and (re)post the managed menu there."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild, menu_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.menu_id = menu_id
        self.channel_id: Optional[int] = None
        self.add_item(ManagePostChannelSelect(self))

    @discord.ui.button(label="📩 Post here", style=discord.ButtonStyle.success, row=1)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.channel_id is None:
            await interaction.response.send_message(
                "Pick a channel first.", ephemeral=True
            )
            return
        await self._do_post(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=base_embed("📩 Post / Move Menu", "neutral", "Cancelled — nothing changed."),
            view=None,
        )

    async def _do_post(self, interaction: discord.Interaction):
        menu = await fetch_menu_by_id(self.menu_id)
        channel = self.guild.get_channel(self.channel_id)
        if menu is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.edit_message(
                embed=err_embed("That menu or channel no longer exists."), view=None
            )
            return

        roles = await fetch_menu_roles(self.menu_id)
        if not roles:
            await interaction.response.edit_message(
                embed=err_embed("This menu has no roles — add some first."), view=None
            )
            return

        view = ReactionMenuView(menu, roles, persistent=True)
        self.bot.add_view(view)
        try:
            message = await channel.send(
                embed=await build_menu_embed(menu, roles), view=view
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.edit_message(
                embed=err_embed(
                    "I couldn't post there. Check my permissions and that every "
                    "emoji on the menu is valid."
                ),
                view=None,
            )
            return

        # Remove the previous message so the menu doesn't exist twice
        if menu["message_id"] and menu["channel_id"]:
            old_channel = self.guild.get_channel(int(menu["channel_id"]))
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old = await old_channel.fetch_message(int(menu["message_id"]))
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await _update_menu_meta(
            self.menu_id, channel_id=str(channel.id), message_id=str(message.id)
        )
        await _log_menu_event(
            self.guild, interaction.user,
            "Reaction Menu Posted/Moved",
            f"Posted/moved reaction-role menu **{menu['name']}**.",
            channel=channel,
        )
        embed = ok_embed(f"Menu posted to {channel.mention}.")
        embed.add_field(name="Jump to it", value=f"[Open the menu]({message.jump_url})")
        await interaction.response.edit_message(embed=embed, view=None)


class ManageDeleteConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild: discord.Guild, menu_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild = guild
        self.menu_id = menu_id

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        menu = await fetch_menu_by_id(self.menu_id)
        if menu is None:
            await interaction.response.edit_message(
                embed=base_embed("🎭 Menu Manager", "neutral",
                                 "That menu is already gone."), view=None
            )
            return
        if menu["message_id"] and menu["channel_id"]:
            channel = self.guild.get_channel(int(menu["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(menu["message_id"]))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await _delete_menu(self.menu_id)
        await _log_menu_event(
            self.guild, interaction.user,
            "Reaction Menu Deleted",
            f"Deleted reaction-role menu **{menu['name']}**.",
        )
        await interaction.response.edit_message(
            embed=ok_embed(f"Deleted menu **{menu['name']}**."), view=None
        )

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary, row=0)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MenuManageView(self.bot, self.guild, self.menu_id)
        embed = await view.build_embed()
        if embed is None:
            await interaction.response.edit_message(
                embed=base_embed("🎭 Menu Manager", "neutral",
                                 "This menu no longer exists."), view=None
            )
            return
        await view.refresh_selects()
        await interaction.response.edit_message(embed=embed, view=view)


class ReactionRoles(commands.Cog):
    """Persistent button and dropdown role menus."""

    reactionrole = app_commands.Group(
        name="reactionrole",
        description="Manage persistent reaction-role menus.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Right-click a message → Apps → Bind Reaction Role (no ID typing).
        self.bind_context_menu = app_commands.ContextMenu(
            name="Bind Reaction Role",
            callback=self._bind_reaction_role_context,
        )
        self.bot.tree.add_command(self.bind_context_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(
            self.bind_context_menu.name, type=self.bind_context_menu.type
        )

    @app_commands.checks.has_permissions(manage_roles=True)
    async def _bind_reaction_role_context(self, interaction: discord.Interaction,
                                          message: discord.Message):
        if interaction.guild is None:
            return
        if message.guild is None or message.guild.id != interaction.guild.id:
            await interaction.response.send_message(
                "That message isn't in this server.", ephemeral=True
            )
            return
        if not isinstance(message.channel, discord.TextChannel):
            await interaction.response.send_message(
                "I can only bind reactions in text channels.", ephemeral=True
            )
            return
        await interaction.response.send_modal(BindEmojiModal(message))

    @reactionrole.command(name="create", description="Create a reaction-role menu.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.describe(
        name="Unique menu name.",
        description="Shown at the top of the menu.",
        style="Button menu or dropdown menu.",
        mode="Toggle roles or replace roles exclusively.",
    )
    async def reactionrole_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 80],
        description: Optional[str],
        style: app_commands.Choice[str],
        mode: app_commands.Choice[str],
    ):
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                cursor = await db.execute(
                    "INSERT INTO reaction_menus (guild_id, name, description, style, mode, "
                    "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(interaction.guild.id),
                        name.strip(),
                        description,
                        style.value,
                        mode.value,
                        str(interaction.user.id),
                        str(discord.utils.utcnow()),
                    ),
                )
                await db.commit()
                menu_id = cursor.lastrowid
        except aiosqlite.IntegrityError:
            await interaction.response.send_message(
                embed=err_embed("A menu with that name already exists."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(
                f"Menu **{name.strip()}** created with ID `{menu_id}`. "
                "Add roles with `/reactionrole add`, post it with `/reactionrole post`, "
                "or skip all that with `/reactionrole wizard` next time."
            ),
            ephemeral=True,
        )

    @reactionrole.command(name="wizard", description="Guided setup: build and post a role menu in one flow.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def reactionrole_wizard(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WizardNameModal(self.bot))

    @reactionrole.command(name="add", description="Add a role to a reaction-role menu.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.autocomplete(menu=menu_autocomplete)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def reactionrole_add(
        self,
        interaction: discord.Interaction,
        menu: str,
        role: discord.Role,
        label: app_commands.Range[str, 1, 80],
        emoji: Optional[str] = None,
        description: Optional[str] = None,
    ):
        menu_data = await fetch_menu(str(interaction.guild.id), menu)
        if menu_data is None:
            await interaction.response.send_message("Menu not found.", ephemeral=True)
            return

        if role == interaction.guild.default_role or role.managed:
            await interaction.response.send_message(
                "Use a normal, non-managed role.", ephemeral=True
            )
            return
        if interaction.guild.me.top_role <= role:
            await interaction.response.send_message(
                "That role is above my highest role, so I cannot assign it.",
                ephemeral=True,
            )
            return

        existing = await fetch_menu_roles(menu_data["menu_id"])
        if any(int(item["role_id"]) == role.id for item in existing):
            await interaction.response.send_message(
                "That role is already on this menu.", ephemeral=True
            )
            return
        if len(existing) >= MAX_MENU_ROLES:
            await interaction.response.send_message(
                f"A menu can contain at most {MAX_MENU_ROLES} roles.", ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO reaction_menu_roles (menu_id, role_id, label, emoji, description, position) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    menu_data["menu_id"],
                    str(role.id),
                    label.strip(),
                    emoji,
                    description,
                    len(existing) + 1,
                ),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Added {role.mention} to **{menu_data['name']}**."),
            ephemeral=True,
        )
        await refresh_posted_menu(self.bot, menu_data["menu_id"])
        await _log_menu_event(
            interaction.guild, interaction.user,
            "Reaction Menu Updated",
            f"Added **{role.name}** to menu **{menu_data['name']}**.",
        )

    @reactionrole.command(name="remove", description="Remove a role from a reaction-role menu.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def reactionrole_remove(
        self,
        interaction: discord.Interaction,
        menu: str,
        role: discord.Role,
    ):
        menu_data = await fetch_menu(str(interaction.guild.id), menu)
        if menu_data is None:
            await interaction.response.send_message("Menu not found.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "DELETE FROM reaction_menu_roles WHERE menu_id = ? AND role_id = ?",
                (menu_data["menu_id"], str(role.id)),
            )
            await db.commit()

        if cursor.rowcount == 0:
            await interaction.response.send_message(
                "That role is not on this menu.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(f"Removed {role.mention} from **{menu_data['name']}**."),
            ephemeral=True,
        )
        await refresh_posted_menu(self.bot, menu_data["menu_id"])
        await _log_menu_event(
            interaction.guild, interaction.user,
            "Reaction Menu Updated",
            f"Removed **{role.name}** from menu **{menu_data['name']}**.",
        )

    @reactionrole.command(name="post", description="Post a reaction-role menu to a channel.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.autocomplete(menu=menu_autocomplete)
    @app_commands.checks.bot_has_permissions(send_messages=True, embed_links=True)
    async def reactionrole_post(
        self,
        interaction: discord.Interaction,
        menu: str,
        channel: discord.TextChannel,
    ):
        menu_data = await fetch_menu(str(interaction.guild.id), menu)
        if menu_data is None:
            await interaction.response.send_message("Menu not found.", ephemeral=True)
            return
        if menu_data["message_id"]:
            await interaction.response.send_message(
                "That menu is already posted. Delete and recreate it to move the existing message.",
                ephemeral=True,
            )
            return

        roles = await fetch_menu_roles(menu_data["menu_id"])
        if not roles:
            await interaction.response.send_message(
                "Add at least one role before posting this menu.", ephemeral=True
            )
            return

        view = ReactionMenuView(menu_data, roles, persistent=True)
        self.bot.add_view(view)
        try:
            message = await channel.send(
                embed=await build_menu_embed(menu_data, roles),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't send the menu to that channel.", ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE reaction_menus SET channel_id = ?, message_id = ? WHERE menu_id = ?",
                (str(channel.id), str(message.id), menu_data["menu_id"]),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Menu posted to {channel.mention}."),
            ephemeral=True,
        )
        await _log_menu_event(
            interaction.guild, interaction.user,
            "Reaction Menu Posted",
            f"Posted reaction-role menu **{menu_data['name']}**.",
            channel=channel,
        )

    @reactionrole.command(name="list", description="List reaction-role menus.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reactionrole_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT menu_id, name, style, mode, message_id FROM reaction_menus "
                "WHERE guild_id = ? ORDER BY name",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🎭 Reaction Roles", "neutral", "No menus have been created."),
                ephemeral=True,
            )
            return

        embed = base_embed("🎭 Reaction-Role Menus", "primary")
        embed.set_footer(text="Tip: /reactionrole manage <name> opens a full edit panel for any menu.")
        for menu_id, name, style, mode, message_id in rows[:25]:
            embed.add_field(
                name=f"{name} · `{menu_id}`",
                value=(
                    f"Style: {style.title()}\n"
                    f"Mode: {mode.title()}\n"
                    f"Posted: {'Yes' if message_id else 'No'}"
                ),
                inline=True,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @reactionrole.command(name="manage", description="Edit, post/move, or delete an existing reaction-role menu.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def reactionrole_manage(self, interaction: discord.Interaction, menu: str):
        menu_data = await fetch_menu(str(interaction.guild.id), menu)
        if menu_data is None:
            await interaction.response.send_message("Menu not found.", ephemeral=True)
            return

        view = MenuManageView(self.bot, interaction.guild, menu_data["menu_id"])
        embed = await view.build_embed()
        if embed is None:
            await interaction.response.send_message("Menu not found.", ephemeral=True)
            return
        await view.refresh_selects()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @reactionrole.command(name="delete", description="Delete a reaction-role menu.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def reactionrole_delete(self, interaction: discord.Interaction, menu: str):
        menu_data = await fetch_menu(str(interaction.guild.id), menu)
        if menu_data is None:
            await interaction.response.send_message("Menu not found.", ephemeral=True)
            return

        if menu_data["message_id"] and menu_data["channel_id"]:
            channel = interaction.guild.get_channel(int(menu_data["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(menu_data["message_id"]))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM reaction_menu_roles WHERE menu_id = ?",
                (menu_data["menu_id"],),
            )
            await db.execute(
                "DELETE FROM reaction_menus WHERE menu_id = ?",
                (menu_data["menu_id"],),
            )
            await db.commit()

        await interaction.response.send_message(
            embed=ok_embed(f"Deleted menu **{menu_data['name']}**."),
            ephemeral=True,
        )
        await _log_menu_event(
            interaction.guild, interaction.user,
            "Reaction Menu Deleted",
            f"Deleted reaction-role menu **{menu_data['name']}**.",
        )

    @reactionrole.command(name="send", description="Send a reaction-role panel message.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(send_messages=True, embed_links=True)
    async def reactionrole_send(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: app_commands.Range[str, 1, 120],
        description: Optional[str] = None,
    ):
        embed = base_embed(title, "primary")
        if description:
            embed.description = description
        try:
            message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't send a panel message to that channel.",
                ephemeral=True,
            )
            return

        result = discord.Embed(
            title="Reaction Panel Sent",
            description=(
                f"Panel: [jump to message]({message.jump_url})\n"
                f"Message ID: `{message.id}`\n\n"
                f"Next step:\n"
                f"`/reactionrole bind channel:{channel.mention} message_id:{message.id} role:<ROLE> emoji:<EMOJI>`\n"
                f"or right-click the message → **Apps → Bind Reaction Role**."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=result, ephemeral=True)

    @reactionrole.command(name="bind", description="Bind an emoji reaction on a message to a role.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, read_message_history=True)
    @app_commands.describe(
        channel="The channel containing the message.",
        message_id="Right-click the message → Copy Message ID.",
        role="The role to grant when someone reacts.",
        emoji="Unicode emoji or a custom emoji from this server.",
    )
    async def reactionrole_bind(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message_id: str,
        role: discord.Role,
        emoji: str,
    ):
        result = await create_reaction_binding(
            interaction,
            channel,
            message_id.strip(),
            role.id,
            emoji.strip(),
        )
        await interaction.response.send_message(embed=result, ephemeral=True)

    @reactionrole.command(name="unbind", description="Remove an emoji reaction-role binding.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reactionrole_unbind(self, interaction: discord.Interaction,
                                  binding_id: int):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT channel_id, message_id, emoji FROM reaction_role_bindings "
                "WHERE binding_id = ? AND guild_id = ?",
                (binding_id, str(interaction.guild.id)),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await interaction.response.send_message("Binding not found.", ephemeral=True)
                return
            await db.execute(
                "DELETE FROM reaction_role_bindings WHERE binding_id = ?",
                (binding_id,),
            )
            await db.commit()

        channel = interaction.guild.get_channel(int(row[0]))
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(int(row[1]))
                await message.remove_reaction(row[2], interaction.guild.me)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await interaction.response.send_message(
            embed=ok_embed(f"Reaction-role binding #{binding_id} removed."),
            ephemeral=True,
        )

    @reactionrole.command(name="bindings", description="List emoji reaction-role bindings.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reactionrole_bindings(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT binding_id, channel_id, message_id, role_id, emoji "
                "FROM reaction_role_bindings WHERE guild_id = ? ORDER BY binding_id DESC LIMIT 25",
                (str(interaction.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                embed=base_embed("🎭 Reaction Bindings", "neutral", "No emoji reaction bindings are configured."),
                ephemeral=True,
            )
            return
        embed = base_embed("🎭 Reaction Bindings", "primary")
        for binding_id, channel_id, message_id, role_id, emoji in rows:
            channel = interaction.guild.get_channel(int(channel_id))
            role = interaction.guild.get_role(int(role_id))
            jump = f"https://discord.com/channels/{interaction.guild.id}/{channel_id}/{message_id}"
            embed.add_field(
                name=f"#{binding_id} · {emoji}",
                value=(
                    f"Role: {role.mention if role else '`deleted role`'}\n"
                    f"Channel: {channel.mention if channel else '`deleted channel`'}\n"
                    f"[Message]({jump})"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await apply_reaction_binding(payload, remove=False, bot=self.bot)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await apply_reaction_binding(payload, remove=True, bot=self.bot)


# ------------------------- Emoji reaction bindings -------------------------

def emoji_key(emoji) -> str:
    if isinstance(emoji, str):
        return emoji.strip()
    return str(emoji)


async def fetch_binding(guild_id: str, channel_id: str,
                        message_id: str, emoji: str) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT binding_id, role_id, emoji FROM reaction_role_bindings "
            "WHERE guild_id = ? AND channel_id = ? AND message_id = ? AND emoji = ?",
            (guild_id, channel_id, message_id, emoji),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {"binding_id": row[0], "role_id": row[1], "emoji": row[2]}


async def apply_reaction_binding(payload: discord.RawReactionActionEvent,
                                 remove: bool = False,
                                 bot: commands.Bot | None = None):
    if payload.guild_id is None:
        return
    guild = payload.member.guild if payload.member is not None else bot.get_guild(payload.guild_id)
    if guild is None:
        return
    binding = await fetch_binding(
        str(guild.id),
        str(payload.channel_id),
        str(payload.message_id),
        emoji_key(payload.emoji),
    )
    if binding is None:
        return

    member = guild.get_member(payload.user_id)
    role = guild.get_role(int(binding["role_id"]))
    if member is None or member.bot or role is None:
        return
    if guild.me.top_role <= role:
        return

    try:
        if remove and role in member.roles:
            await member.remove_roles(role, reason="Emoji reaction role removed")
        elif not remove and role not in member.roles:
            await member.add_roles(role, reason="Emoji reaction role added")
    except (discord.Forbidden, discord.HTTPException):
        pass


# Add the emoji-based workflow as simple commands. The older button/dropdown
# commands remain loaded for compatibility but are hidden from the tree.
def _parse_role_id(value: str) -> int | None:
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else None


class ReactionRoleBindModal(discord.ui.Modal):
    def __init__(self, guild: discord.Guild):
        super().__init__(title="Add Reaction Role", timeout=180)
        self.guild = guild
        self.channel = discord.ui.TextInput(
            label="Channel ID or #mention",
            placeholder="1489043893227094036 or #roles",
            max_length=100,
            required=True,
        )
        self.message_id = discord.ui.TextInput(
            label="Message ID",
            max_length=30,
            required=True,
        )
        self.role_id = discord.ui.TextInput(
            label="Role ID or @mention",
            max_length=100,
            required=True,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji",
            placeholder="🎮 or <:role:123456789>",
            max_length=100,
            required=True,
        )
        self.add_item(self.channel)
        self.add_item(self.message_id)
        self.add_item(self.role_id)
        self.add_item(self.emoji)

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = _parse_role_id(self.channel.value)
        message_id = self.message_id.value.strip()
        role_id = _parse_role_id(self.role_id.value)
        emoji = self.emoji.value.strip()
        if channel_id is None:
            await interaction.response.send_message("I couldn't understand that channel.", ephemeral=True)
            return
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return
        result = await create_reaction_binding(
            interaction,
            channel,
            message_id,
            role_id,
            emoji,
        )
        if result is not None:
            await interaction.response.send_message(embed=result, ephemeral=True)


async def create_reaction_binding(interaction: discord.Interaction,
                                  channel: discord.TextChannel,
                                  message_id: str,
                                  role_id: int | None,
                                  emoji: str) -> discord.Embed | None:
    if role_id is None:
        return discord.Embed(title="Invalid Role", description="Role not found. Use a mention, an ID, or the exact role name.", color=discord.Color.red())
    guild = interaction.guild
    role = guild.get_role(role_id)
    if role is None or role == guild.default_role or role.managed:
        return discord.Embed(title="Invalid Role", description="Role not found or not assignable.", color=discord.Color.red())
    if guild.me.top_role <= role:
        return discord.Embed(title="Invalid Role", description="That role is above my highest role.", color=discord.Color.red())
    try:
        message = await channel.fetch_message(int(message_id))
    except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
        return discord.Embed(title="Message Not Found", description="I couldn't find that message in the selected channel.", color=discord.Color.red())

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO reaction_role_bindings (guild_id, channel_id, message_id, role_id, emoji, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(guild.id),
                    str(channel.id),
                    str(message.id),
                    str(role.id),
                    emoji,
                    str(interaction.user.id),
                    str(discord.utils.utcnow()),
                ),
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        return discord.Embed(title="Already Bound", description="That emoji is already bound on that message.", color=discord.Color.red())

    try:
        await message.add_reaction(emoji)
    except (discord.Forbidden, discord.HTTPException):
        return discord.Embed(title="Binding Saved", description="The binding was saved, but I couldn't add the reaction. Check emoji and permissions.", color=discord.Color.orange())

    return discord.Embed(
        title="Reaction Role Added",
        description=f"{emoji} on [this message]({message.jump_url}) now grants {role.mention}.",
        color=discord.Color.green(),
    )


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
