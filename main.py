import discord
import os.path
import json
import os
import random
import asyncio
import aiohttp
import re
from discord.ext import commands
from pathlib import Path
from dotenv import load_dotenv
import logging
load_dotenv()
token = os.getenv("TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='.', intents=intents)

@bot.event
async def on_ready():

    await bot.change_presence(status=discord.Status.online, activity=discord.Game("Need help do /help"))

print("Bot is ready!")

##########################################################

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s.", delete_after=10)
    elif isinstance(error, commands.NoPrivateMessage):
        pass
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("This command can only be used by the bot owner.")
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        message = "An error occurred while running the command. Please try again later."
        print(f"An error occurred: {error}")
        try:
            if isinstance(ctx, discord.InteractionContext):
                await ctx.response.send_message(message)
            else:
                await ctx.send(message)
        except Exception as e:
            print(f"An error occurred while handling command error: {e}")
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

async def fetch(url)
    async with aiohttp.ClientSession() as session:
        headers = {"Accept": "application/json"}
        async with session.get(url, headers=headers) as response:
            return response.json()

def create_config(guild_id):
    channels = load_channels()
    if guild_id not in channels:
        channels[guild_id] = {"join": None, "leave": None, "join_message": "Welcome {user} to the server! You are the {member_count}th member.", "leave_message": "{user} has left the server", "logs": None}
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

##############################################

@bot.event
async def on_message_edit(before, after):
    logging.debug(f"Message edited: {before.content} -> {after.content}")

    guild_id = str(before.guild.id) if before.guild else None
    if guild_id is None:
        return
    channels = load_channels()
    log_channel_id = channels[guild_id]["logs"]
    if log_channel_id is None:
        return
    log_channel = bot.get_channel(log_channel_id)
    if log_channel is None:
        return

    # Create the log embed
    embed = discord.Embed(color=0x00ff00)

    # Check if the edited message contains an embed
    before_embeds = before.embeds
    after_embeds = after.embeds
    if before_embeds and after_embeds:
        # Compare the embed fields to see what changed
        before_embed = before_embeds[0]
        after_embed = after_embeds[0]
        changes = []
        for field in before_embed.fields:
            matching_field = next((f for f in after_embed.fields if f.name == field.name), None)
            if matching_field and matching_field.value != field.value:
                changes.append(f"{field.name}: {field.value} -> {matching_field.value}")
        if changes:
            embed.title = "Embed Message Edited"
            embed.add_field(name="Changes", value="\n".join(changes), inline=False)
            embed.set_footer(text=f"Edited by {after.author.display_name}#{after.author.discriminator}")
            embed.set_footer(text=f"User ID: {before.author.id} | Message ID: {before.id}")
    elif before.content != after.content:
        embed.title = "Message Edited"
        embed.add_field(name="Before", value=before.content, inline=False)
        embed.add_field(name="After", value=after.content, inline=False)
        embed.set_footer(text=f"Edited by {after.author.display_name}#{after.author.discriminator}")
        embed.set_footer(text=f"User ID: {before.author.id} | Message ID: {before.id}")

    # Get the user who edited the message
    edited_by = await bot.fetch_user(after.author.id)

    # Mention the user who edited the message
    embed.description = f"{edited_by.mention} edited a message.\n{embed.description}"

    # Add the user's ID to the log embed
    embed.set_footer(text=f"User ID: {edited_by.id} | Message ID: {after.id}")

    # Send the log embed to the log channel
    await log_channel.send(embed=embed)

@bot.event
async def on_message_delete(message):
    logging.debug(f"Message deleted: {message.content}")

    guild_id = str(message.guild.id) if message.guild else None
    if guild_id is None:
        return
    channels = load_channels()
    log_channel_id = channels[guild_id]["logs"]
    if log_channel_id is None:
        return
    log_channel = bot.get_channel(log_channel_id)
    if log_channel is None:
        return

    # Check if the deleted message contains an embed
    embed = None
    if message.embeds:
        embed = message.embeds[0]

    # Create the log embed
    log_embed = discord.Embed(title="Message Deleted", color=0xFF0000)
    log_embed.set_footer(text=f"Message ID: {message.id}")

    # Add information about the deleted message to the log embed
    if embed:
        log_embed.title = "Embed Message Deleted"
        log_embed.add_field(name="Embed Title", value=embed.title, inline=False)
        log_embed.add_field(name="Embed URL", value=embed.url, inline=False)
        log_embed.add_field(name="Embed Description", value=embed.description, inline=False)
        for field in embed.fields:
            log_embed.add_field(name=field.name, value=field.value, inline=False)
    else:
        log_embed.add_field(name="Content", value=message.content or "No content", inline=False)

    # Get the user who deleted the message
    deleted_by = await bot.fetch_user(message.author.id)

    # Mention the user who deleted the message
    log_embed.description = f"{deleted_by.mention} deleted a message.\n{log_embed.description}"

    # Add the user's ID to the log embed
    log_embed.set_footer(text=f"User ID: {deleted_by.id} | Message ID: {message.id}")

    # Send the log embed to the log channel
    await log_channel.send(embed=log_embed)

@bot.event
async def on_guild_channel_create(channel):
    logging.debug(f"Channel created: {channel.name}")

    guild_id = str(channel.guild.id)
    channels = load_channels()
    log_channel_id = channels[guild_id]["logs"]
    if log_channel_id is None:
        return
    log_channel = bot.get_channel(log_channel_id)
    if log_channel is None:
        return

    # Get the user who created the channel
    entry = await channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create).flatten()
    created_by = entry[0].user

    # Create the log embed
    log_embed = discord.Embed(title="Channel Created", color=0x00ff00)
    log_embed.add_field(name="Channel", value=channel.mention, inline=False)
    log_embed.set_footer(text=f"Channel ID: {channel.id}")
    log_embed.set_author(name=f"{created_by.display_name}#{created_by.discriminator}", icon_url=created_by.avatar_url)
    log_embed.description = f"{created_by.mention} created a new channel.\nUser ID: {created_by.id}"

    # Send the log embed to the log channel
    await log_channel.send(embed=log_embed)

