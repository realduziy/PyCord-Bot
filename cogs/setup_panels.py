import datetime
import json

import aiosqlite
from typing import Awaitable, Callable, Optional

import discord
from discord.ui import Button, Modal, Select, TextInput

from cog_utils import (
    DB_FILE,
    get_staff_bypass_roles,
    set_staff_bypass_roles,
)
from ui_common import PanelView, base_embed, ok_embed, truncate, yn

from cogs.games import get_counting_state, save_counting_state
from cogs.leveling import (
    fetch_role_rewards,
    get_level_config,
    update_level_config,
)
from cogs.mod_workflows import (
    get_ignored_maintenance_roles,
    get_role_maintenance_enabled,
)
from cogs.reaction_roles import (
    ReactionMenuView,
    build_menu_embed,
    fetch_menu,
    fetch_menu_roles,
    refresh_posted_menu,
)
from cogs.server_defense import (
    active_preset_run,
    disarm_antiraid,
    fetch_custom_preset,
    get_antiraid_config,
    get_antiraid_state,
    restore_active_preset,
    start_preset_run,
    trigger_antiraid,
    update_antiraid_config,
    BUILTIN_PRESETS,
)
from cogs.suggestions import get_suggestion_channel_id, set_suggestion_channel_id


class SetupView(PanelView):
    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class TextChannelSelect(Select):
    def __init__(self, view: "SetupView", *, allow_clear: bool = False,
                 placeholder: str = "Select a text channel…"):
        options = [
            discord.SelectOption(label=channel.name[:100], value=str(channel.id))
            for channel in view.guild.text_channels[:24]
        ]
        if allow_clear:
            options.append(discord.SelectOption(label="Clear this setting", value="clear"))
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.setup_view = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.setup_view.guard(interaction):
            return
        await self.setup_view.channel_selected(interaction, self.values[0])
        await self.setup_view.refresh(interaction)


class RoleSelect(Select):
    def __init__(self, view: "SetupView", *, placeholder: str = "Select a role…",
                 row: int = 1):
        options = [
            discord.SelectOption(label=role.name[:100], value=str(role.id))
            for role in view.guild.roles
            if not role.is_default() and not role.managed
        ][:25]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )
        self.setup_view = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.setup_view.guard(interaction):
            return
        await self.setup_view.role_selected(interaction, self.values[0])
        await self.setup_view.refresh(interaction)


