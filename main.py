import discord
import os.path
import json
import os
import requests
import random
import asyncio
import aiohttp
import re
from discord.ext import commands
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
token = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(intents=intents)

@bot.event
async def on_ready():
    await bot.wait_until_ready()

    statuses = [f"listening on {len(bot.guilds)} server's", "Need help? do /help"]

    while not bot.is_closed():

        status = random.choice(statuses)

        await bot.change_presence(activity=discord.Game(name=status))

        await asyncio.sleep(5)

bot.loop.create_task(on_ready())
print("Bot is ready!")

##########################################################

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.respond(f"You are on cooldown. Try again in {error.retry_after:.2f}s.", delete_after=10)
    elif isinstance(error, commands.NoPrivateMessage):
        pass
    elif isinstance(error, commands.MissingPermissions):
        await ctx.respond("You don't have permission to use this command.")
    elif isinstance(error, commands.NotOwner):
        await ctx.respond("This command can only be used by the bot owner.")
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        await ctx.respond("An error occurred while running the command. Please try again later.")
        raise error

##########################################################

dir_path = Path(__file__).parent.absolute()
channels_file = dir_path / "channels.json"

def load_channels():
    with open(channels_file, 'r') as f:
        return json.load(f)

def save_channels(channels):
    with open(channels_file, 'w') as f:
        json.dump(channels, f, indent=4)

def create_config(guild_id):
    channels = load_channels()
    if guild_id not in channels:
        channels[guild_id] = {"join": None, "leave": None,
                              "join_message": "Welcome {user} to the server! You are the {member_count}th member.", "leave_message": "Goodbye {user}!"}
        save_channels(channels)

@bot.event
async def on_guild_join(guild):
    guild_id = str(guild.id)
    create_config(guild_id)