@bot.event
async def on_guild_channel_delete(channel):
    logging.debug(f"Channel deleted: {channel.name}")

    guild_id = str(channel.guild.id)
    channels = load_channels()
    log_channel_id = channels[guild_id]["logs"]
    if log_channel_id is None:
        return
    log_channel = bot.get_channel(log_channel_id)
    if log_channel is None:
        return

    # Get the user who deleted the channel
    entry = await channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete).flatten()
    deleted_by = entry[0].user

    # Create the log embed
    log_embed = discord.Embed(title="Channel Deleted", color=0xFF0000)
    log_embed.add_field(name="Channel", value=channel.name, inline=False)
    log_embed.set_footer(text=f"Channel ID: {channel.id}")
    log_embed.set_author(name=f"{deleted_by.display_name}#{deleted_by.discriminator}", icon_url=deleted_by.avatar_url)
    log_embed.description = f"{deleted_by.mention} deleted a channel.\nUser ID: {deleted_by.id}"

    # Send the log embed to the log channel
    await log_channel.send(embed=log_embed)

@bot.event
async def on_guild_channel_update(before, after):
    logging.debug(f"Channel updated: {before.name} -> {after.name}")

    guild_id = str(before.guild.id)
    channels = load_channels()
    log_channel_id = channels[guild_id]["logs"]
    if log_channel_id is None:
        return
    log_channel = bot.get_channel(log_channel_id)
    if log_channel is None:
        return

    # Get the user who updated the channel
    entry = await before.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_update).flatten()
    updated_by = entry[0].user

    # Create the log embed
    log_embed = discord.Embed(title="Channel Updated", color=0x00ff00)
    log_embed.add_field(name="Before", value=before.name, inline=False)
    log_embed.add_field(name="After", value=after.name, inline=False)
    log_embed.set_footer(text=f"Channel ID: {before.id}")
    log_embed.set_author(name=f"{updated_by.display_name}#{updated_by.discriminator}", icon_url=updated_by.avatar_url)
    log_embed.description = f"{updated_by.mention} updated a channel.\nUser ID: {updated_by.id}"

    # Send the log embed to the log channel
    await log_channel.send(embed=log_embed)

