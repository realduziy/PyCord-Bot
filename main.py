import discord
import os.path
import json
import os
import requests
import random
import asyncio
import asyncpraw
import re
from discord.ext import commands
from pathlib import Path

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(intents=intents)

if os.path.exists(os.getcwd() + "/config.json"):
    with open("./config.json") as f:
        configData = json.load(f)
else:
    configTemplate = {"Token": ""}
    with open(os.getcwd() + "/config.json", "w+") as f:
        json.dump(configTemplate, f)

token = configData["Token"]


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
    if isinstance(error, commands.CommandNotFound):
        return  # Don't log or print anything
    print(f"An error occurred: {error}")

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


@bot.slash_command()
async def setjoinchannel(ctx, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need to be an administrator to use this command.")
        return

    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join"] = channel.id
    save_channels(channels)
    await ctx.send(f"Join channel set to {channel.mention}")


@bot.slash_command()
async def setleavechannel(ctx, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need to be an administrator to use this command.")
        return

    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["leave"] = channel.id
    save_channels(channels)
    await ctx.send(f"Leave channel set to {channel.mention}")


@bot.slash_command()
async def setjoinmessage(ctx, message: str):
    guild_id = str(ctx.guild.id)
    create_config(guild_id)
    channels = load_channels()
    channels[guild_id]["join_message"] = message
    save_channels(channels)
    await ctx.send(f"Join message set to '{message}'")


@bot.slash_command()
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
    if str(member.guild.id) in channels and channels[str(member.guild.id)]["join"] is not None:
        welcome_channel = bot.get_channel(
            channels[str(member.guild.id)]["join"])
        join_message = channels[str(member.guild.id)].get(
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
        leave_channel = bot.get_channel(channels[guild_id]["leave"])
        leave_message = channels[guild_id].get(
            "leave_message", "Goodbye {user}! We are now {count} members.")
        leave_message = leave_message.replace("{user}", member.mention).replace(
            "{count}", str(member.guild.member_count))
        await leave_channel.send(leave_message)


##########################################################

@bot.slash_command()
async def hello(ctx):
    await ctx.send(f'Hello, I am a bot made by the one and only duziy!')
    return


@bot.slash_command(name="math", description="Performs basic math operations.")
async def math(ctx, *, expression: str):
    try:
        # Split input expression into separate operations
        operations = re.findall(r'\d+\s*[+\-*/]\s*\d+', expression)

        # Evaluate each operation and accumulate the result
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
async def announce(ctx, *, message=None):
   if message == None:
       return
   if (not ctx.author.guild_permissions.manage_messages):
    await ctx.send('You do not have permission to use this command.')
    return
   else:
       embed = discord.Embed(color=0xFF0000, title='', description=message)
       embed.set_footer(text=f'Announced By {ctx.author.name}')
       await ctx.send(embed=embed)


@bot.slash_command(name="clear", description="Clears the specified number of messages in the channel.")
async def clear(ctx, amount: int):
    if not ctx.author.guild_permissions.manage_messages:
        await ctx.send('You do not have permission to use this command.')
        return
    if amount < 1 or amount > 100:
        await ctx.send('You can only clear between 1 and 100 messages at a time!')
        return
    deleted = await ctx.channel.purge(limit=amount)
    msg = await ctx.send(f'Cleared {len(deleted)} messages.')
    await asyncio.sleep(5)  # wait for 5 seconds
    await msg.delete()  # delete the bot's message after 5 seconds


@bot.slash_command()
async def kick(ctx, user: discord.User):
  guild = ctx.guild
  mbed = discord.Embed(
      description=f"{user} has been kicked"
  )
  if (not ctx.author.guild_permissions.kick_members):
    await ctx.send('You do not have permission to use this command.')
    return
  if ctx.author.guild_permissions.kick_members:
    await ctx.send(embed=mbed)
    await guild.kick(user=user)


@bot.slash_command()
async def ban(ctx, user: discord.User):
  guild = ctx.guild
  mbed = discord.Embed(
      description=f"{user} has been banned"
  )
  if (not ctx.author.guild_permissions.ban_members):
    await ctx.send('You do not have permission to use this command.')
    return
  if ctx.author.guild_permissions.ban_members:
    await ctx.send(embed=mbed)
    await guild.ban(user=user)


@bot.slash_command()
async def unban(ctx, user: discord.User):
  guild = ctx.guild
  mbed = discord.Embed(
      description=f"{user} has been unbanned"
  )
  if (not ctx.author.guild_permissions.ban_members):
    await ctx.send('You do not have permission to use this command.')
    return
  if ctx.author.guild_permissions.ban_members:
    await ctx.send(embed=mbed)
    await guild.unban(user=user)


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

client = discord.Client()
reddit = asyncpraw.Reddit(
    client_id='BidTgjqMzuCIAG0VAQaC5g',
    client_secret='ICLztB-C7vFra9-XFPBLuWbcr1XIBA',
    user_agent='duziy bot',
)


@bot.slash_command()
async def meme(ctx):
    subreddit_name = 'memes'
    subreddit = await reddit.subreddit(subreddit_name)
    all_posts = []
    async for post in subreddit.hot(limit=50):
        all_posts.append(post)
    random_post = random.choice(all_posts)
    title = random_post.title
    url = random_post.url
    embed = discord.Embed(title=title)
    embed.set_image(url=url)
    await ctx.send(embed=embed)


@bot.slash_command()
async def advice(ctx):
    url = 'https://api.adviceslip.com/advice'
    response = requests.get(url)
    if response.status_code == 200:
        advice = response.json()['slip']['advice']
        await ctx.send(advice)
    else:
        await ctx.send('Oops, something went wrong. Please try again later.')


@bot.slash_command()
async def help(ctx):
    embed = discord.Embed(title="Here is a list of all of the bot commands", color=0xFF0000, description='''
Utilities:

/ping: Displays the bot's latency.
/announce [message]: Announces a message in the current channel (requires Manage Messages permission).
/setjoinchannel [channel] - Set the channel where join messages will be posted. Only users with admin permissions can use this command.
/setleavechannel [channel] - Set the channel where leave messages will be posted. Only users with admin permissions can use this command.
/setjoinmessage [message] - Set the message that will be posted when a user joins the server. Only users with admin permissions can use this command. You can include the user's mention by including `{user}` and`{number}` for what number of user that they are in the message.
/setleavemessage [message] - Set the message that will be posted when a user leaves the server. Only users with admin permissions can use this command. You can include the user's mention by including `{user}` and`{number}` for what number of user that they are in the message.

Moderation:

/kick [user]: Kicks the specified user from the server (requires Kick Members permission).
/ban [user]: Bans the specified user from the server (requires Ban Members permission).
/unban [user]: Unbans the specified user from the server (requires Ban Members permission).
/lockdown [channel]: Locks down the specified channel (requires Administrator permission).
/unlock [channel]: Unlocks the specified channel (requires Administrator permission).
/clear [amount]: Clears the specified amount of messages in the current channel (requires Manage Messages permission).

Fun:

/hello: Greets the user.
/meme: Fetches a random meme.
/dadjoke: Fetches a random dad joke.
/cat: Fetches a random picture of a cat.
/dog: Fetches a random picture of a dog.
/eightball [question]: Responds with a random 8ball response to the given question.
/roll [number of sides] : Rolls a dice with the specified number of sides.
/math [your math question] : Does simple math for you.
/advice : Gives you some random advice.
''')
    await ctx.send(embed=embed)

bot.run(token)
