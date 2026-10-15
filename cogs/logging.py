import datetime

import discord
from discord import app_commands
from discord.ext import commands

from cog_utils import LOG_EVENTS, is_log_event_enabled, set_log_event


class Logging(commands.Cog):
    """Customizable server logging: pick exactly which events reach your log channel."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="logsettings", description="Toggle which events are sent to the log channel.")
    @app_commands.describe(event="The event category to toggle")
    @app_commands.choices(event=[
        app_commands.Choice(name=name, value=key) for key, name in LOG_EVENTS.items()
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logsettings(self, interaction: discord.Interaction, event: app_commands.Choice[str]):
        guild_id = str(interaction.guild.id)
        currently_enabled = await is_log_event_enabled(guild_id, event.value)
        await set_log_event(guild_id, event.value, not currently_enabled)

        state = "\u2705 Enabled" if not currently_enabled else "\u274c Disabled"
        await interaction.response.send_message(
            f"{state} logging for **{event.name}**.",
            ephemeral=True,
        )

    @app_commands.command(name="logstatus", description="Show which log events are currently enabled.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logstatus(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        embed = discord.Embed(title="\U0001f4cb Log Event Status", color=0x5865f2)
        lines = []
        for key, name in LOG_EVENTS.items():
            mark = "\u2705" if await is_log_event_enabled(guild_id, key) else "\u274c"
            lines.append(f"{mark} {name}")
        embed.description = "\n".join(lines)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Logging(bot))
