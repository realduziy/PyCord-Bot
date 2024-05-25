import discord
import os.path
import json
import os
import random
import asyncio
import aiohttp
import re
import time
import platform
from discord.ext import commands
from pathlib import Path
from dotenv import load_dotenv
from colorama import Fore, Back, Style
import logging
from typing import Optional
load_dotenv()
from discord.ui import Button, View
token = os.getenv("TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='.', intents=intents)

##########################################################
@bot.event
async def on_ready():
    prfx = (Back.BLACK + Fore.GREEN + time.strftime("%H:%M:%S UTC", time.gmtime()) + Back.RESET + Fore.WHITE + Style.BRIGHT)
    print(prfx + " Logged in as " + Fore.YELLOW + bot.user.name)
    print(prfx + " Bot ID " + Fore.YELLOW + str(bot.user.id))
    print(prfx + " Discord Version " + Fore.YELLOW + discord.__version__)
    print(prfx + " Python Version " + Fore.YELLOW + str(platform.python_version()))
    synced = await bot.tree.sync()
    print(prfx + " Slash CMDs Synced " + Fore.YELLOW + str(len(synced)) + " Commands")
    await bot.change_presence(status=discord.Status.online, activity=discord.Game("Need help do /help"))

##########################################################

@bot.event
async def on_command_error(interaction, error):
    if isinstance(error, commands.CommandNotFound):
        # Ignore CommandNotFound errors
        return
    elif isinstance(error, commands.CommandOnCooldown):
        await interaction.response.send_message(f"You are on cooldown. Try again in {error.retry_after:.2f}s.", delete_after=10)
    elif isinstance(error, commands.NoPrivateMessage):
        pass
    elif isinstance(error, commands.MissingPermissions):
        await interaction.response.send_message("You don't have permission to use this command.")
    elif isinstance(error, commands.NotOwner):
        await interaction.response.send_message("This command can only be used by the bot owner.")
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        message = "An error occurred while running the command. Please try again later."
        print(f"An error occurred: {error}")
        try:
            if isinstance(interaction, discord.InteractionContext):
                await interaction.response.send_message(message)
            else:
                await interaction.response.send_message(message)
        except Exception as e:
            print(f"An error occurred while handling command error: {e}")
        raise error

##########################################################

# Define an asynchronous fetch function
async def fetch(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={'Accept': 'application/json'}) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return None
    except aiohttp.ContentTypeError:
        return None
    except Exception as e:
        print(f"An error occurred while fetching data: {e}")
        return None

dir_path = Path(__file__).parent.absolute()
channels_file = dir_path / "channels.json"
open_tickets_file = dir_path / "open_tickets.json"

def load_channels():
    with open(channels_file, 'r') as f:
        return json.load(f)

def save_channels(channels):
    with open(channels_file, 'w') as f:
        json.dump(channels, f, indent=4)

def create_config(guild_id):
    channels = load_channels()
    if guild_id not in channels:
        channels[guild_id] = {
            "join": None,
            "leave": None,
            "join_message": "Welcome {user} to the server! You are the {member_count}th member.",
            "leave_message": "{user} has left the server",
            "logs": None,
            "ticket_panel": None,
            "transcripts": None,
            "ticket_support_role": None,
            "ticket_category": None
        }
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

#ticket system

def load_open_tickets():
    if open_tickets_file.exists():
        with open(open_tickets_file, 'r') as f:
            return json.load(f)
    return {}

def save_open_tickets(open_tickets):
    with open(open_tickets_file, 'w') as f:
        json.dump(open_tickets, f, indent=4)

@bot.tree.command(name="setticketpanel", description="Set the ticket panel channel.")
@commands.has_permissions(manage_channels=True)
async def setticketpanel(interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["ticket_panel"] = channel.id
    save_channels(channels)
    await interaction.response.send_message(f'Ticket panel channel set to {channel.mention}.')

@bot.tree.command(name="settranscriptschannel", description="Set the transcripts channel.")
@commands.has_permissions(manage_channels=True)
async def settranscriptschannel(interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["transcripts"] = channel.id
    save_channels(channels)
    await interaction.response.send_message(f'Transcripts channel set to {channel.mention}.')

@bot.tree.command(name="setticketcategory", description="Set the category where tickets will be created.")
@commands.has_permissions(manage_channels=True)
async def setticketcategory(interaction, category: discord.CategoryChannel):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["ticket_category"] = category.id
    save_channels(channels)
    await interaction.response.send_message(f'Ticket category set to {category.mention}.')

@bot.tree.command(name="setticketsupportrole", description="Sets the role that will be given to support tickets.")
@commands.has_permissions(manage_roles=True)
async def setticketsupportrole(interaction, role: discord.Role):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["ticket_support_role"] = role.id
    save_channels(channels)
    await interaction.response.send_message(f'Ticket support role set to {role.mention}.')

@bot.tree.command(name="sendticketpanel", description="Sends a message in the ticket panel channel.")
async def sendticketpanel(interaction):
    guild_id = str(interaction.guild.id)
    channels = load_channels()
    panel_channel_id = channels[guild_id]["ticket_panel"]
    panel_channel = interaction.guild.get_channel(panel_channel_id)
    if not panel_channel:
        await interaction.response.send_message('Ticket panel channel not found.')
        return

    embed = discord.Embed(title='Support Tickets', description='Click the button below to create a support ticket.', color=0x00ff00)
    embed.set_footer(text='Ticket System')
    message = await panel_channel.send(embed=embed)

    view = View()
    view.add_item(Button(label='Create Ticket', style=discord.ButtonStyle.green, custom_id='create_ticket'))
    await message.edit(view=view)

@bot.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.component:
        return

    open_tickets = load_open_tickets()
    guild_id = str(interaction.guild_id)

    if interaction.data["custom_id"] == "create_ticket":
        channels = load_channels()
        category_id = channels[guild_id]["ticket_category"]
        category = discord.utils.get(interaction.guild.categories, id=category_id)
        if not category:
            await interaction.response.send_message('Ticket category not found.', ephemeral=True)
            return

        support_role_id = channels[guild_id]["ticket_support_role"]
        support_role = interaction.guild.get_role(support_role_id)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True),
            support_role: discord.PermissionOverwrite(view_channel=True)
        }

        channel = await interaction.guild.create_text_channel(f'ticket-{interaction.user.id}', category=category, overwrites=overwrites)

        embed = discord.Embed(title='Ticket Created', description=f'{interaction.user.mention}, your ticket has been created. Please wait for a staff member to assist you.', color=0x00ff00)
        message = await channel.send(embed=embed)

        await message.add_reaction('📝')

        view = View()
        close_button = Button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
        view.add_item(close_button)
        await message.edit(view=view)

        open_tickets[str(channel.id)] = {
            'user_id': str(interaction.user.id),
            'user_name': str(interaction.user),
            'created_at': str(channel.created_at)
        }
        save_open_tickets(open_tickets)

        panel_channel_id = channels[guild_id]["ticket_panel"]
        panel_channel = interaction.guild.get_channel(panel_channel_id)
        if not panel_channel:
            await interaction.response.send_message('Ticket panel channel not found.', ephemeral=True)
            return

        await interaction.response.send_message(f'Your ticket has been created: {channel.mention}', ephemeral=True)

    elif interaction.data["custom_id"] == "close_ticket":
        channel = interaction.guild.get_channel(int(interaction.channel_id))
        if not channel:
            await interaction.response.send_message("Ticket channel not found.", ephemeral=True)
            return

        if str(channel.id) not in open_tickets:
            await interaction.response.send_message("This ticket is not open.", ephemeral=True)
            return

        guild = interaction.guild
        channels = load_channels()
        transcripts_channel_id = channels[guild_id]["transcripts"]
        transcripts_channel = guild.get_channel(transcripts_channel_id)
        if not transcripts_channel:
            await interaction.response.send_message("Transcripts channel not found.", ephemeral=True)
            return

        # Initialize an empty list to store message content
        message_contents = []

        # Get ticket creation and opening information
        ticket_info = open_tickets[str(channel.id)]
        ticket_created_at = ticket_info['created_at']
        ticket_user_id = ticket_info['user_id']
        ticket_user_name = ticket_info['user_name']

        # Append ticket creation and opening information to the message content
        ticket_info_text = f"**Ticket Created on:** {ticket_created_at}\n**Ticket Opened by:** {ticket_user_name} ({ticket_user_id})"
        message_contents.append(ticket_info_text)

        # Check if there are any messages in the channel
        has_messages = False
        async for message in channel.history(limit=1):
            has_messages = True
            break

        if not has_messages:
            # If no messages were sent in the ticket, add a message indicating that
            message_contents.append("No messages were sent in this ticket.")
        else:
            # Iterate through the channel history to collect message content and sender information
            async for message in channel.history(limit=None):
                if message.author == bot.user:
                    continue
                message_content = f"**Content:** {message.content}\n" if message.content else ""
                author_info = f"**From:** {message.author}\n"
                embed_description = f"{message_content}{author_info}"
                message_contents.append(embed_description)

        # Concatenate all message contents into a single embedded message without extra newlines
        full_embed_description = "\n".join(message_contents)
        embed = discord.Embed(title="Transcript", description=full_embed_description, color=0x00ff00)

        # Send the transcript message
        await transcripts_channel.send(embed=embed)

        await channel.delete()

        del open_tickets[str(channel.id)]
        save_open_tickets(open_tickets)

        await interaction.response.send_message("Ticket channel has been closed.", ephemeral=True)

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
    entry = None
    async for log_entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
        entry = log_entry
        break
    
    if entry is None:
        return

    created_by = entry.user

    # Create the log embed
    log_embed = discord.Embed(title="Channel Created", color=0x00ff00)
    log_embed.add_field(name="Channel", value=channel.mention, inline=False)
    log_embed.set_footer(text=f"Channel ID: {channel.id}")
    log_embed.set_author(name=f"{created_by.display_name}#{created_by.discriminator}")
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
    entry = None
    async for log_entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        entry = log_entry
        break
    
    if entry is None:
        return

    deleted_by = entry.user

    # Create the log embed
    log_embed = discord.Embed(title="Channel Deleted", color=0xFF0000)
    log_embed.add_field(name="Channel", value=channel.name, inline=False)
    log_embed.set_footer(text=f"Channel ID: {channel.id}")
    log_embed.set_author(name=f"{deleted_by.display_name}#{deleted_by.discriminator}")
    log_embed.description = f"{deleted_by.mention} deleted a channel.\nUser ID: {deleted_by.id}"

    # Send the log embed to the log channel
    await log_channel.send(embed=log_embed)

@bot.event
async def on_guild_channel_update(before, after):
    logging.debug(f"Channel updated: {before.name} -> {after.name}")

    if before.name == after.name and before.topic == after.topic and before.overwrites == after.overwrites:
        # If only the position has changed, ignore the update
        if before.position != after.position:
            return

    guild_id = str(before.guild.id)
    channels = load_channels()
    log_channel_id = channels[guild_id].get("logs")
    if log_channel_id is None:
        return

    log_channel = bot.get_channel(log_channel_id)
    if log_channel is None:
        return

    # Get the user who updated the channel
    entry = None
    async for log_entry in before.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_update):
        entry = log_entry
        break
    
    if entry is None:
        return

    updated_by = entry.user

    # Create the log embed
    log_embed = discord.Embed(title="Channel Updated", color=0x00ff00)
    log_embed.add_field(name="Before", value=before.name, inline=False)
    log_embed.add_field(name="After", value=after.name, inline=False)
    log_embed.set_footer(text=f"Channel ID: {before.id}")
    log_embed.set_author(name=f"{updated_by.display_name}#{updated_by.discriminator}", icon_url=updated_by.avatar.url)
    log_embed.description = f"{updated_by.mention} updated a channel.\nUser ID: {updated_by.id}"

    # Send the log embed to the log channel
    await log_channel.send(embed=log_embed)

