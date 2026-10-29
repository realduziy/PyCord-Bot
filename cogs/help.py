from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Modal, Select, TextInput

from ui_common import base_embed, truncate


COMMANDS_PER_PAGE = 8

# Presentation metadata only. Cogs not listed here are still discovered
# automatically using their class/module name.
CATEGORY_META = {
    "Events": ("⚙️", "Utilities"),
    "Moderation": ("🛡️", "Moderation"),
    "LockdownGroup": ("🔒", "Lockdown"),
    "Logging": ("📋", "Logging"),
    "Verification": ("🎟️", "Verification"),
    "Voice": ("🔊", "Voice"),
    "Fun": ("🎮", "Fun & Info"),
    "Tickets": ("🎫", "Tickets"),
    "Help": ("❓", "Help"),
}


@dataclass
class HelpPage:
    category_key: str
    category_label: str
    emoji: str
    embed: discord.Embed
    command_count: int
    first_in_category: bool


def _is_chat_input(command) -> bool:
    """Only list slash commands; context menus need a different format."""
    command_type = getattr(command, "type", discord.AppCommandType.chat_input)
    return command_type == discord.AppCommandType.chat_input


def _command_children(command) -> list:
    return list(getattr(command, "commands", None) or [])


def _category_key(command) -> str:
    binding = getattr(command, "binding", None)
    if binding is not None:
        key = binding.__class__.__name__
    else:
        callback = getattr(command, "callback", None)
        module = getattr(callback, "__module__", "")
        key = (
            module.split(".", 1)[1].title().replace("_", " ")
            if module.startswith("cogs.")
            else "Other"
        )

    # Groups created as helper classes should appear as a command category,
    # not by their internal implementation class name.
    return "Lockdown" if key == "LockdownGroup" else key


def _category_style(key: str) -> tuple[str, str]:
    return CATEGORY_META.get(key, ("📁", key.replace("_", " ").title()))


def _walk_commands(commands_):
    for command in commands_:
        if not _is_chat_input(command):
            continue

        yield command

        children = _command_children(command)
        if children:
            yield from _walk_commands(children)


def _usage(command) -> str:
    parts = [f"/{command.qualified_name}"]

    for parameter in getattr(command, "parameters", []) or []:
        parts.append(
            f"<{parameter.name}>"
            if parameter.required
            else f"[{parameter.name}]"
        )

    return " ".join(parts)


def _command_detail_embed(command, avatar_url: str) -> discord.Embed:
    embed = base_embed(f"/{command.qualified_name}", "primary")
    embed.set_thumbnail(url=avatar_url)

    description = command.description or "No description provided."
    children = _command_children(command)

    if children:
        embed.description = description
        lines = []

        for child in sorted(children, key=lambda item: item.qualified_name):
            child_description = child.description or "No description provided."
            lines.append(
                f"`/{child.qualified_name}` — {truncate(child_description, 100)}"
            )

        embed.add_field(
            name=f"Subcommands ({len(children)})",
            value=truncate("\n".join(lines), 1024),
            inline=False,
        )
        embed.set_footer(text="Use /help <subcommand> to inspect its options.")
        return embed

    parameters = list(getattr(command, "parameters", []) or [])
    embed.description = f"{description}\n\n**Usage:**\n`{_usage(command)}`"

    for parameter in parameters[:25]:
        requirement = "required" if parameter.required else "optional"
        parameter_description = parameter.description or "No description provided."
        embed.add_field(
            name=f"`{parameter.name}` ({requirement})",
            value=truncate(parameter_description, 300),
            inline=False,
        )

    if len(parameters) > 25:
        embed.set_footer(text="Additional options were omitted from help.")

    return embed