class CommunityPanel(SetupView):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        self.add_item(TextChannelSelect(self, allow_clear=True, placeholder="Set suggestion channel…"))

    async def build(self):
        suggestion_id = await get_suggestion_channel_id(str(self.guild.id))
        suggestion = self.guild.get_channel(int(suggestion_id)) if suggestion_id else None
        counting = await get_counting_state(str(self.guild.id))
        embed = base_embed("🏘️ Community Setup", "primary")
        embed.add_field(
            name="Suggestion Channel",
            value=suggestion.mention if suggestion else "Not configured",
            inline=True,
        )
        if counting:
            channel = self.guild.get_channel(int(counting["channel_id"])) if counting["channel_id"] else None
            embed.add_field(name="Counting", value=yn(counting["enabled"]), inline=True)
            embed.add_field(
                name="Counting Channel",
                value=channel.mention if channel else "Not configured",
                inline=True,
            )
        else:
            embed.add_field(name="Counting", value="Not configured", inline=True)
        embed.set_footer(text="Use the controls below to manage community settings.")
        return embed

    async def channel_selected(self, interaction, value):
        if value == "clear":
            await set_suggestion_channel_id(str(self.guild.id), None)
        else:
            await set_suggestion_channel_id(str(self.guild.id), value)

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Counting", style=discord.ButtonStyle.primary, row=2)
    async def counting(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CountingPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Leveling", style=discord.ButtonStyle.primary, row=2)
    async def leveling(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = LevelingPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Level Rewards", style=discord.ButtonStyle.secondary, row=2)
    async def rewards(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = LevelRewardsPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Reaction Roles", style=discord.ButtonStyle.secondary, row=3)
    async def reaction_roles(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = ReactionRoleSimplePanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class CountingPanel(SetupView):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        self.add_item(TextChannelSelect(self, placeholder="Set counting channel…"))

    async def build(self):
        state = await get_counting_state(str(self.guild.id))
        embed = base_embed("🔢 Counting Setup", "primary")
        if not state:
            embed.description = "Counting is not configured yet."
            return embed
        channel = self.guild.get_channel(int(state["channel_id"])) if state["channel_id"] else None
        ignored = [self.guild.get_role(int(role_id)) for role_id in state["ignored_roles"]]
        embed.add_field(name="Enabled", value=yn(state["enabled"]), inline=True)
        embed.add_field(name="Current", value=str(state["current_count"]), inline=True)
        embed.add_field(name="Highest", value=str(state["high_count"]), inline=True)
        embed.add_field(name="Channel", value=channel.mention if channel else "Not set", inline=False)
        embed.add_field(
            name="Ignored Roles",
            value=", ".join(role.mention for role in ignored if role) or "None",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO counting_state (guild_id, channel_id, enabled, current_count, "
                "last_user_id, high_count, total_counts) VALUES (?, ?, 1, 0, NULL, 0, 0) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, "
                "enabled = 1, current_count = 0, last_user_id = NULL",
                (str(self.guild.id), value),
            )
            await db.commit()

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.primary, row=2)
    async def toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        state = await get_counting_state(str(self.guild.id))
        if not state:
            await interaction.response.send_message("Set a counting channel first.", ephemeral=True)
            return
        await save_counting_state(str(self.guild.id), enabled=0 if state["enabled"] else 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await save_counting_state(str(self.guild.id), current_count=0, last_user_id=None)
        await self.refresh(interaction)

    @discord.ui.button(label="Ignored Roles", style=discord.ButtonStyle.secondary, row=2)
    async def ignored_roles(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CountingIgnoredRolesPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class CountingIgnoredRolesPanel(SetupView):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        self.add_item(RoleSelect(self, placeholder="Select a role to add/remove…", row=0))

    async def build(self):
        state = await get_counting_state(str(self.guild.id)) or {"ignored_roles": []}
        roles = [self.guild.get_role(int(role_id)) for role_id in state["ignored_roles"]]
        embed = base_embed("🔢 Counting Ignored Roles", "primary")
        embed.description = "Select a role to toggle whether it can participate in counting."
        embed.add_field(
            name="Ignored Roles",
            value=", ".join(role.mention for role in roles if role) or "None",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        state = await get_counting_state(str(self.guild.id)) or {"ignored_roles": []}
        ignored = set(state["ignored_roles"])
        if value in ignored:
            ignored.remove(value)
        else:
            ignored.add(value)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO counting_state (guild_id, ignored_roles) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET ignored_roles = excluded.ignored_roles",
                (str(self.guild.id), json.dumps(sorted(ignored))),
            )
            await db.commit()

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CountingPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class LevelingPanel(SetupView):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def build(self):
        config = await get_level_config(str(self.guild.id))
        embed = base_embed("📈 Leveling Setup", "primary")
        embed.add_field(name="Enabled", value=yn(config["enabled"]), inline=True)
        embed.add_field(name="XP", value=f"{config['xp_min']}–{config['xp_max']}", inline=True)
        embed.add_field(name="Cooldown", value=f"{config['cooldown_seconds']}s", inline=True)
        embed.add_field(name="Stack Roles", value=yn(config["stack_roles"]), inline=True)
        embed.add_field(name="Auto-create Roles", value=yn(config["auto_create_roles"]), inline=True)
        embed.add_field(name="Ignored Channels", value=str(len(config["ignored_channels"])), inline=True)
        embed.add_field(name="Ignored Roles", value=str(len(config["ignored_roles"])), inline=True)
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.primary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        config = await get_level_config(str(self.guild.id))
        await update_level_config(str(self.guild.id), enabled=0 if config["enabled"] else 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Set Rates", style=discord.ButtonStyle.primary, row=0)
    async def rates(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(LevelingRatesModal(self))

    @discord.ui.button(label="Stacking", style=discord.ButtonStyle.secondary, row=0)
    async def stacking(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        config = await get_level_config(str(self.guild.id))
        await update_level_config(str(self.guild.id), stack_roles=0 if config["stack_roles"] else 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Auto-create", style=discord.ButtonStyle.secondary, row=0)
    async def auto_create(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        config = await get_level_config(str(self.guild.id))
        await update_level_config(str(self.guild.id), auto_create_roles=0 if config["auto_create_roles"] else 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Ignored Channels", style=discord.ButtonStyle.secondary, row=1)
    async def ignored_channels(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = LevelingIgnoredChannelsPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Ignored Roles", style=discord.ButtonStyle.secondary, row=1)
    async def ignored_roles(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = LevelingIgnoredRolesPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CommunityPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class LevelingRatesModal(Modal):
    def __init__(self, panel: LevelingPanel):
        super().__init__(title="Leveling Rates", timeout=180)
        self.panel = panel
        self.min_xp = TextInput(label="Minimum XP", placeholder="15", max_length=4, required=True)
        self.max_xp = TextInput(label="Maximum XP", placeholder="25", max_length=4, required=True)
        self.cooldown = TextInput(label="Cooldown seconds", placeholder="60", max_length=5, required=True)
        self.add_item(self.min_xp)
        self.add_item(self.max_xp)
        self.add_item(self.cooldown)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            minimum = int(self.min_xp.value)
            maximum = int(self.max_xp.value)
            cooldown = int(self.cooldown.value)
            if minimum < 1 or maximum < minimum or cooldown < 5:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter valid XP and cooldown values.", ephemeral=True)
            return
        await update_level_config(
            str(self.panel.guild.id),
            xp_min=minimum,
            xp_max=maximum,
            cooldown_seconds=cooldown,
        )
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class LevelingIgnoredChannelsPanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        self.add_item(TextChannelSelect(self, placeholder="Toggle ignored channel…"))

    async def build(self):
        config = await get_level_config(str(self.guild.id))
        channels = [self.guild.get_channel(int(channel_id)) for channel_id in config["ignored_channels"]]
        embed = base_embed("📈 Ignored XP Channels", "primary")
        embed.description = "Select a channel to toggle XP gain there."
        embed.add_field(
            name="Ignored Channels",
            value=", ".join(channel.mention for channel in channels if channel) or "None",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        config = await get_level_config(str(self.guild.id))
        ignored = set(config["ignored_channels"])
        ignored.symmetric_difference_update({value})
        await update_level_config(str(self.guild.id), ignored_channels=json.dumps(sorted(ignored)))

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = LevelingPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class LevelingIgnoredRolesPanel(LevelingIgnoredChannelsPanel):
    def __init__(self, guild):
        SetupView.__init__(self)
        self.guild = guild
        self.add_item(RoleSelect(self, placeholder="Toggle ignored role…", row=0))

    async def build(self):
        config = await get_level_config(str(self.guild.id))
        roles = [self.guild.get_role(int(role_id)) for role_id in config["ignored_roles"]]
        embed = base_embed("📈 Ignored XP Roles", "primary")
        embed.description = "Select a role to toggle XP gain for members with that role."
        embed.add_field(
            name="Ignored Roles",
            value=", ".join(role.mention for role in roles if role) or "None",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        config = await get_level_config(str(self.guild.id))
        ignored = set(config["ignored_roles"])
        ignored.symmetric_difference_update({value})
        await update_level_config(str(self.guild.id), ignored_roles=json.dumps(sorted(ignored)))


class LevelRewardsPanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        self.selected_role_id: int | None = None
        self.add_item(RoleSelect(self, placeholder="Select a role to add…", row=0))

    async def build(self):
        rewards = await fetch_role_rewards(str(self.guild.id))
        lines = []
        for reward in rewards:
            role = self.guild.get_role(int(reward["role_id"]))
            lines.append(f"**Level {reward['level']}:** {role.mention if role else '`deleted role`'}")
        embed = base_embed("🎁 Level Rewards", "primary")
        embed.description = "\n".join(lines) if lines else "No level rewards are configured."
        embed.add_field(name="Add Role", value="Select a role below, then choose a level.", inline=False)
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        self.selected_role_id = int(value)

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Choose Level", style=discord.ButtonStyle.primary, row=2)
    async def choose_level(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        if self.selected_role_id is None:
            await interaction.response.send_message("Select a role first.", ephemeral=True)
            return
        role = self.guild.get_role(self.selected_role_id)
        await interaction.response.send_modal(LevelRewardModal(self, role))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CommunityPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class LevelRewardModal(Modal):
    def __init__(self, panel: LevelRewardsPanel, role: discord.Role):
        super().__init__(title=f"Reward for {role.name}"[:45], timeout=180)
        self.panel = panel
        self.role = role
        self.level = TextInput(label="Level", placeholder="10", max_length=5, required=True)
        self.add_item(self.level)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            level = int(self.level.value)
            if level < 1 or level > 10000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a valid level.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO leveling_role_rewards (guild_id, level, role_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, level) DO UPDATE SET role_id = excluded.role_id",
                (
                    str(self.panel.guild.id),
                    level,
                    str(self.role.id),
                    str(interaction.user.id),
                    str(discord.utils.utcnow()),
                ),
            )
            await db.commit()
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class ModerationSafetyPanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild

    async def build(self):
        enabled = await get_role_maintenance_enabled(str(self.guild.id))
        ignored = await get_ignored_maintenance_roles(str(self.guild.id))
        bypass = await get_staff_bypass_roles(str(self.guild.id))
        ignored_roles = [self.guild.get_role(int(role_id)) for role_id in ignored]
        bypass_roles = [self.guild.get_role(int(role_id)) for role_id in bypass]
        embed = base_embed("⚖️ Moderation Safety", "primary")
        embed.add_field(name="Role Maintenance", value=yn(enabled), inline=True)
        embed.add_field(
            name="Never Restored",
            value=", ".join(role.mention for role in ignored_roles if role) or "None",
            inline=False,
        )
        embed.add_field(
            name="Staff Bypass Roles",
            value=", ".join(role.mention for role in bypass_roles if role) or "Automatic permission detection",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Toggle Maintenance", style=discord.ButtonStyle.primary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        enabled = await get_role_maintenance_enabled(str(self.guild.id))
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO role_maintenance_settings (guild_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
                (str(self.guild.id), 0 if enabled else 1),
            )
            await db.commit()
        await self.refresh(interaction)

    @discord.ui.button(label="Never Restore Roles", style=discord.ButtonStyle.secondary, row=0)
    async def ignored(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = MaintenanceIgnoredRolesPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Staff Bypass Roles", style=discord.ButtonStyle.secondary, row=0)
    async def bypass(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = StaffBypassRolesPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class MaintenanceIgnoredRolesPanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        self.add_item(RoleSelect(self, row=0))

    async def build(self):
        ignored = await get_ignored_maintenance_roles(str(self.guild.id))
        roles = [self.guild.get_role(int(role_id)) for role_id in ignored]
        embed = base_embed("🧰 Never Restore Roles", "primary")
        embed.description = "Select a role to toggle whether role maintenance skips it."
        embed.add_field(
            name="Ignored Roles",
            value=", ".join(role.mention for role in roles if role) or "None",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        async with aiosqlite.connect(DB_FILE) as db:
            exists = await (await db.execute(
                "SELECT 1 FROM role_maintenance_ignored_roles WHERE guild_id = ? AND role_id = ?",
                (str(self.guild.id), value),
            )).fetchone()
            if exists:
                await db.execute(
                    "DELETE FROM role_maintenance_ignored_roles WHERE guild_id = ? AND role_id = ?",
                    (str(self.guild.id), value),
                )
            else:
                await db.execute(
                    "INSERT OR IGNORE INTO role_maintenance_ignored_roles (guild_id, role_id) VALUES (?, ?)",
                    (str(self.guild.id), value),
                )
            await db.commit()

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = ModerationSafetyPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class StaffBypassRolesPanel(MaintenanceIgnoredRolesPanel):
    def __init__(self, guild):
        SetupView.__init__(self)
        self.guild = guild
        self.add_item(RoleSelect(self, row=0))

    async def build(self):
        bypass = await get_staff_bypass_roles(str(self.guild.id))
        roles = [self.guild.get_role(int(role_id)) for role_id in bypass]
        embed = base_embed("🛡️ Staff Bypass Roles", "primary")
        embed.description = "Select a role to toggle explicit staff access for freezes and defense controls."
        embed.add_field(
            name="Configured Roles",
            value=", ".join(role.mention for role in roles if role) or "Automatic permission detection",
            inline=False,
        )
        return embed

    async def role_selected(self, interaction, value):
        bypass = await get_staff_bypass_roles(str(self.guild.id))
        if int(value) in bypass:
            bypass.remove(int(value))
        else:
            bypass.add(int(value))
        await set_staff_bypass_roles(str(self.guild.id), bypass)


class ServerDefensePanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild

    async def build(self):
        config = await get_antiraid_config(str(self.guild.id))
        state = await get_antiraid_state(str(self.guild.id))
        active = await active_preset_run(str(self.guild.id))
        embed = base_embed("🚨 Server Defense", "warning" if state["active"] else "primary")
        embed.add_field(name="Anti-Raid Enabled", value=yn(config["enabled"]), inline=True)
        embed.add_field(name="Anti-Raid Active", value=yn(state["active"]), inline=True)
        embed.add_field(
            name="Preset Active",
            value=active["preset_name"] if active else "None",
            inline=True,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Anti-Raid", style=discord.ButtonStyle.primary, row=0)
    async def antiraid(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = AntiRaidPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Lockdown Presets", style=discord.ButtonStyle.primary, row=0)
    async def presets(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = LockdownPresetPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class AntiRaidPanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        options = [
            discord.SelectOption(label="Alert Only", value="alert"),
            discord.SelectOption(label="Lockdown", value="lockdown"),
            discord.SelectOption(label="Quarantine", value="quarantine"),
            discord.SelectOption(label="Lockdown + Quarantine", value="lockdown_quarantine"),
        ]
        self.action_select = Select(
            placeholder="Select response action…", min_values=1, max_values=1,
            options=options, row=0,
        )
        self.action_select.callback = self.action_callback
        self.add_item(self.action_select)
        self.add_item(RoleSelect(self, placeholder="Select quarantine role…", row=1))

    async def build(self):
        config = await get_antiraid_config(str(self.guild.id))
        role = self.guild.get_role(int(config["quarantine_role_id"])) if config["quarantine_role_id"] else None
        embed = base_embed("🚨 Anti-Raid Setup", "primary")
        embed.add_field(name="Enabled", value=yn(config["enabled"]), inline=True)
        embed.add_field(
            name="Threshold",
            value=f"{config['join_threshold']} joins / {config['window_seconds']}s",
            inline=True,
        )
        embed.add_field(name="New Accounts", value=f"< {config['new_account_days']} days", inline=True)
        embed.add_field(name="Action", value=config["action"].replace("_", " ").title(), inline=True)
        embed.add_field(
            name="Duration",
            value=str(datetime.timedelta(seconds=config["duration_seconds"])) if config["duration_seconds"] else "Manual",
            inline=True,
        )
        embed.add_field(name="Quarantine Role", value=role.mention if role else "None", inline=True)
        return embed

    async def action_callback(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        await update_antiraid_config(str(self.guild.id), action=self.action_select.values[0])
        await self.refresh(interaction)

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        await update_antiraid_config(str(self.guild.id), quarantine_role_id=value)

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.primary, row=2)
    async def toggle(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        config = await get_antiraid_config(str(self.guild.id))
        await update_antiraid_config(str(self.guild.id), enabled=0 if config["enabled"] else 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Threshold", style=discord.ButtonStyle.secondary, row=2)
    async def threshold(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(AntiRaidThresholdModal(self))

    @discord.ui.button(label="New Account Age", style=discord.ButtonStyle.secondary, row=2)
    async def new_account(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(AntiRaidNewAccountModal(self))

    @discord.ui.button(label="Duration", style=discord.ButtonStyle.secondary, row=3)
    async def duration(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(AntiRaidDurationModal(self))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = ServerDefensePanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class AntiRaidThresholdModal(Modal):
    def __init__(self, panel):
        super().__init__(title="Anti-Raid Threshold", timeout=180)
        self.panel = panel
        self.joins = TextInput(label="Joins", placeholder="10", max_length=4, required=True)
        self.window = TextInput(label="Window seconds", placeholder="30", max_length=5, required=True)
        self.add_item(self.joins); self.add_item(self.window)

    async def on_submit(self, interaction):
        try:
            joins = int(self.joins.value); window = int(self.window.value)
            if joins < 2 or window < 5: raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a valid threshold and window.", ephemeral=True); return
        await update_antiraid_config(str(self.panel.guild.id), join_threshold=joins, window_seconds=window)
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class AntiRaidNewAccountModal(Modal):
    def __init__(self, panel):
        super().__init__(title="New Account Age", timeout=180)
        self.panel = panel
        self.days = TextInput(label="Days", placeholder="7", max_length=4, required=True)
        self.add_item(self.days)

    async def on_submit(self, interaction):
        try: days = int(self.days.value)
        except ValueError:
            await interaction.response.send_message("Enter a valid number of days.", ephemeral=True); return
        await update_antiraid_config(str(self.panel.guild.id), new_account_days=days)
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class AntiRaidDurationModal(Modal):
    def __init__(self, panel):
        super().__init__(title="Anti-Raid Duration", timeout=180)
        self.panel = panel
        self.duration = TextInput(label="Duration (blank = manual)", placeholder="15m", max_length=20, required=False)
        self.add_item(self.duration)

    async def on_submit(self, interaction):
        value = self.duration.value.strip()
        if value:
            from cogs.channel_tools import parse_duration
            try: delta = parse_duration(value, datetime.timedelta(days=7))
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True); return
            duration_seconds = int(delta.total_seconds())
        else:
            duration_seconds = None
        await update_antiraid_config(str(self.panel.guild.id), duration_seconds=duration_seconds)
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class LockdownPresetPanel(SetupView):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        options = [
            discord.SelectOption(label="Raid", value="raid"),
            discord.SelectOption(label="Verification", value="verification"),
            discord.SelectOption(label="Maintenance", value="maintenance"),
        ]
        self.preset_select = Select(
            placeholder="Run a built-in preset…", min_values=1, max_values=1,
            options=options, row=0,
        )
        self.preset_select.callback = self.run_builtin
        self.add_item(self.preset_select)

    async def build(self):
        active = await active_preset_run(str(self.guild.id))
        embed = base_embed("🔒 Lockdown Preset Setup", "primary")
        embed.add_field(
            name="Active Preset",
            value=active["preset_name"] if active else "None",
            inline=True,
        )
        embed.add_field(
            name="Built-ins",
            value=", ".join(BUILTIN_PRESETS.keys()),
            inline=True,
        )
        embed.description = "Run a built-in preset below. Advanced custom presets remain available through `/lockdown preset`."
        return embed

    async def run_builtin(self, interaction):
        if not await self.guard(interaction):
            return
        preset_name = self.preset_select.values[0]
        await interaction.response.defer(ephemeral=True)
        result = await start_preset_run(
            self.guild,
            BUILTIN_PRESETS[preset_name],
            interaction.user,
            duration=datetime.timedelta(seconds=BUILTIN_PRESETS[preset_name]["duration_seconds"])
            if BUILTIN_PRESETS[preset_name]["duration_seconds"] else None,
            announce_channel=interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None,
            reason="Applied from /setup",
        )
        if result:
            await interaction.followup.send(
                embed=ok_embed(f"Preset **{preset_name}** applied."),
                ephemeral=True,
            )
        else:
            await interaction.followup.send("No channels were locked for that preset.", ephemeral=True)

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Restore Active", style=discord.ButtonStyle.danger, row=1)
    async def restore(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        restored = await restore_active_preset(self.guild, interaction.user, "Restored from /setup")
        if restored:
            await interaction.response.send_message(embed=ok_embed("Active preset restored."), ephemeral=True)
        else:
            await interaction.response.send_message("No active preset was found.", ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = ServerDefensePanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


# ------------------------- Reaction-role setup -------------------------

async def fetch_guild_menus(guild_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT menu_id, name, style, mode, message_id FROM reaction_menus "
            "WHERE guild_id = ? ORDER BY name LIMIT 25",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "menu_id": row[0],
            "name": row[1],
            "style": row[2],
            "mode": row[3],
            "posted": bool(row[4]),
        }
        for row in rows
    ]


class ReactionMenuSelect(Select):
    def __init__(self, panel: "ReactionRolesSetupPanel", menus: list[dict]):
        if menus:
            options = [
                discord.SelectOption(
                    label=menu["name"][:100],
                    value=str(menu["menu_id"]),
                    description=(
                        f"{menu['style']} · {menu['mode']} · "
                        f"{'posted' if menu['posted'] else 'not posted'}"
                    )[:100],
                )
                for menu in menus
            ]
        else:
            options = [
                discord.SelectOption(label="No menus created yet", value="none", default=True)
            ]
        super().__init__(
            placeholder="Select a reaction-role menu…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        if not await self.panel.guard(interaction):
            return
        value = self.values[0]
        self.panel.selected_menu_id = None if value == "none" else int(value)
        await self.panel.refresh(interaction)


class ReactionRolesSetupPanel(SetupView):
    def __init__(self, guild: discord.Guild, menus: list[dict]):
        super().__init__()
        self.guild = guild
        self.selected_menu_id: int | None = None
        self.selected_role_id: int | None = None
        self.menu_select = ReactionMenuSelect(self, menus)
        self.add_item(self.menu_select)
        self.add_item(RoleSelect(self, placeholder="Select a role to add…", row=1))

    @classmethod
    async def create(cls, guild: discord.Guild):
        return cls(guild, await fetch_guild_menus(str(guild.id)))

    async def build(self):
        embed = base_embed("🎭 Reaction-Role Setup", "primary")
        if self.selected_menu_id is None:
            embed.description = (
                "Create a menu below, then select it to add roles and post it. "
                "Advanced management remains available through `/reactionrole`."
            )
            return embed

        menu = await fetch_menu(str(self.guild.id), str(self.selected_menu_id))
        if menu is None:
            self.selected_menu_id = None
            embed.description = "That menu no longer exists."
            return embed

        roles = await fetch_menu_roles(menu["menu_id"])
        lines = []
        for role_data in roles:
            role = self.guild.get_role(int(role_data["role_id"]))
            label = role_data["label"]
            emoji = f"{role_data['emoji']} " if role_data["emoji"] else ""
            lines.append(f"{emoji}**{label}** → {role.mention if role else '`deleted role`'}")

        embed.add_field(name="Menu", value=menu["name"], inline=True)
        embed.add_field(name="Style", value=menu["style"].title(), inline=True)
        embed.add_field(name="Mode", value=menu["mode"].title(), inline=True)
        embed.add_field(
            name="Roles",
            value="\n".join(lines) if lines else "No roles configured",
            inline=False,
        )
        channel = self.guild.get_channel(int(menu["channel_id"])) if menu["channel_id"] else None
        embed.add_field(
            name="Posted",
            value=f"Yes — {channel.mention}" if menu["message_id"] and channel else "No",
            inline=True,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        self.selected_role_id = int(value)

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Create Button Menu", style=discord.ButtonStyle.primary, row=2)
    async def create_buttons(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(ReactionMenuCreateModal(self, "buttons"))

    @discord.ui.button(label="Create Dropdown Menu", style=discord.ButtonStyle.primary, row=2)
    async def create_dropdown(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(ReactionMenuCreateModal(self, "dropdown"))

    @discord.ui.button(label="Add Role", style=discord.ButtonStyle.success, row=3)
    async def add_role(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        if self.selected_menu_id is None or self.selected_role_id is None:
            await interaction.response.send_message(
                "Select both a menu and a role first.",
                ephemeral=True,
            )
            return
        menu = await fetch_menu(str(self.guild.id), str(self.selected_menu_id))
        role = self.guild.get_role(self.selected_role_id)
        if menu is None or role is None:
            await interaction.response.send_message("Menu or role no longer exists.", ephemeral=True)
            return
        await interaction.response.send_modal(ReactionRoleAddModal(self, menu, role))

    @discord.ui.button(label="Post Menu", style=discord.ButtonStyle.success, row=3)
    async def post_menu(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        if self.selected_menu_id is None:
            await interaction.response.send_message("Select a menu first.", ephemeral=True)
            return
        menu = await fetch_menu(str(self.guild.id), str(self.selected_menu_id))
        if menu is None:
            await interaction.response.send_message("Menu no longer exists.", ephemeral=True)
            return
        if menu["message_id"]:
            await interaction.response.send_message("That menu is already posted.", ephemeral=True)
            return
        roles = await fetch_menu_roles(menu["menu_id"])
        if not roles:
            await interaction.response.send_message("Add at least one role first.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Open setup in a text channel.", ephemeral=True)
            return
        view = ReactionMenuView(menu, roles, persistent=True)
        interaction.client.add_view(view)
        try:
            message = await interaction.channel.send(
                embed=await build_menu_embed(menu, roles),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("I couldn't post the menu here.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE reaction_menus SET channel_id = ?, message_id = ? WHERE menu_id = ?",
                (str(interaction.channel.id), str(message.id), menu["menu_id"]),
            )
            await db.commit()
        await interaction.response.send_message(embed=ok_embed("Menu posted."), ephemeral=True)
        await self.refresh_from_message(interaction)

    @discord.ui.button(label="Delete Menu", style=discord.ButtonStyle.danger, row=4)
    async def delete_menu(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        if self.selected_menu_id is None:
            await interaction.response.send_message("Select a menu first.", ephemeral=True)
            return
        menu = await fetch_menu(str(self.guild.id), str(self.selected_menu_id))
        if menu is None:
            await interaction.response.send_message("Menu no longer exists.", ephemeral=True)
            return
        if menu["message_id"] and menu["channel_id"]:
            channel = self.guild.get_channel(int(menu["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(menu["message_id"]))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "DELETE FROM reaction_menu_roles WHERE menu_id = ?",
                (menu["menu_id"],),
            )
            await db.execute(
                "DELETE FROM reaction_menus WHERE menu_id = ?",
                (menu["menu_id"],),
            )
            await db.commit()
        self.selected_menu_id = None
        self.menu_select.options = [
            discord.SelectOption(label="No menus created yet", value="none", default=True)
        ]
        await self.refresh(interaction)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CommunityPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    async def refresh_from_message(self, interaction: discord.Interaction):
        # Posting uses an ephemeral response first, so refresh the setup panel
        # through the original message when available.
        if interaction.message is not None:
            try:
                await interaction.message.edit(embed=await self.build(), view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class ReactionMenuCreateModal(Modal):
    def __init__(self, panel: ReactionRolesSetupPanel, style: str):
        super().__init__(title="Create Reaction Menu", timeout=180)
        self.panel = panel
        self.style = style
        self.name = TextInput(label="Menu name", max_length=80, required=True)
        self.description = TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        self.mode = TextInput(
            label="Mode",
            placeholder="toggle or exclusive",
            max_length=20,
            required=True,
        )
        self.add_item(self.name)
        self.add_item(self.description)
        self.add_item(self.mode)

    async def on_submit(self, interaction: discord.Interaction):
        mode = self.mode.value.strip().lower()
        if mode not in {"toggle", "exclusive"}:
            await interaction.response.send_message("Mode must be `toggle` or `exclusive`.", ephemeral=True)
            return
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                cursor = await db.execute(
                    "INSERT INTO reaction_menus (guild_id, name, description, style, mode, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(self.panel.guild.id),
                        self.name.value.strip(),
                        self.description.value or None,
                        self.style,
                        mode,
                        str(interaction.user.id),
                        str(discord.utils.utcnow()),
                    ),
                )
                await db.commit()
                menu_id = cursor.lastrowid
        except aiosqlite.IntegrityError:
            await interaction.response.send_message("A menu with that name already exists.", ephemeral=True)
            return

        self.panel.selected_menu_id = menu_id
        self.panel.menu_select.options = [
            discord.SelectOption(
                label=self.name.value.strip()[:100],
                value=str(menu_id),
                description=f"{self.style} · {mode}",
            )
        ]
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class ReactionRoleAddModal(Modal):
    def __init__(self, panel: ReactionRolesSetupPanel, menu: dict, role: discord.Role):
        super().__init__(title=f"Add {role.name}"[:45], timeout=180)
        self.panel = panel
        self.menu = menu
        self.role = role
        self.label = TextInput(label="Button label", max_length=80, required=True)
        self.emoji = TextInput(label="Emoji", max_length=100, required=False)
        self.description = TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            max_length=200,
            required=False,
        )
        self.add_item(self.label)
        self.add_item(self.emoji)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        existing = await fetch_menu_roles(self.menu["menu_id"])
        if len(existing) >= 25:
            await interaction.response.send_message("A menu can contain at most 25 roles.", ephemeral=True)
            return
        if any(int(item["role_id"]) == self.role.id for item in existing):
            await interaction.response.send_message("That role is already on this menu.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO reaction_menu_roles (menu_id, role_id, label, emoji, description, position) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.menu["menu_id"],
                    str(self.role.id),
                    self.label.value.strip(),
                    self.emoji.value.strip() or None,
                    self.description.value or None,
                    len(existing) + 1,
                ),
            )
            await db.commit()
        interaction.client.loop.create_task(
            refresh_posted_menu(interaction.client, self.menu["menu_id"])
        )
        await interaction.response.edit_message(embed=await self.panel.build(), view=self.panel)


class ReactionRoleSimplePanel(SetupView):
    """Simple emoji-on-message reaction role setup."""

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def build(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM reaction_role_bindings WHERE guild_id = ?",
                (str(self.guild.id),),
            ) as cursor:
                (count,) = await cursor.fetchone()
        embed = base_embed("🎭 Reaction Roles", "primary")
        embed.description = (
            "Use emoji reactions on any existing message. You can enter role IDs directly, "
            "so a long role list is not a problem."
        )
        embed.add_field(name="Active Bindings", value=str(count), inline=True)
        embed.add_field(
            name="Add Binding",
            value="Click **Add Binding** and enter the channel, message ID, role ID, and emoji.",
            inline=False,
        )
        embed.add_field(
            name="Create Panel Message",
            value="Click **Create Panel Message** to post a simple message here, then bind emojis to it.",
            inline=False,
        )
        return embed

    async def channel_selected(self, interaction, value):
        pass

    async def role_selected(self, interaction, value):
        pass

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Add Binding", style=discord.ButtonStyle.success, row=1)
    async def add_binding(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.reaction_roles import ReactionRoleBindModal
        await interaction.response.send_modal(ReactionRoleBindModal(self.guild))

    @discord.ui.button(label="Create Panel Message", style=discord.ButtonStyle.primary, row=1)
    async def create_panel(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(ReactionPanelMessageModal(self.guild))

    @discord.ui.button(label="List Bindings", style=discord.ButtonStyle.secondary, row=2)
    async def list_bindings(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT binding_id, channel_id, message_id, role_id, emoji "
                "FROM reaction_role_bindings WHERE guild_id = ? ORDER BY binding_id DESC LIMIT 10",
                (str(self.guild.id),),
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No reaction bindings are configured.", ephemeral=True)
            return
        embed = base_embed("🎭 Reaction Bindings", "primary")
        for binding_id, channel_id, message_id, role_id, emoji in rows:
            channel = self.guild.get_channel(int(channel_id))
            role = self.guild.get_role(int(role_id))
            embed.add_field(
                name=f"#{binding_id} · {emoji}",
                value=(
                    f"Role: {role.mention if role else f'`{role_id}`'}\n"
                    f"Channel: {channel.mention if channel else f'`{channel_id}`'}\n"
                    f"Message ID: `{message_id}`"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = CommunityPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)


class ReactionPanelMessageModal(Modal):
    def __init__(self, guild: discord.Guild):
        super().__init__(title="Create Reaction Panel Message", timeout=180)
        self.guild = guild
        self.title_input = TextInput(label="Title", max_length=120, required=True)
        self.description = TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.add_item(self.title_input)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Open setup in a text channel.", ephemeral=True)
            return
        embed = base_embed(self.title_input.value[:120], "primary")
        if self.description.value:
            embed.description = self.description.value
        try:
            message = await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("I couldn't send the panel message here.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=ok_embed(
                f"Panel message created: [jump]({message.jump_url})\n"
                f"Message ID: `{message.id}`"
            ),
            ephemeral=True,
        )


# This module is imported by the setup hub rather than functioning as a cog.
async def setup(bot):
    return None
