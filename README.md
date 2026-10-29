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
- **Reaction roles** — button or dropdown role menus, emoji-reaction bindings, and a guided wizard. Build a whole menu in one flow, then edit or delete it anytime from a live panel. See **[Reaction roles](#reaction-roles)** below.
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
5. Turn on auto-mod and verification, then open **Log Settings** in `/setup` to choose which of the 56 log events you want on. Everything the panel does is covered in the **[Audit logging](#audit-logging)** section below — per-event toggles, per-channel overrides, and full channel exclusion.

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

Reaction roles:

- `/reactionrole wizard` — guided setup: name/description → style & mode → click roles to add them → pick a channel → posted. Nothing is saved until you post.
- `/reactionrole manage` — full edit panel for an existing menu: add/remove/edit roles, rename, switch style/mode, post/move to another channel, or delete (with confirmation). Every change saves instantly and updates the live menu.
- `/reactionrole list` — all menus with style, mode, and posted status
- `/reactionrole create` `/reactionrole add` `/reactionrole remove` `/reactionrole post` `/reactionrole delete` — manual equivalents of the wizard steps, if you prefer scripting it
- `/reactionrole bind` `/reactionrole unbind` `/reactionrole bindings` — bind an emoji reaction on any message to a role (classic add-by-reacting flow)
- Right-click any message → **Apps → Bind Reaction Role** — the fastest way to attach a reaction role; just type the role and emoji

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
| Channels | Channel Created, Channel Renamed, Channel Topic Changed, Channel Settings Changed (slowmode / NSFW / category), Channel Permissions Changed, Channel Deleted |
| Roles | Role Created, Role Renamed, Role Settings Changed (color / hoist / mentionable), Role Permissions Changed, Role Deleted |
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

1. **Event toggle** — each of the 56 individual events can be on or off (missing row = on).
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

## Reaction roles

Two ways to hand out roles, both fully managed from Discord:

### Button/dropdown menus (recommended)

`/reactionrole wizard` walks you through the whole setup in one flow:

1. **Name & description** — shown at the top of the menu embed.
2. **Look & behavior** — buttons (one per role) or a dropdown; *toggle* lets members stack roles, *exclusive* swaps the previously picked role for the new one.
3. **Roles** — pick roles from a dropdown and they're added instantly (role name is used as the label). Optionally use **✏️ Edit an entry** to give a role a custom emoji, label, or description.
4. **Preview & post** — see the exact embed the menu will use, pick a channel, and post.

Once a menu exists, `/reactionrole manage <name>` opens a live control panel for it:

- **Add / remove / edit roles** — changes save instantly and refresh the posted message.
- **Rename, rewrite the description, switch style or mode** — same live update.
- **Post/Move** — post an unposted menu anywhere, or move a posted one (the old message is deleted).
- **Delete** — asks for confirmation, then removes the menu and its message.

Menus survive restarts (the views are re-registered from the database on startup), so members can keep clicking them forever.

### Emoji reactions on any message

For the classic "react with 🎮 to get the Gaming role" flow:

- Right-click a message → **Apps → Bind Reaction Role** → type the role and emoji, or
- `/reactionrole bind channel:#roles message_id:… role:@Gaming emoji:🎮`

Reactions the bot adds itself toggle the role on add and remove it on remove. Manage existing bindings with `/reactionrole bindings` and `/reactionrole unbind`.

The bot's highest role must sit **above** every role it hands out, in Server Settings → Roles, and it needs **Manage Roles**.

## Troubleshooting

- **`TOKEN not found`** — `.env` must be next to `main.py` (or mounted to `/app/.env` in Docker) and contain `TOKEN=...`.
- **Bot doesn't respond to commands** — wait a minute for the command sync, or re-invite with the `applications.commands` scope.
- **Settings disappear after a rebuild (Docker)** — the volume must target `/app/bot_data.db`; see the Docker section.
- **`unable to open database file`** — Docker created a folder named `bot_data.db`; remove it and `touch bot_data.db`.
- **Verification role not assigned** — the bot's highest role must be above the verified role in Server Settings → Roles.
- **Reaction-role buttons do nothing** — same cause: the bot's role must be above the roles it assigns, and it needs Manage Roles. For emoji bindings, also check the message is in a channel the bot can read.
- **A menu edit didn't update the posted message** — the original message may have been deleted; use **Post/Move** in `/reactionrole manage` to publish a fresh copy.
- **Logs not arriving** — open `/setup`; if the log channel shows as missing, re-set it in Server Settings and check the channel permissions. Also check the event is toggled on in **Log Settings** and the channel isn't ignored or overridden.
- **Console spammed with "DELETE captured" / "EDIT captured" lines** — those are debug traces. They're off by default; toggle them with the 🐞 button in the **Log Settings** panel.
- **"You need the Manage Server permission"** — panel buttons check permissions when clicked; admins should use the panels or grant Manage Server.
- **Deleted message shows "not in cache"** — the message was older than the bot's cache window. The delete is still logged, but the content is unrecoverable.

## Bug reports

Found a bug? Message `duziy` on Discord or open an issue on GitHub. Fixes can take a while to roll out.

## To do

- Music integration (Spotify/YouTube playback)

Made by **duziy**