def _build_help_pages(bot: commands.Bot) -> list[HelpPage]:
    """Build fresh pages on every invocation so command changes are reflected."""

    top_level_commands = [
        command
        for command in bot.tree.get_commands()
        if _is_chat_input(command)
    ]

    grouped: dict[str, list] = {}
    for command in top_level_commands:
        # A help entry for /help inside the browser is redundant.
        if command.qualified_name == "help":
            continue
        grouped.setdefault(_category_key(command), []).append(command)

    page_specs = []
    for key in sorted(grouped, key=lambda item: _category_style(item)[1].casefold()):
        emoji, label = _category_style(key)
        commands_ = sorted(grouped[key], key=lambda item: item.qualified_name)
        chunks = [
            commands_[index:index + COMMANDS_PER_PAGE]
            for index in range(0, len(commands_), COMMANDS_PER_PAGE)
        ]

        for chunk_number, chunk in enumerate(chunks, start=1):
            page_specs.append((
                key,
                label,
                emoji,
                chunk,
                len(commands_),
                chunk_number,
                len(chunks),
            ))

    total_pages = len(page_specs)
    pages: list[HelpPage] = []

    for page_number, (
        key,
        label,
        emoji,
        commands_chunk,
        command_count,
        chunk_number,
        chunk_total,
    ) in enumerate(page_specs, start=1):
        title = f"{emoji} {label}"
        if chunk_total > 1:
            title += f" · {chunk_number}/{chunk_total}"

        embed = base_embed(
            title,
            "primary",
            f"Showing {len(commands_chunk)} of {command_count} command(s) in this category.",
        )

        for command in commands_chunk:
            value = command.description or "No description provided."
            children = _command_children(command)

            if children:
                value += (
                    f"\n↳ **{len(children)} subcommand(s)** — "
                    f"use `/help {command.qualified_name}` for details."
                )

            embed.add_field(
                name=f"/{command.qualified_name}",
                value=truncate(value, 350),
                inline=False,
            )

        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)

        embed.set_footer(
            text=(
                f"Help page {page_number}/{total_pages} · "
                "Pick a category or use /help <command>"
            )
        )

        pages.append(HelpPage(
            category_key=key,
            category_label=label,
            emoji=emoji,
            embed=embed,
            command_count=command_count,
            first_in_category=chunk_number == 1,
        ))

    return pages


class HelpCategorySelect(Select):
    def __init__(self, help_view: "HelpView"):
        self.help_view = help_view
        self._page_by_option: dict[str, int] = {}
        options = []

        # Discord allows at most 25 select options. The page buttons still
        # make categories beyond the first 25 reachable.
        for page_number, page in enumerate(help_view.pages):
            if not page.first_in_category or len(options) >= 25:
                continue

            value = str(page_number)
            self._page_by_option[value] = page_number
            options.append(discord.SelectOption(
                label=page.category_label[:100],
                description=f"{page.command_count} command(s)",
                emoji=page.emoji,
                value=value,
            ))

        super().__init__(
            placeholder="Jump to a category…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not await self.help_view.check_owner(interaction):
            return

        await self.help_view.show_page(
            interaction,
            self._page_by_option[self.values[0]],
        )


class HelpSearchModal(Modal):
    def __init__(self, help_view: "HelpView"):
        super().__init__(title="Find a Command", timeout=120)
        self.help_view = help_view
        self.query = TextInput(
            label="Command name",
            placeholder="e.g. clear, voicehub, ticket",
            max_length=100,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        command = self.help_view.find_command(self.query.value)

        if command is None:
            await interaction.response.send_message(
                embed=base_embed(
                    "❓ Command Not Found",
                    "warning",
                    "I couldn't find a command with that name.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=_command_detail_embed(command, self.help_view.avatar_url),
            ephemeral=True,
        )


class HelpView(discord.ui.View):
    def __init__(
        self,
        pages: list[HelpPage],
        command_index: dict[str, app_commands.Command],
        owner: discord.abc.User,
        avatar_url: str,
    ):
        super().__init__(timeout=300)
        self.pages = pages
        self.command_index = command_index
        self.owner_id = owner.id
        self.avatar_url = avatar_url
        self.current_page = 0
        self.message: discord.Message | None = None

        self.category_select = HelpCategorySelect(self)
        self.add_item(self.category_select)
        self._sync_buttons()

    async def check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "This help panel belongs to someone else. Run `/help` to open your own.",
            ephemeral=True,
        )
        return False

    def _sync_buttons(self):
        for child in self.children:
            if not isinstance(child, Button):
                continue
            if child.custom_id == "help_previous":
                child.disabled = self.current_page == 0
            elif child.custom_id == "help_next":
                child.disabled = self.current_page == len(self.pages) - 1

    async def show_page(self, interaction: discord.Interaction, page_number: int):
        self.current_page = max(0, min(page_number, len(self.pages) - 1))
        self._sync_buttons()

        await interaction.response.edit_message(
            embed=self.pages[self.current_page].embed,
            view=self,
        )

    @staticmethod
    def _matching_names(
        command_index: dict[str, app_commands.Command],
        query: str,
        limit: int = 25,
    ) -> list[str]:
        normalized = query.strip().lower().lstrip("/")
        if not normalized:
            return []

        prefix_matches = []
        substring_matches = []
        fuzzy_matches = []

        for name in command_index:
            lowered = name.lower()
            if lowered.startswith(normalized):
                prefix_matches.append(name)
            elif normalized in lowered:
                substring_matches.append(name)
            elif all(word in lowered for word in normalized.split() if word):
                fuzzy_matches.append(name)

        ordered = (
            sorted(prefix_matches, key=lambda item: (len(item), item))
            + sorted(substring_matches, key=lambda item: (len(item), item))
            + sorted(fuzzy_matches, key=lambda item: (len(item), item))
        )
        return ordered[:limit]

    def find_command(self, query: str):
        matches = self._matching_names(self.command_index, query, limit=1)
        return self.command_index[matches[0]] if matches else None

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        emoji="◀️",
        custom_id="help_previous",
        row=1,
    )
    async def previous_page(self, interaction: discord.Interaction, button: Button):
        if not await self.check_owner(interaction):
            return
        await self.show_page(interaction, self.current_page - 1)

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary,
        emoji="▶️",
        custom_id="help_next",
        row=1,
    )
    async def next_page(self, interaction: discord.Interaction, button: Button):
        if not await self.check_owner(interaction):
            return
        await self.show_page(interaction, self.current_page + 1)

    @discord.ui.button(
        label="Search",
        style=discord.ButtonStyle.primary,
        emoji="🔎",
        custom_id="help_search",
        row=2,
    )
    async def search(self, interaction: discord.Interaction, button: Button):
        if not await self.check_owner(interaction):
            return
        await interaction.response.send_modal(HelpSearchModal(self))

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji="✖️",
        custom_id="help_close",
        row=2,
    )
    async def close(self, interaction: discord.Interaction, button: Button):
        if not await self.check_owner(interaction):
            return

        await interaction.response.edit_message(view=None)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        self.stop()