@bot.hybrid_command(name="setlogchannel", description="Set log channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setlogchannel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["logs"] = channel.id
    save_channels(channels)
    await ctx.send(f"Log channel set to {channel.mention}")

##############################################

@bot.hybrid_command(name="setjoinchannel", description="Set join channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setjoinchannel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join"] = channel.id
    save_channels(channels)
    await ctx.send(f"Join channel set to {channel.mention}")

@bot.hybrid_command(name="setleavechannel", description="Set leave channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setleavechannel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["leave"] = channel.id
    save_channels(channels)
    await ctx.send(f"Leave channel set to {channel.mention}")

@bot.hybrid_command(name="setjoinmessage", description="Set join message for the bot.")
@commands.has_permissions(manage_messages=True)
async def setjoinmessage(ctx, message: str):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join_message"] = message
    save_channels(channels)
    await ctx.send(f"Join message set to '{message}'")

@bot.hybrid_command(name="setleavemessage", description="Set leave message for the bot.")
@commands.has_permissions(manage_messages=True)
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

@bot.hybrid_command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, *, reason="No reason provided."):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")

    if not mute_role:

        mute_role = await ctx.guild.create_role(name="Muted")

        for channel in ctx.guild.channels:
            await channel.set_permissions(mute_role, send_messages=False)

    await member.add_roles(mute_role, reason=reason)
    await ctx.send(f"{member.mention} has been muted. Reason: {reason}")

@bot.hybrid_command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")

    if mute_role in member.roles:
        await member.remove_roles(mute_role)
        await ctx.send(f"{member.mention} has been unmuted.")
    else:
        await ctx.send(f"{member.mention} is not muted.")

###########################################################

@bot.hybrid_command()
async def hello(ctx):
    await ctx.send(f'Hello, I am a bot made by the one and only duziy!', ephemeral=True)


@bot.hybrid_command(name="math", description="Performs basic math operations.")
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

@bot.hybrid_command()
async def ping(ctx):
    await ctx.send(f'Pong! {round(bot.latency * 1000)}ms', ephemeral=True)

@bot.hybrid_command()
@commands.has_permissions(manage_messages=True)
async def announce(ctx, *, message=None):
    if message is None:
        raise commands.MissingRequiredArgument
    embed = discord.Embed(title="", description=message, color=0xFF0000)
    embed.set_footer(text=f"Announced by {ctx.author.name}")
    await ctx.send(embed=embed)

import asyncio

import asyncio

interaction_response = None

@bot.hybrid_command(name="clear", description="Clears the specified amount of messages.")
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    global interaction_response

    if amount < 1 or amount > 100:
        await ctx.send("Amount must be between 1 and 100.")
        return

    # Acknowledge the command immediately
    if ctx.interaction is not None:
        interaction_response = await ctx.interaction.response.defer()

    # Perform the long-running task in the background
    asyncio.create_task(clear_messages(ctx, amount))

async def clear_messages(ctx, amount):
    await asyncio.sleep(1)

    deleted = await ctx.channel.purge(limit=amount)

@bot.hybrid_command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, user: discord.User):
    guild = ctx.guild
    try:
        await guild.kick(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been kicked")
    await ctx.send(embed=embed)

@bot.hybrid_command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, user: discord.User):
    guild = ctx.guild
    try:
        await guild.ban(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been banned")
    await ctx.send(embed=embed)

@bot.hybrid_command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user: discord.User):
    guild = ctx.guild
    try:
        await guild.unban(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been unbanned")
    await ctx.send(embed=embed)

@bot.hybrid_command()
async def dadjoke(ctx):
    joke = await fetch("https://icanhazdadjoke.com/")

    await ctx.send(joke['joke'])

@bot.hybrid_command()
async def cat(ctx):
    request = await fetch("https://api.thecatapi.com/v1/images/search")

    embed = discord.Embed(title="Here's a cat!")
    embed.set_image(url=data[0]['url'])
    await ctx.send(embed=embed)

@bot.hybrid_command()
async def dog(ctx):
    req = await fetch("https://dog.ceo/api/breeds/image/random")
    image = req['message']

    embed = discord.Embed(title="Here's a dog!")
    embed.set_image(url=image)
    await ctx.send(embed=embed)

@bot.hybrid_command()
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

@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def lockdown(ctx, channel: discord.TextChannel):
    role = ctx.guild.default_role
    permissions = channel.overwrites_for(role)
    permissions.send_messages = False
    await channel.set_permissions(role, overwrite=permissions)
    await ctx.send(f"{channel.mention} has been locked down")

@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def unlock(ctx, channel: discord.TextChannel):
    role = ctx.guild.default_role
    permissions = channel.overwrites_for(role)
    permissions.send_messages = True
    await channel.set_permissions(role, overwrite=permissions)
    await ctx.send(f"{channel.mention} has been unlocked")

@bot.hybrid_command()
async def roll(ctx, sides: int):
    result = random.randint(1, sides)
    await ctx.send(f"Rolling a {sides}-sided dice... You rolled a {result}!")

@bot.hybrid_command()
async def howgay(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author
    gay_percent = random.randint(0, 100)
    await ctx.send(f"{member.mention} is {gay_percent}% gay 🏳️‍🌈")

@bot.hybrid_command(name="meme", description="Get a random meme from Imgur!")
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

@bot.hybrid_command()
async def advice(ctx):
    req = await fetch('https://api.adviceslip.com/advice')
    advice = req['slip']['advice']
    await ctx.send(advice)

@bot.hybrid_command()
async def avatar(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author
    
    embed = discord.Embed(title=f"{member.name}'s Avatar")
    embed.set_image(url=member.avatar.url)
    
    await ctx.send(embed=embed)

###########################################################
# Help Command

existing_help_command = bot.get_command("help")

if existing_help_command:
    bot.remove_command("help")
    @bot.hybrid_command()
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
            `/avatar [user]`: Displays the specified user's avatar.
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
                reaction, user = await bot.wait_for("reaction_add", timeout=40, check=check)

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