def validate_channels_data(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid channels data format. Expected a dictionary.")
    
    for guild_id, guild_data in data.items():
        if not isinstance(guild_data, dict):
            raise ValueError(f"Invalid data format for guild {guild_id}. Expected a dictionary.")
        
        if "join_message" not in guild_data:
            raise ValueError(f"Missing 'join_message' key for guild {guild_id}.")
        
        if "leave_message" not in guild_data:
            raise ValueError(f"Missing 'leave_message' key for guild {guild_id}.")
        
        if "join" not in guild_data:
            raise ValueError(f"Missing 'join' key for guild {guild_id}.")
        
        if "leave" not in guild_data:
            raise ValueError(f"Missing 'leave' key for guild {guild_id}.")
    
    return data

@bot.slash_command(name="setjoinchannel", description="Set join channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setjoinchannel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join"] = channel.id
    save_channels(channels)
    await ctx.send(f"Join channel set to {channel.mention}")

@bot.slash_command(name="setleavechannel", description="Set leave channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setleavechannel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["leave"] = channel.id
    save_channels(channels)
    await ctx.send(f"Leave channel set to {channel.mention}")

@bot.slash_command(name="setjoinmessage", description="Set join message for the bot.")
@commands.has_permissions(manage_messages=True)
async def setjoinmessage(ctx, message: str):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join_message"] = message
    save_channels(channels)
    await ctx.send(f"Join message set to '{message}'")

@bot.slash_command(name="setleavemessage", description="Set leave message for the bot.")
@commands.has_permissions(manage_messages=True)  # Replace with the appropriate permission(s)
async def setleavemessage(ctx, message: str):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["leave_message"] = message
    save_channels(channels)
    await ctx.send(f"Leave message set to '{message}'")


@bot.event
async def on_member_join(member):
    channels = load_channels()
    guild_id = str(member.guild.id)
    if guild_id in channels and channels[guild_id]["join"] is not None:
        welcome_channel = bot.get_channel(int(channels[guild_id]["join"]))
        join_message = channels[guild_id].get(
            "join_message", "Welcome {user} to the server! You are the {member_count} member to join.")
        join_message = join_message.replace("{user}", member.mention)
        member_count = sum(
            1 for member in member.guild.members if not member.bot)
        join_message = join_message.replace(
            "{member_count}", str(member_count))
        await welcome_channel.send(join_message)

@bot.event
async def on_member_remove(member):
    channels = load_channels()
    guild_id = str(member.guild.id)
    if guild_id in channels and channels[guild_id]["leave"] is not None:
        leave_channel = bot.get_channel(int(channels[guild_id]["leave"]))
        leave_message = channels[guild_id].get(
            "leave_message", "Goodbye {user}! We are now {count} members.")
        leave_message = leave_message.replace("{user}", member.mention).replace(
            "{count}", str(member.guild.member_count))
        await leave_channel.send(leave_message)

###########################################################
# Mute command

@bot.slash_command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, *, reason="No reason provided."):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")

    if not mute_role:

        mute_role = await ctx.guild.create_role(name="Muted")

        for channel in ctx.guild.channels:
            await channel.set_permissions(mute_role, send_messages=False)

    await member.add_roles(mute_role, reason=reason)
    await ctx.send(f"{member.mention} has been muted. Reason: {reason}")

@bot.slash_command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")

    if mute_role in member.roles:
        await member.remove_roles(mute_role)
        await ctx.send(f"{member.mention} has been unmuted.")
    else:
        await ctx.send(f"{member.mention} is not muted.")

###########################################################

@bot.slash_command()
async def hello(ctx):
    await ctx.send(f'Hello, I am a bot made by the one and only duziy!')
    return

@bot.slash_command(name="math", description="Performs basic math operations.")
async def math(ctx, *, expression: str):
    try:

        operations = re.findall(r'\d+\s*[+\-*/]\s*\d+', expression)

        result = 0
        for op in operations:
            num1, operator, num2 = re.findall(
                r'(\d+)\s*([+\-*/])\s*(\d+)', op)[0]
            num1, num2 = int(num1), int(num2)
            if operator == '+':
                result += num1 + num2
            elif operator == '-':
                result += num1 - num2
            elif operator == '*':
                result += num1 * num2
            elif operator == '/':
                result += num1 / num2

        await ctx.send(f"Result: {result}")
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")

@bot.slash_command()
async def ping(ctx):
    await ctx.respond(f'Pong! {round(bot.latency * 1000)}ms')

@bot.slash_command()
@commands.has_permissions(manage_messages=True)
async def announce(ctx, *, message=None):
    if message is None:
        raise commands.MissingRequiredArgument
    embed = discord.Embed(title="", description=message, color=0xFF0000)
    embed.set_footer(text=f"Announced by {ctx.author.name}")
    await ctx.send(embed=embed)

@bot.slash_command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    try:
        if amount < 1 or amount > 100:
            raise commands.BadArgument("Amount must be between 1 and 100.")

        deleted = await ctx.channel.purge(limit=amount)

        response_msg = await ctx.send(f'Cleared {len(deleted)} messages.')

        await asyncio.sleep(5)

        await response_msg.delete()
    except commands.BadArgument as e:
        await ctx.respond(str(e))
    except discord.Forbidden:
        await ctx.respond("I don't have permission to delete messages.")

@bot.slash_command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, user: discord.User):
    guild = ctx.guild
    try:
        await guild.kick(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been kicked")
    await ctx.send(embed=embed)

@bot.slash_command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, user: discord.User):
    guild = ctx.guild
    try:
        await guild.ban(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been banned")
    await ctx.send(embed=embed)

@bot.slash_command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user: discord.User):
    guild = ctx.guild
    try:
        await guild.unban(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been unbanned")
    await ctx.send(embed=embed)

@bot.slash_command()
async def dadjoke(ctx):
    url = "https://icanhazdadjoke.com/"
    headers = {"Accept": "application/json"}
    response = requests.get(url, headers=headers)
    joke = response.json()['joke']
    await ctx.send(joke)


@bot.slash_command()
async def cat(ctx):
    url = "https://api.thecatapi.com/v1/images/search"
    response = requests.get(url)
    data = response.json()[0]
    embed = discord.Embed(title="Here's a cat!")
    embed.set_image(url=data['url'])
    await ctx.send(embed=embed)

@bot.slash_command()
async def dog(ctx):
    url = "https://dog.ceo/api/breeds/image/random"
    response = requests.get(url)
    data = response.json()['message']
    embed = discord.Embed(title="Here's a dog!")
    embed.set_image(url=data)
    await ctx.send(embed=embed)

@bot.slash_command()
async def eightball(ctx, *, question):
    responses = [
        "It is certain.",
        "Without a doubt.",
        "Yes - definitely.",
        "You may rely on it.",
        "As I see it, yes.",
        "Most likely.",
        "Yes.",
        "Signs point to yes.",
        "Ask again later.",
        "Better not tell you now.",
        "Cannot predict now.",
        "Concentrate and ask again.",
        "Don't count on it.",
        "Outlook not so good.",
        "My sources say no.",
        "Very doubtful."
    ]
    response = random.choice(responses)
    await ctx.send(f"🎱 Question: {question}\n🎱 Answer: {response}")

@bot.slash_command()
@commands.has_permissions(administrator=True)
async def lockdown(ctx, channel: discord.TextChannel):
    role = ctx.guild.default_role
    await channel.set_permissions(role, send_messages=False)
    await ctx.send(f"{channel.mention} has been locked down")

@bot.slash_command()
@commands.has_permissions(administrator=True)
async def unlock(ctx, channel: discord.TextChannel):
    role = ctx.guild.default_role
    await channel.set_permissions(role, send_messages=True)
    await ctx.send(f"{channel.mention} has been unlocked")

@bot.slash_command()
async def roll(ctx, sides: int):
    result = random.randint(1, sides)
    await ctx.send(f"Rolling a {sides}-sided dice... You rolled a {result}!")

@bot.slash_command()
async def howgay(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author
    gay_percent = random.randint(0, 100)
    await ctx.send(f"{member.mention} is {gay_percent}% gay 🏳️‍🌈")

@bot.slash_command(name="meme", description="Get a random meme from Imgur!")
async def meme(ctx):
    async with aiohttp.ClientSession() as session:
        async with session.get("https://www.reddit.com/r/memes/random/.json") as response:
            if response.status == 200:
                meme_data = await response.json()
                meme_url = meme_data[0]["data"]["children"][0]["data"]["url"]
                
                embed = discord.Embed(title="Random Meme")
                embed.set_image(url=meme_url)
                
                await ctx.send(embed=embed)
            else:
                await ctx.send("Failed to fetch a meme. Please try again later.")

@bot.slash_command()
async def advice(ctx):
    url = 'https://api.adviceslip.com/advice'
    response = requests.get(url)
    if response.status_code == 200:
        advice = response.json()['slip']['advice']
        await ctx.send(advice)
    else:
        await ctx.send('Oops, something went wrong. Please try again later.')

###########################################################
# Help Command

@bot.slash_command()
async def help(ctx):
    contents = [
        '''
        **Utilities:**

        `/ping`: Displays the bot's latency.
        `/announce [message]`: Announces a message in the current channel (requires Manage Messages permission).
        `/setjoinchannel [channel]`: Set the channel where join messages will be posted. Only users with admin permissions can use this command.
        `/setleavechannel [channel]`: Set the channel where leave messages will be posted. Only users with admin permissions can use this command.
        `/setjoinmessage [message]`: Set the message that will be posted when a user joins the server. Only users with admin permissions can use this command. You can include the user's mention by including `{user}` and `{number}` for what number of user that they are in the message.
        `/setleavemessage [message]`: Set the message that will be posted when a user leaves the server. Only users with admin permissions can use this command. You can include the user's mention by including `{user}` and `{number}` for what number of user that they are in the message.
        ''',

        '''
        **Moderation:**

        `/kick [user]`: Kicks the specified user from the server (requires Kick Members permission).
        `/ban [user]`: Bans the specified user from the server (requires Ban Members permission).
        `/unban [user]`: Unbans the specified user from the server (requires Ban Members permission).
        `/lockdown [channel]`: Locks down the specified channel (requires Administrator permission).
        `/unlock [channel]`: Unlocks the specified channel (requires Administrator permission).
        `/clear [amount]`: Clears the specified amount of messages in the current channel (requires Manage Messages permission).
        `/mute [user]` : Makes it to that the user can not talk in any channels (requires Timeout permission).
        `/unmute [user]` : Makes it to that the user able to talk in any channels again (requires Timeout permission). 
        ''',

        '''
        **Fun:**

        `/hello`: Greets the user.
        `/meme`: Fetches a random meme.
        `/dadjoke`: Fetches a random dad joke.
        `/cat`: Fetches a random picture of a cat.
        `/dog`: Fetches a random picture of a dog.
        `/eightball [question]`: Responds with a random 8ball response to the given question.
        `/roll [number of sides]`: Rolls a dice with the specified number of sides.
        `/math [your math question]`: Does simple math for you.
        `/advice`: Gives you some random advice.
        ''',
    ]

    pages = len(contents)
    cur_page = 0
    embed = discord.Embed(description=contents[cur_page], color=discord.Color.blurple())
    embed.set_footer(text=f"Page {cur_page+1}/{pages}")

    message = await ctx.send(embed=embed)
    await message.add_reaction("◀️")
    await message.add_reaction("▶️")
    await message.add_reaction("❌")

    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in ["◀️", "▶️", "❌"]

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=30, check=check)

            if str(reaction.emoji) == "▶️" and cur_page < pages - 1:
                cur_page += 1
                embed.description = contents[cur_page]
                embed.set_footer(text=f"Page {cur_page+1}/{pages}")
                await message.edit(embed=embed)
                await message.remove_reaction(reaction, user)

            elif str(reaction.emoji) == "◀️" and cur_page > 0:
                cur_page -= 1
                embed.description = contents[cur_page]
                embed.set_footer(text=f"Page {cur_page+1}/{pages}")
                await message.edit(embed=embed)
                await message.remove_reaction(reaction, user)

            elif str(reaction.emoji) == "❌":
                await message.delete()
                break

            else:
                await message.remove_reaction(reaction, user)

        except asyncio.TimeoutError:
            await message.delete()
            break

bot.run(token)
