import discord
from discord.ui import Button

from ui_common import PanelView, base_embed, ok_embed, yn

from cogs.leveling import get_level_config, update_level_config
from cogs.mod_workflows import get_role_maintenance_enabled
from cogs.server_defense import get_antiraid_config, update_antiraid_config
from cogs.suggestions import get_suggestion_channel_id


class WizardHomePanel(PanelView):
    """Simple task-oriented replacement for the dense setup hub."""

    def __init__(self):
        super().__init__()

    def build(self):
        embed = base_embed("🛠️ Server Setup", "primary")
        embed.description = (
            "Choose what you want to configure. **Quick Start** covers the most common settings, "
            "or jump directly into a section."
        )
        embed.add_field(name="Quick Start", value="Core settings with sensible defaults", inline=True)
        embed.add_field(name="Community", value="Suggestions, counting, and leveling", inline=True)
        embed.add_field(name="Reaction Roles", value="Emoji roles on any message", inline=True)
        embed.add_field(name="Moderation Safety", value="Role maintenance and staff access", inline=True)
        embed.add_field(name="Server Defense", value="Anti-raid and lockdown presets", inline=True)
        embed.add_field(name="Advanced Setup", value="Original detailed panels", inline=True)
        return embed

    @discord.ui.button(label="Quick Start", style=discord.ButtonStyle.success, row=0)
    async def quick_start(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        panel = QuickStartPanel(interaction.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Community", style=discord.ButtonStyle.primary, row=0)
    async def community(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.setup_panels import CommunityPanel
        panel = CommunityPanel(interaction.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Reaction Roles", style=discord.ButtonStyle.primary, row=0)
    async def reaction_roles(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.setup_panels import ReactionRoleSimplePanel
        panel = ReactionRoleSimplePanel(interaction.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Moderation Safety", style=discord.ButtonStyle.primary, row=1)
    async def moderation_safety(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.setup_panels import ModerationSafetyPanel
        panel = ModerationSafetyPanel(interaction.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Server Defense", style=discord.ButtonStyle.primary, row=1)
    async def server_defense(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.setup_panels import ServerDefensePanel
        panel = ServerDefensePanel(interaction.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Advanced Setup", style=discord.ButtonStyle.secondary, row=1)
    async def advanced(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs._hub import SetupHubView, build_setup_hub_embed
        advanced = SetupHubView()
        await interaction.response.edit_message(
            embed=await build_setup_hub_embed(interaction.guild),
            view=advanced,
        )


class QuickStartPanel(PanelView):
    """One-page guided checklist for the most common settings."""

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def build(self):
        suggestion_channel_id = await get_suggestion_channel_id(str(self.guild.id))
        suggestion_channel = self.guild.get_channel(int(suggestion_channel_id)) if suggestion_channel_id else None
        leveling = await get_level_config(str(self.guild.id))
        maintenance = await get_role_maintenance_enabled(str(self.guild.id))
        antiraid = await get_antiraid_config(str(self.guild.id))

        embed = base_embed("⚡ Quick Start", "primary")
        embed.description = (
            "Use the buttons below for the core settings. Advanced options remain available from the home screen."
        )
        embed.add_field(
            name="Suggestions",
            value=f"{yn(suggestion_channel is not None)}\n{suggestion_channel.mention if suggestion_channel else 'No channel selected'}",
            inline=True,
        )
        embed.add_field(name="Leveling", value=yn(leveling["enabled"]), inline=True)
        embed.add_field(name="Role Maintenance", value=yn(maintenance), inline=True)
        embed.add_field(name="Anti-Raid", value=yn(antiraid["enabled"]), inline=True)
        embed.add_field(
            name="Recommended Community Defaults",
            value="15–25 XP · 60s cooldown · stacking rewards · auto-create roles",
            inline=False,
        )
        return embed

    @discord.ui.button(label="Set Log Channel", style=discord.ButtonStyle.primary, row=0)
    async def log_channel(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_message(
            "Use `/setup` → **Advanced Setup** → **Logging** to choose the log channel.",
            ephemeral=True,
        )

    @discord.ui.button(label="Community Setup", style=discord.ButtonStyle.primary, row=0)
    async def community(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.setup_panels import CommunityPanel
        panel = CommunityPanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Toggle Leveling", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_leveling(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        config = await get_level_config(str(self.guild.id))
        await update_level_config(str(self.guild.id), enabled=0 if config["enabled"] else 1)
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Recommended Leveling", style=discord.ButtonStyle.success, row=1)
    async def recommended_leveling(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await update_level_config(
            str(self.guild.id),
            enabled=1,
            xp_min=15,
            xp_max=25,
            cooldown_seconds=60,
            stack_roles=1,
            auto_create_roles=1,
        )
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Toggle Role Maintenance", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_maintenance(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        import aiosqlite
        from cog_utils import DB_FILE
        enabled = await get_role_maintenance_enabled(str(self.guild.id))
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO role_maintenance_settings (guild_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
                (str(self.guild.id), 0 if enabled else 1),
            )
            await db.commit()
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Anti-Raid: Off", style=discord.ButtonStyle.danger, row=1)
    async def antiraid_off(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await update_antiraid_config(str(self.guild.id), enabled=0)
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Anti-Raid: Recommended", style=discord.ButtonStyle.success, row=2)
    async def antiraid_recommended(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        await update_antiraid_config(
            str(self.guild.id),
            enabled=1,
            join_threshold=10,
            window_seconds=30,
            new_account_days=7,
            action="lockdown_quarantine",
            duration_seconds=900,
        )
        await interaction.response.edit_message(embed=await self.build(), view=self)

    @discord.ui.button(label="Server Defense", style=discord.ButtonStyle.primary, row=2)
    async def defense(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        from cogs.setup_panels import ServerDefensePanel
        panel = ServerDefensePanel(self.guild)
        await interaction.response.edit_message(embed=await panel.build(), view=panel)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self.guard(interaction):
            return
        home = WizardHomePanel()
        await interaction.response.edit_message(embed=home.build(), view=home)


# Imported by the setup command; no cog is registered from this module.
async def setup(bot):
    return None
