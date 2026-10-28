# PyCord-Bot

A multipurpose Discord bot written in Python with [discord.py](https://github.com/Rapptz/discord.py). Works in any number of servers at once — every server's settings are stored separately.

> This bot is in active development. Much of the code was written with the help of AI, Stack Overflow, and YouTube tutorials.

## Features

- **Setup hub** — everything is configured from `/setup`: auto-mod, verification, logging, channel/role settings, and a one-click quick setup that creates channels and roles for you. Every panel has a Back button.
- **Tickets** — category-based support panel, claiming with a leaderboard, close-reason modal, HTML-free transcripts (an info embed plus a plain `.txt` log), blacklisting, and auto-close after 3 days of inactivity.
- **Audit logging** — 51 events across 19 categories, each with its own on/off toggle, plus per-channel overrides and full channel exclusion. See **[Audit logging](#audit-logging)** below for the full reference.
- **Auto-moderation** — blocked words (batch add/remove), Discord invites, external links, caps, mass mentions, and spam. Optional staff bypass and per-channel exemptions.
- **Verification** — captcha or one-click, with success/failure logging, stats, and manual verify for staff.
- **Lockdown** — lock or unlock channels instantly, with configurable default channels and automatic permission restoration.
- **Temporary voice channels** — join a hub to get your own channel with a control panel (lock, limit, rename, invite, kick). Empty channels clean themselves up.
- **Moderation** — mute/unmute, kick, ban/unban, purge, and a persistent mod-case history.
- **Self-maintaining database** — config rows create themselves, stale entries are only removed after Discord confirms they're really gone, and a rolling daily backup is kept.

## Requirements

- Python 3.10 or newer (developed on 3.15)
- A bot token from the [Discord Developer Portal](https://discord.com/developers/applications)
- **discord.py 2.5 or newer** (poll-vote and soundboard logging need it — the pinned `requirements.txt` has no version cap, so a fresh install gets a compatible version)

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt --upgrade
   ```

2. Create a file named `.env` next to `main.py` containing your token:

   ```env
   TOKEN=your_token_here
   ```

3. Run the bot:

   ```bash
   python main.py
   ```

The database (`bot_data.db`) is created automatically on first run. Data is kept between restarts, a backup is saved daily to `backups/` (newest 7 kept), and stale entries are cleaned every 6 hours.

## Running with Docker

The app runs from `/app` inside the container, so the volumes must mount there:

```bash
docker build -t pycord-bot .
docker run -d --name discord-bot --restart unless-stopped \
  -v "$(pwd)/bot_data.db:/app/bot_data.db" \
  -v "$(pwd)/.env:/app/.env" \
  pycord-bot
docker logs -f discord-bot
```

Add a `.dockerignore` so secrets and data don't get baked into the image:

```dockerignore
bot_data.db
bot_data.db.bak
.env
backups/
__pycache__/
*.pyc
```

Two things to watch out for:

- If Docker ever creates a **folder** named `bot_data.db` on the host (it does that when the file doesn't exist), delete it and `touch bot_data.db` before restarting — SQLite can't open a directory.
- Only ever run **one** bot container at a time. Two bots sharing one database will corrupt it.

## Using the bot

1. Invite the bot with the `bot` and `applications.commands` scopes.
2. Run `/setup` and click **Quick Setup** (or run `/setup-auto`). It creates and wires up the log channel, transcripts channel, verify channel, support and verified roles, ticket category, and a voice hub — skipping anything you've already set.
3. Post the public panels with `/sendticketpanel` and `/sendverifypanel`.
4. Adjust anything from the `/setup` panels. The **Server Settings** panel covers channels, roles, and messages; it also has an "enter manually" option if your server has more roles or channels than the dropdown can show (Discord's list caps at 25).
5. Turn on auto-mod and verification, then open **Log Settings** in `/setup` to choose which of the 51 log events you want on. Everything the panel does is covered in the **[Audit logging](#audit-logging)** section below — per-event toggles, per-channel overrides, and full channel exclusion.

Settings are per-server. If you set something in one server it won't appear in another — run the setup steps in each server you use.

Slash commands can take up to an hour to show up in a brand-new server for the first time.

## Commands

Setup and utilities:

- `/setup` — configuration hub (all settings panels live here)
- `/setup-auto` — create and configure channels/roles/categories automatically
- `/logstatus` — show which log categories are enabled (✅ / ⚠️ / ❌)
- `/logcategory` — enable or disable a whole log category at once
- `/logignore add` `/logignore remove` `/logignore list` — exclude channels from logging entirely
- `/backup` — database backup on demand
- `/cleanupdb` — remove database entries confirmed to point at deleted things
- `/help` — command list

Moderation:

- `/mute` `/unmute` — timeout or remove a timeout
- `/kick` `/ban` `/unban`
- `/clear` — purge up to 100 messages
- `/cases` — recent mod cases
- `/lockdown start` `/lockdown end` `/lockdown defaults`

Tickets:

- `/sendticketpanel` — post the ticket panel
- `/closeticket` — close a ticket
- `/ticketadd` `/ticketremove` — add or remove a user from a ticket
- `/ticketblacklist` — block users from opening tickets
- `/ticketstats` — stats and claim leaderboard

Verification:

- `/sendverifypanel` — post the verification panel
- `/verifyuser` — manually verify a member
- `/verificationstats` — pass/fail stats

Voice channels:

- `/voicehub` — create a hub or turn an existing voice channel into one
- `/voicehubremove` `/voicehubs` `/voicecleanup` — manage hubs and temp channels

Fun:

- `/hello` `/ping` `/math` `/dadjoke` `/advice` `/meme` `/cat` `/dog` `/eightball` `/roll` `/howgay` `/avatar` `/serverinfo` `/announce`

Older versions had `/set`, `/automod`, `/verification`, and `/logsettings` as separate commands — those are all panels inside `/setup` now.

## Audit logging

Everything the bot can log, and how to control it.

### What's logged

Every category below is made of **individual toggles** — e.g. Channel Created, Channel Updated, and Channel Deleted are three separate switches, not one:

| Category | Events |
|---|---|
| Messages | Message Deleted, Message Edited, Message Purge |
| Channels | Channel Created, Channel Updated, Channel Deleted |
| Roles | Role Created, Role Updated, Role Deleted |
| Threads | Thread Created, Thread Updated, Thread Deleted |
| Users | Member Joined, Member Left, Profile Changes (name/avatar), Nickname Changes, Role Add/Remove |
| Moderation & Punishments | Mod Commands (kick/warn/etc.), Bans, Unbans, Timeouts (incl. manual ones) |
| Server | Invite Created, Invite Deleted, Server Settings Changed |
| Voice | Voice Join, Voice Leave, Voice Move, Temp Channel Created |
| Events (Scheduled) | Event Created, Event Updated, Event Deleted |
| Stage | Stage Started, Stage Updated, Stage Ended |
| Emojis | Emoji Changes (added/removed/renamed) |
| Stickers | Sticker Changes |
| Soundboard | Sound Added, Sound Updated, Sound Removed |
| Applications & Webhooks | Integration/Bot Changes, Webhook Changes |
| Polls | Vote Added, Vote Removed |
| Discord AutoMod | AutoMod Actions |
| Tickets | Ticket Opened, Ticket Closed, Ticket Claimed |
| Verification | Verification Passed, Verification Failed |
| Lockdowns | Lockdown Started, Lockdown Ended |

### The Log Settings panel (`/setup → Log Settings`)

- **Overview** — one line per category: ✅ all on · ⚠️ partially on · ❌ all off.
- **Toggle individual events** — pick a category from the dropdown to get a menu of just that category's events, pre-set to what's on. It saves automatically; picking none turns the whole category off.
- **🎟 Channel Overrides** — silence *specific events* in one channel while everything else keeps logging ("in #general, don't log Message Edited but keep Message Deleted"). Three steps: pick the channel → pick the event group → tick the events to silence. The panel shows everything currently silenced in that channel.
- **Channel picker** — excludes channels from logging **entirely** (nothing logs there).
- **🐞 Debug Console Logs** — prints "DELETE/EDIT/PURGE captured" style traces to the bot's console for troubleshooting. Off by default; only affects the console, never the Discord log channel.

### How the toggles stack

Three layers, checked in order for every event:

1. **Event toggle** — each of the 51 individual events can be on or off (missing row = on).
2. **Channel override** — specific events silenced in a channel stop logging *there only*, even if they're on globally.
3. **Ignored channel** — nothing is logged in that channel at all (structural events like channel/role created-or-deleted still log).

Threads follow their parent channel, and setting an override on a **category channel** applies it to every channel inside. A category turned off globally never logs anywhere, no matter what the channel settings say.

### Quick commands

- `/logstatus` — overview embed (✅ / ⚠️ / ❌) with ignored channels
- `/logcategory <category> <on/off>` — flip a whole category at once
- `/logignore add|remove|list` — manage fully-ignored channels

## Tickets and transcripts

When a ticket closes, the transcripts channel and the person who opened the ticket each receive two messages:

1. An embed with the ticket info — who opened it, who claimed it, who closed it, the reason, creation time, and message count.
2. A `transcript-<channel>.txt` file with the raw message log, one message per line.

## Troubleshooting

- **`TOKEN not found`** — `.env` must be next to `main.py` (or mounted to `/app/.env` in Docker) and contain `TOKEN=...`.
- **Bot doesn't respond to commands** — wait a minute for the command sync, or re-invite with the `applications.commands` scope.
- **Settings disappear after a rebuild (Docker)** — the volume must target `/app/bot_data.db`; see the Docker section.
- **`unable to open database file`** — Docker created a folder named `bot_data.db`; remove it and `touch bot_data.db`.
- **Verification role not assigned** — the bot's highest role must be above the verified role in Server Settings → Roles.
- **Logs not arriving** — open `/setup`; if the log channel shows as missing, re-set it in Server Settings and check the channel permissions. Also check the event is toggled on in **Log Settings** and the channel isn't ignored or overridden.
- **Console spammed with "DELETE captured" / "EDIT captured" lines** — those are debug traces. They're off by default; toggle them with the 🐞 button in the **Log Settings** panel.
- **"You need the Manage Server permission"** — panel buttons check permissions when clicked; admins should use the panels or grant Manage Server.
- **Deleted message shows "not in cache"** — the message was older than the bot's cache window. The delete is still logged, but the content is unrecoverable.

## Bug reports

Found a bug? Message `duziy` on Discord or open an issue on GitHub. Fixes can take a while to roll out.

## To do

- Music integration (Spotify/YouTube playback)

Made by **duziy**
