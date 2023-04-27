import discord
from discord.ext import commands
import json
import os
import requests
import random
import asyncio
import asyncpraw

if os.path.exists(os.getcwd() + "/config.json"):
    with open("./config.json") as f:
        configData = json.load(f)
else:
    configTemplate = {"Token": ""}
    with open(os.getcwd() + "/config.json", "w+") as f:
        json.dump(configTemplate, f)

token = configData["Token"]

bot = commands.Bot(command_prefix='>')


async def on_ready():
 await bot.wait_until_ready()

 statuses = [f"listening on {len(bot.guilds)} server's", "Need help? do /help"]

 while not bot.is_closed():

   status = random.choice(statuses)

   await bot.change_presence(activity=discord.Game(name=status))

   await asyncio.sleep(5)

bot.loop.create_task(on_ready())
print("Bot is ready!")


@bot.slash_command()
async def hello(ctx):
    await ctx.send(f'Hello, I am a bot made by the one and only duziy!')
    return


@bot.slash_command()
async def math(ctx, *, expression: str):
    calculation = eval(expression)
    await ctx.send('Math: {}\nAnswer: {}'.format(expression, calculation))


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
    await ctx.send(f'Cleared {len(deleted)} messages.')


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
async def help(ctx):
    embed = discord.Embed(title="Here is a list of all of the bot commands", color=0xFF0000, description='''
Utilities:

/ping: Displays the bot's latency.
/announce [message]: Announces a message in the current channel (requires Manage Messages permission).
/clear [amount]: Clears the specified amount of messages in the current channel (requires Manage Messages permission).

Moderation:

/kick [user]: Kicks the specified user from the server (requires Kick Members permission).
/ban [user]: Bans the specified user from the server (requires Ban Members permission).
/unban [user]: Unbans the specified user from the server (requires Ban Members permission).
/lockdown [channel]: Locks down the specified channel (requires Administrator permission).
/unlock [channel]: Unlocks the specified channel (requires Administrator permission).

Fun:

/hello: Greets the user.
/meme: Fetches a random meme.
/dadjoke: Fetches a random dad joke.
/cat: Fetches a random picture of a cat.
/dog: Fetches a random picture of a dog.
/eightball [question]: Responds with a random 8ball response to the given question.
/roll [number of sides] : Rolls a dice with the specified number of sides.
/math [your math question] : Does simple math for you.
''')
    await ctx.send(embed=embed)

bot.run(token)