class Help(commands.Cog):
    """Dynamic, interactive slash-command help."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _command_index(bot: commands.Bot) -> dict[str, app_commands.Command]:
        return {
            command.qualified_name: command
            for command in _walk_commands(bot.tree.get_commands())
        }

    async def _command_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        command_index = self._command_index(interaction.client)
        matches = HelpView._matching_names(command_index, current, limit=25)

        return [
            app_commands.Choice(name=f"/{name}", value=name)
            for name in matches
        ]

    @app_commands.command(
        name="help",
        description="Browse commands or inspect a specific command.",
    )
    @app_commands.describe(command="Optional command or command group to inspect.")
    @app_commands.autocomplete(command=_command_autocomplete)
    async def help(
        self,
        interaction: discord.Interaction,
        command: Optional[str] = None,
    ):
        command_index = self._command_index(self.bot)
        avatar_url = interaction.client.user.display_avatar.url

        if command and command.strip():
            matches = HelpView._matching_names(command_index, command, limit=1)
            match = command_index[matches[0]] if matches else None

            if match is None:
                suggestions = HelpView._matching_names(command_index, command, limit=5)
                suggestion_text = (
                    "\n".join(f"• `/{name}`" for name in suggestions)
                    if suggestions
                    else "No close matches were found."
                )

                await interaction.response.send_message(
                    embed=base_embed(
                        "❓ Command Not Found",
                        "warning",
                        f"I couldn't find `{command}`.\n\n{suggestion_text}",
                    ),
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                embed=_command_detail_embed(match, avatar_url),
                ephemeral=True,
            )
            return

        pages = _build_help_pages(self.bot)
        if not pages:
            await interaction.response.send_message(
                embed=base_embed(
                    "❓ Help",
                    "neutral",
                    "No slash commands are currently loaded.",
                ),
                ephemeral=True,
            )
            return

        view = HelpView(pages, command_index, interaction.user, avatar_url)
        await interaction.response.send_message(
            embed=pages[0].embed,
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
