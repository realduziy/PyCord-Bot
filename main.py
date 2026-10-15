import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    sys.exit("ERROR: TOKEN not found. Create a .env file with TOKEN=your_token.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("duziy-bot")

intents = discord.Intents.all()


class DuziyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=".", intents=intents)

    async def setup_hook(self):
        """Runs ONCE before login: DB init, cogs, persistent views, sync."""
        from cog_utils import init_db
        await init_db()

        await self.load_extensions()

        # Persistent views must be registered before any interactions arrive
        from cogs.tickets import TicketControlsView, TicketPanelView
        from cogs.verification import VerifyPanelView
        self.add_view(TicketPanelView())
        self.add_view(TicketControlsView())
        self.add_view(VerifyPanelView())

        # Global slash-command error handling from the start
        self.tree.on_error = self.handle_app_error

        # Commands are server-only: each server's config is keyed by guild ID,
        # and DM usage would just error on missing guild data.
        async def guild_only_check(interaction: discord.Interaction) -> bool:
            if interaction.guild is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "\U0001f4e2 This bot only works inside servers — "
                        "invite it and use the commands there.", ephemeral=True)
                return False
            return True
        self.tree.interaction_check = guild_only_check

        synced = await self.tree.sync()
        log.info("Slash commands synced: %d", len(synced))

    async def load_extensions(self):
        cogs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cogs")
        for filename in sorted(os.listdir(cogs_dir)):
            if filename.endswith(".py") and not filename.startswith("_"):
                await self.load_extension(f"cogs.{filename[:-3]}")
                log.info("Loaded extension: cogs.%s", filename[:-3])

    async def handle_app_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Global slash-command error handler with friendly, specific messages."""
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down! Try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f"I'm missing permissions for that: {', '.join(error.missing_permissions)}"
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use this command."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You can't use this command here."
        else:
            log.error("Unhandled app command error: %s", error, exc_info=error)
            msg = "Something went wrong while running that command. The error has been logged."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s) | discord.py %s",
                 self.user, self.user.id, discord.__version__)


async def main():
    async with DuziyBot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass