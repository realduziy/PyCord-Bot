import ast
import operator
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View

from cog_utils import fetch_json


# --- Safe math evaluator: parses the AST instead of using eval() ---
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval(expression: str):
    node = ast.parse(expression, mode="eval").body

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("Exponent too large")
            return _OPS[type(n.op)](left, right)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("Unsupported expression")

    return ev(node)


class HelpPaginator(View):
    def __init__(self, pages):
        super().__init__(timeout=120)
        self.pages = pages
        self.cur_page = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self):
        # Disable arrows at the ends so the layout feels intentional
        self.prev_page.disabled = self.cur_page == 0
        self.next_page.disabled = self.cur_page == len(self.pages) - 1

    @discord.ui.button(label="\u25c0\ufe0f", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: Button):
        if self.cur_page > 0:
            self.cur_page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.cur_page], view=self)

    @discord.ui.button(label="\u25b6\ufe0f", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: Button):
        if self.cur_page < len(self.pages) - 1:
            self.cur_page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.cur_page], view=self)

    async def on_timeout(self):
        # Freeze the arrows and persist the disabled state on the message
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="hello", description="Say hello to the bot.")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message("Hello, I am a bot made by the one and only duziy!", ephemeral=True)

    @app_commands.command(name="ping", description="Pings the bot.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f'Pong! `{round(self.bot.latency * 1000)}ms`', ephemeral=True)

    @app_commands.command(name="math", description="Performs basic math operations safely.")
    async def math(self, interaction: discord.Interaction, expression: app_commands.Range[str, 1, 200]):
        try:
            result = safe_eval(expression)
            await interaction.response.send_message(f"Result: `{result}`", ephemeral=True)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message("Invalid mathematical expression.", ephemeral=True)

    @app_commands.command(name="dadjoke", description="Sends a random dad joke.")
    @app_commands.checks.cooldown(1, 5)
    async def dadjoke(self, interaction: discord.Interaction):
        request = await fetch_json("https://icanhazdadjoke.com/")
        if request and 'joke' in request:
            await interaction.response.send_message(request['joke'])
        else:
            await interaction.response.send_message("Could not fetch a dad joke right now, try again later.",
                                                    ephemeral=True)

    @app_commands.command(name="advice", description="Get random advice.")
    @app_commands.checks.cooldown(1, 5)
    async def advice(self, interaction: discord.Interaction):
        request = await fetch_json("https://api.adviceslip.com/advice")
        if request and 'slip' in request and 'advice' in request['slip']:
            await interaction.response.send_message(request['slip']['advice'])
        else:
            await interaction.response.send_message("Could not fetch advice right now, try again later.",
                                                    ephemeral=True)

    @app_commands.command(name="cat", description="Sends a random cat image.")
    @app_commands.checks.cooldown(1, 5)
    async def cat(self, interaction: discord.Interaction):
        request = await fetch_json("https://api.thecatapi.com/v1/images/search")
        if request and isinstance(request, list) and request[0].get('url'):
            embed = discord.Embed(title="Here's a cat!")
            embed.set_image(url=request[0]['url'])
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Could not fetch a cat picture right now.", ephemeral=True)

    @app_commands.command(name="dog", description="Sends a random dog image.")
    @app_commands.checks.cooldown(1, 5)
    async def dog(self, interaction: discord.Interaction):
        request = await fetch_json("https://dog.ceo/api/breeds/image/random")
        if request and request.get('status') == 'success':
            embed = discord.Embed(title="Here's a dog!")
            embed.set_image(url=request['message'])
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Could not fetch a dog picture right now.", ephemeral=True)

    async def _fetch_meme(self):
        """Reddit first, imgflip fallback. Returns a dict or None."""
        data = await fetch_json(
            "https://www.reddit.com/r/dankmemes/hot.json?limit=50",
            headers={"User-Agent": "duziy-utility-bot/1.0"}
        )
        if data and data.get("data"):
            posts = [
                p["data"] for p in data["data"]["children"]
                if p["data"].get("post_hint") == "image" and not p["data"].get("over_18")
            ]
            if posts:
                post = random.choice(posts)
                return {
                    "title": post.get("title"),
                    "url": post.get("url"),
                    "subreddit": post.get("subreddit"),
                    "ups": post.get("ups"),
                }
        # Fallback: imgflip popular templates
        data = await fetch_json("https://api.imgflip.com/get_memes")
        if data and data.get("success") and data.get("data", {}).get("memes"):
            meme = random.choice(data["data"]["memes"])
            return {"title": meme.get("name"), "url": meme.get("url"), "subreddit": "imgflip", "ups": None}
        return None

    @app_commands.command(name="meme", description="Sends a random meme.")
    @app_commands.checks.cooldown(1, 5)
    async def meme(self, interaction: discord.Interaction):
        meme = await self._fetch_meme()
        if meme:
            embed = discord.Embed(title=meme["title"] or "Here's a meme!", color=discord.Color.random())
            embed.set_image(url=meme["url"])
            footer = f"r/{meme['subreddit']}"
            if meme.get("ups") is not None:
                footer += f" \u00b7 \u2b06\ufe0f {meme['ups']}"
            embed.set_footer(text=footer)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Could not fetch a meme right now.", ephemeral=True)

    @app_commands.command(name="eightball", description="Asks the magic eightball a question.")
    @app_commands.checks.cooldown(1, 5)
    async def eightball(self, interaction: discord.Interaction, question: str):
        responses = [
            "It is certain.", "Without a doubt.", "Yes - definitely.", "You may rely on it.",
            "As I see it, yes.", "Most likely.", "Yes.", "Signs point to yes.",
            "Ask again later.", "Better not tell you now.", "Cannot predict now.",
            "Concentrate and ask again.", "Don't count on it.", "Outlook not so good.",
            "My sources say no.", "Very doubtful."
        ]
        await interaction.response.send_message(
            f"\U0001f3b1 **Question:** {question}\n\U0001f3b1 **Answer:** {random.choice(responses)}",
            ephemeral=True)

    @app_commands.command(name="roll", description="Rolls a dice with the specified number of sides.")
    async def roll(self, interaction: discord.Interaction, sides: app_commands.Range[int, 1, 1000000]):
        result = random.randint(1, sides)
        await interaction.response.send_message(f"Rolling a {sides}-sided dice... You rolled a **{result}**!")

    @app_commands.command(name="howgay", description="Get how gay you are.")
    @app_commands.checks.cooldown(1, 5)
    async def howgay(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        gay_percent = random.randint(0, 100)
        await interaction.response.send_message(
            f"{target.mention} is **{gay_percent}%** gay \U0001f3f3\ufe0f\u200d\U0001f308", ephemeral=True)

    @app_commands.command(name="avatar", description="Get the avatar of a user.")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        embed = discord.Embed(title=f"{target.name}'s Avatar")
        embed.set_image(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Shows information about the server.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        embed = discord.Embed(title=f"{guild.name}", description=guild.description or "",
                              color=discord.Color.blurple())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
        embed.add_field(name="ID", value=guild.id, inline=True)
        embed.add_field(name="Created at", value=guild.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        embed.add_field(name="Members", value=guild.member_count, inline=True)
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)
        embed.add_field(name="Channels", value=len(guild.channels), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="announce", description="Announces a message.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def announce(self, interaction: discord.Interaction, message: str):
        embed = discord.Embed(description=message, color=0xFF0000)
        embed.set_footer(text=f"Announced by {interaction.user.name}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Shows all commands.")
    async def help_cmd(self, interaction: discord.Interaction):
        # Built dynamically from the command tree so it never drifts out of date
        category_titles = {
            "Events": "\u2699\ufe0f Utilities",
            "Moderation": "\U0001f6e1\ufe0f Moderation",
            "Fun": "\U0001f3ae Fun & Info",
            "Tickets": "\U0001f3ab Tickets",
        }
        categories = []
        for cog_name, title in category_titles.items():
            cog = interaction.client.get_cog(cog_name)
            if not cog:
                continue
            cmds = sorted(cog.get_app_commands(), key=lambda c: c.name)
            if cmds:
                categories.append((title, cmds))

        pages = []
        total = len(categories)
        for i, (title, cmds) in enumerate(categories):
            embed = discord.Embed(title=title, color=discord.Color.blurple())
            for cmd in cmds:
                embed.add_field(name=f"/{cmd.name}", value=cmd.description or "No description", inline=True)
            embed.set_thumbnail(url=interaction.client.user.display_avatar.url)
            embed.set_footer(text=f"Page {i + 1}/{total} \u00b7 use the buttons to browse")
            pages.append(embed)

        view = HelpPaginator(pages)
        await interaction.response.send_message(embed=pages[0], view=view)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(Fun(bot))