@bot.tree.command(name="setlogchannel", description="Set log channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setlogchannel(interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["logs"] = channel.id
    save_channels(channels)
    await interaction.response.send_message(f"Log channel set to {channel.mention}")

##############################################

@bot.tree.command(name="setjoinchannel", description="Set join channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setjoinchannel(interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join"] = channel.id
    save_channels(channels)
    await interaction.response.send_message(f"Join channel set to {channel.mention}")

@bot.tree.command(name="setleavechannel", description="Set leave channel for the bot.")
@commands.has_permissions(manage_messages=True)
async def setleavechannel(interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["leave"] = channel.id
    save_channels(channels)
    await interaction.response.send_message(f"Leave channel set to {channel.mention}")

@bot.tree.command(name="setjoinmessage", description="Set join message for the bot.")
@commands.has_permissions(manage_messages=True)
async def setjoinmessage(interaction, message: str):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join_message"] = message
    save_channels(channels)
    await interaction.response.send_message(f"Join message set to '{message}'")

@bot.tree.command(name="setleavemessage", description="Set leave message for the bot.")
@commands.has_permissions(manage_messages=True)
async def setleavemessage(interaction, message: str):
    guild_id = str(interaction.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["leave_message"] = message
    save_channels(channels)
    await interaction.response.send_message(f"Leave message set to '{message}'")

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

@bot.tree.command(name="mute", description="Mute a user.")
@commands.has_permissions(moderate_members=True)
async def mute(interaction, member: discord.Member, reason: str = "No reason provided."):
    mute_role = discord.utils.get(interaction.guild.roles, name="Muted")

    if not mute_role:
        mute_role = await interaction.guild.create_role(name="Muted")
        for channel in interaction.guild.channels:
            await channel.set_permissions(mute_role, send_messages=False)

    await member.add_roles(mute_role, reason=reason)
    await interaction.response.send_message(f"{member.mention} has been muted. Reason: {reason}")

@bot.tree.command(name="unmute", description="Unmute a user.")
@commands.has_permissions(moderate_members=True)
async def unmute(interaction, member: discord.Member):
    mute_role = discord.utils.get(interaction.guild.roles, name="Muted")

    if mute_role in member.roles:
        await member.remove_roles(mute_role)
        await interaction.response.send_message(f"{member.mention} has been unmuted.")
    else:
        await interaction.response.send_message(f"{member.mention} is not muted.")

###########################################################

@bot.tree.command(name="hello", description="Say hello to the bot.")
async def hello(interaction):
    await interaction.response.send_message(f'Hello, I am a bot made by the one and only duziy!', ephemeral=True)


@bot.tree.command(name="math", description="Performs basic math operations.")
async def math(interaction, *, expression: str):
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

        await interaction.response.send_message(f"Result: {result}")
    except Exception as e:
        await interaction.response.send_message(f"Error: {str(e)}")

@bot.tree.command(name="ping", description="Pings the bot.")
async def ping(interaction):
    await interaction.response.send_message(f'Pong! {round(bot.latency * 1000)}ms', ephemeral=True)

@bot.tree.command(name="announce", description="Announces a message.")
@commands.has_permissions(manage_messages=True)
async def announce(interaction: discord.Interaction, *, message: Optional[str] = None):
    if message is None:
        raise commands.MissingRequiredArgument

    embed = discord.Embed(title="", description=message, color=0xFF0000)
    embed.set_footer(text=f"Announced by {interaction.user.name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clear", description="Clears the specified amount of messages.")
@commands.has_permissions(manage_messages=True)
async def clear(interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("Amount must be between 1 and 100.")
        return

    # Acknowledge the command immediately
    await interaction.response.defer()

    # Perform the long-running task in the background
    await asyncio.sleep(1)

    deleted = await interaction.channel.purge(limit=amount)

    try:
        await interaction.followup.send("Deleted {} messages.".format(len(deleted)))
    except discord.NotFound:
        pass  # Ignore NotFound error if the original message is not found

@bot.tree.command(name="kick", description="Kicks a user.")
@commands.has_permissions(kick_members=True)
async def kick(interaction, user: discord.User):
    guild = interaction.guild
    try:
        await guild.kick(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been kicked")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ban", description="Bans a user.")
@commands.has_permissions(ban_members=True)
async def ban(interaction, user: discord.User):
    guild = interaction.guild
    try:
        await guild.ban(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been banned")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unban", description="Unbans a user.")
@commands.has_permissions(ban_members=True)
async def unban(interaction, user: discord.User):
    guild = interaction.guild
    try:
        await guild.unban(user=user)
    except discord.errors.NotFound:
        pass
    embed = discord.Embed(description=f"{user} has been unbanned")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="dadjoke", description="Sends a random dad joke.")
async def dadjoke(interaction):
    request = await fetch("https://icanhazdadjoke.com/")
    if request and 'joke' in request:
        joke = request['joke']
        await interaction.response.send_message(joke)

@bot.tree.command(name="advice", description="Get an advice from Advice Slip.")
async def advice(interaction):
    url = 'https://api.chucknorris.io/jokes/random'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                joke = data['value']
                await interaction.response.send_message(joke)
            else:
                await interaction.response.send_message("Sorry, couldn't fetch advice. The response is not in the expected format.")

@bot.tree.command(name="cat", description="Sends a random cat image.")
async def cat(interaction):
    request = await fetch("https://api.thecatapi.com/v1/images/search")
    if request and isinstance(request, list) and len(request) > 0:
        image_url = request[0].get('url')
        if image_url:
            embed = discord.Embed(title="Here's a cat!")
            embed.set_image(url=image_url)
            await interaction.response.send_message(embed=embed)

@bot.tree.command(name="dog", description="Sends a random dog image.")
async def dog(interaction):
    req = await fetch("https://dog.ceo/api/breeds/image/random")
    image = req['message']
    embed = discord.Embed(title="Here's a dog!")
    embed.set_image(url=image)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="8ball", description="Asks the magic 8ball a question.")
async def eightball(interaction, *, question: str):
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
    await interaction.response.send_message(f"🎱 Question: {question}\n🎱 Answer: {response}")

@bot.tree.command(name="lockdown")
@commands.has_permissions(administrator=True)
async def lockdown(interaction, channel: discord.TextChannel):
    role = interaction.guild.default_role
    permissions = channel.overwrites_for(role)
    permissions.send_messages = False
    await channel.set_permissions(role, overwrite=permissions)
    await interaction.response.send_message(f"{channel.mention} has been locked down")

@bot.tree.command(name="unlock", description="Unlocks a channel.")
@commands.has_permissions(administrator=True)
async def unlock(interaction, channel: discord.TextChannel):
    role = interaction.guild.default_role
    permissions = channel.overwrites_for(role)
    permissions.send_messages = None
    await channel.set_permissions(role, overwrite=permissions)
    await interaction.response.send_message(f"{channel.mention} has been unlocked")

@bot.tree.command(name="roll", description="Rolls a dice with the specified number of sides.")
async def roll(interaction, sides: int):
    result = random.randint(1, sides)
    await interaction.response.send_message(f"Rolling a {sides}-sided dice... You rolled a {result}!")

@bot.tree.command(name="howgay", description="Get how gay you are.")
async def howgay(interaction, member: discord.Member = None):
    if member is None:
        member = interaction.author
    gay_percent = random.randint(0, 100)
    await interaction.response.send_message(f"{member.mention} is {gay_percent}% gay 🏳️‍🌈")

@bot.tree.command(name="meme", description="Get a random meme from Imgur!")
async def meme(interaction):
    async with aiohttp.ClientSession() as session:
        async with session.get("https://www.reddit.com/r/memes/random/.json") as response:
            if response.status == 200:
                meme_data = await response.json()
                meme_url = meme_data[0]["data"]["children"][0]["data"]["url"]
                
                embed = discord.Embed(title="Random Meme")
                embed.set_image(url=meme_url)
                
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message("Failed to fetch a meme. Please try again later.")

@bot.tree.command(name="avatar", description="Get the avatar of a user.")
async def avatar(interaction, member: discord.Member = None):
    if member is None:
        member = interaction.author
    
    embed = discord.Embed(title=f"{member.name}'s Avatar")
    embed.set_image(url=member.avatar.url)
    
    await interaction.response.send_message(embed=embed)

###########################################################

@bot.event
async def on_message_delete(message):
    if message.author == bot.user:
        return

#Help Command

@bot.tree.command(name="help", description="Shows all the commands.")
async def help(interaction: discord.Interaction):
    contents = [
        '''
        **Utilities:**

        `/ping`: Displays the bot's latency.
        `/announce [message]`: Announces a message in the current channel (requires Manage Messages permission).
        `/setjoinchannel [channel]`: Set the channel where join messages will be posted. (requires Manage Messages permission).
        `/setleavechannel [channel]`: Set the channel where leave messages will be posted. (requires Manage Messages permission).
        `/setjoinmessage [message]`: Set the message that will be sent when a user joins. (requires Manage Messages permission). Include the user's mention by putting `{user}` and `{number}` for the number of members
        `/setleavemessage [message]`: Set the message that will be posted when a user leaves. (requires Manage Messages permission). Include the user's mention by putting `{user}` and `{number}` for the number of members
        `/setlogchannel [channel]`: Set the channel where logs will be posted. (requires Manage Messages permission).
        `/settranscriptchannel [channel]`: Set the channel where transcripts will be posted. (requires Manage Messages permission).
        `/setticketcategory [category]`: Set the category where tickets will be created. (requires Manage Channels permission).
        `/setticketrole [role]`: Set the role that will be given to users when they create a ticket. (requires Manage Roles permission).
        `/setticketchannel [channel]`: Set the channel where tickets will be created. (requires Manage Channels permission).
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
    message = await interaction.response.send_message(embed=embed)
    bot_message = await interaction.channel.history(limit=1).__anext__()

    await bot_message.add_reaction("◀️")
    await bot_message.add_reaction("▶️")

    def check(reaction, user):
        return user == interaction.user and str(reaction.emoji) in ["◀️", "▶️"]

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=40, check=check)

            if str(reaction.emoji) == "▶️" and cur_page < pages - 1:
                cur_page += 1
                embed.description = contents[cur_page]
                embed.set_footer(text=f"Page {cur_page+1}/{pages}")
                await bot_message.edit(embed=embed)
                await bot_message.remove_reaction(reaction, user)

            elif str(reaction.emoji) == "◀️" and cur_page > 0:
                cur_page -= 1
                embed.description = contents[cur_page]
                embed.set_footer(text=f"Page {cur_page+1}/{pages}")
                await bot_message.edit(embed=embed)
                await bot_message.remove_reaction(reaction, user)

            else:
                await bot_message.remove_reaction(reaction, user)

        except asyncio.TimeoutError:
            await bot_message.delete()
            break

bot.run(token)
