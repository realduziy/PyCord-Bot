# PyCord-Bot

<div align="center">

A multipurpose Discord bot coded in Python using [discord.py](https://github.com/Rapptz/discord.py).

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?logo=discord&logoColor=white)
![License](https://img.shields.io/badge/Status-Active%20Development-green)

</div>

> [!NOTE]
> This bot is in active development. Most of the code was put together with the help of AI, Stack Overflow, and YouTube tutorials!

---

## 📖 Table of Contents

- [Key Features](#-key-features)
- [Installation & Setup](#-installation--setup)
- [Running with Docker](#-running-with-docker)
- [Multiple Servers](#-multiple-servers)
- [Configuration](#-configuration)
- [Command Overview](#-command-overview)
- [Troubleshooting](#-troubleshooting)
- [Bug Reports](#-bug-reports)
- [To-Do List](#-to-do-list)

---

## ⚡ Key Features

| Feature | Description |
| --- | --- |
| 🧩 **Modular Cog Architecture** | The codebase is fully organized into Cogs for cleaner code and easier maintenance. |
| 🎛️ **Unified Setup Hub** | Everything is configured from one place: `/setup` opens an interactive hub — Auto-Mod, Verification, Log Settings, and Server Settings panels, plus one-click Quick Setup. Every panel has ⬅️ Back navigation and matching ✅/❌ feedback. |
| 🎫 **Ticket System** | Full ticketing workflow — support panel with category selection, ticket claiming (with leaderboard in `/ticketstats`), close-reason modal, automatic HTML transcripts sent to a custom channel and DM'd to the opener, blacklisting, and inactivity auto-close. |
| 📋 **Full Audit Logging** | Message deletes/edits, channel & role changes, manual bans/timeouts (even done by hand), invites, nickname/role changes, joins/leaves — all toggleable per category from the hub's Log Settings panel. |
| 🛡️ **Auto-Moderation** | Configurable filters for blocked words (with comma-separated batch add/remove), Discord invites, external links, excessive caps, mass mentions, and message spam — with an optional staff bypass and per-channel exemptions. |
| 🎟️ **Verification System** | Captcha or one-click verification with success/failure logging, statistics, and manual verification for staff. |
| 🔒 **Lockdown System** | Lock channels instantly — pick defaults with `/lockdown defaults`, or fall back to the current channel. Previous permission states are restored on unlock. |
| 🎙️ **Temporary Voice Channels** | VoiceMaster-style hubs: join a hub to spawn your own temp channel with a control panel in its chat — lock, user limit, rename, invite, kick. Empty channels are deleted automatically. |
| 🧹 **Self-Healing Database** | Config rows create themselves on demand, stale entries are only removed after Discord confirms they're really gone (never on a cache guess), a rolling daily backup runs automatically (newest 7 kept), and the bot logs a per-guild config summary at startup. |
| 🔨 **Moderation Tools** | Mute, kick, ban, unban, unmute, message purge, and a persistent mod-case history. |

---

## 📦 Installation & Setup

### Prerequisites

- **Python 3.10 or newer** (developed on 3.15) — check with `python --version`
- A Discord bot token from the [Discord Developer Portal](https://discord.com/developers/applications)

### 1️⃣ Install dependencies

Clone or download the project, then open a terminal in the project folder and run:

```bash
pip install -r requirements.txt --upgrade
```

> `PyNaCl` is included for voice support — if you skip it, voice channels won't work.

### 2️⃣ Configure your token

Create a file named `.env` in the project folder with your bot token:

```env
TOKEN=your_token_here
```

### 3️⃣ Run the bot

```bash
python main.py
```

> [!TIP]
> The database (`bot_data.db`) is created automatically on first run — no manual setup needed. Your data is preserved between updates, a fresh backup is saved daily to `backups/` (newest 7 kept), and stale references are cleaned up every 6 hours. Use `/backup` or `/cleanupdb` to run either on demand.

---

## 🐳 Running with Docker

A `Dockerfile` is included. The important detail: **the app runs from `/app` inside the container**, so volumes must mount there — mounting anywhere else means your data lives in the container layer and is lost on every rebuild.

```bash
docker build -t pycord-bot .
docker run -d --name discord-bot --restart unless-stopped \
  -v "$(pwd)/bot_data.db:/app/bot_data.db" \
  -v "$(pwd)/.env:/app/.env" \
  pycord-bot
docker logs -f discord-bot
```

Keep `.env` and database files out of the image with a `.dockerignore`:

```dockerignore
bot_data.db
bot_data.db.bak
.env
backups/
__pycache__/
*.pyc
```

> [!WARNING]
> If Docker ever creates a **directory** named `bot_data.db` on the host (it does this when a `-v` source file doesn't exist), delete it and `touch bot_data.db` before restarting — SQLite can't open a directory, and `COPY . .` would bake it into the image.
>
> Also make sure only **one** bot container is ever running — two bots on one database will fight over it.

---

## 🌐 Multiple Servers

The bot works in **any number of servers at the same time**. Every server's settings — tickets, voice hubs, lockdown defaults, auto-mod, verification, logs — are stored separately in the database, so nothing ever leaks between servers.

**To add the bot to another server:**
1. Make sure **Public Bot** is enabled in the [Discord Developer Portal](https://discord.com/developers/applications) → your app → **Bot**
2. Invite it with the `bot` and `applications.commands` scopes
3. Run **`/setup`** in the new server → click **Quick Setup** (or `/setup-auto`) — the bot creates and wires up channels, roles, and the voice hub for that server only
4. Each server manages itself independently — panels only affect the server you open them in

> [!NOTE]
> Slash commands can take up to an hour to appear in a brand-new server for the first time. Restarting the bot usually speeds this up.

## 🔧 Configuration

Everything is configured from the **`/setup` hub**. Open it and you'll see a live status board plus five buttons:

| Button | What it does |
| --- | --- |
| 🛡️ **Auto-Mod** | Toggle the filter on/off, flip invites/links/staff-bypass, and adjust caps %, mention limits, blocked words and exempt channels |
| 🎟️ **Verification** | Toggle on/off, switch between Captcha and One-click, pick the verified role |
| 📋 **Log Settings** | Dropdown-toggle every log category |
| 🛠️ **Server Settings** | Set log/join/leave/ticket/transcripts/verify channels, ticket category, support & verified roles, and join/leave messages — all from dropdowns (with an **⌨️ Enter manually** fallback when a server has more than 25 channels/roles) |
| ⚡ **Quick Setup** | Creates everything missing in one go |

Every panel answers with the same ✅/❌ embeds and has a ⬅️ **Back** button that returns you to the hub in-place. Config actions require the **Manage Server** permission (Quick Setup requires Administrator), checked at click time.

After Quick Setup, post the public panels with **`/sendticketpanel`** and **`/sendverifypanel`**.

---

## 🗂️ Command Overview

### Setup & Utilities
| Command | Description |
| --- | --- |
| `/setup` | Configuration hub — status board + all settings panels |
| `/setup-auto` | Create and configure channels/roles/categories automatically |
| `/backup` | Create a database backup on demand (auto-backup runs daily) |
| `/cleanupdb` | Remove database entries confirmed to point at deleted channels/roles/guilds |
| `/logstatus` | Show which log events are currently enabled |
| `/help` | Show all available commands |

### Moderation
| Command | Description |
| --- | --- |
| `/mute` `/unmute` | Timeout or un-timeout a member |
| `/kick` `/ban` `/unban` | Remove or restore members |
| `/clear` | Purge up to 100 messages |
| `/cases` | View recent moderation cases |
| `/lockdown start` `/lockdown end` `/lockdown defaults` | Lock or unlock channels (with configurable defaults) |

### Tickets
| Command | Description |
| --- | --- |
| `/sendticketpanel` | Post the ticket panel |
| `/closeticket` | Close a ticket (with reason modal or command) |
| `/ticketadd` `/ticketremove` | Add or remove a user from a ticket |
| `/ticketblacklist` | Manage the ticket blacklist |
| `/ticketstats` | Ticket statistics + claim leaderboard |

### Verification
| Command | Description |
| --- | --- |
| `/sendverifypanel` | Post the verification panel |
| `/verifyuser` | Manually verify a member |
| `/verificationstats` | Pass/fail statistics |

### Voice Channels
| Command | Description |
| --- | --- |
| `/voicehub [channel] [name]` | Create a hub, or turn an existing voice channel into one |
| `/voicehubremove` | Remove hub status from a channel |
| `/voicehubs` | List all hubs |
| `/voicecleanup` | Delete all empty temp channels |

### Fun
| Command | Description |
| --- | --- |
| `/hello` `/ping` `/math` | Basics |
| `/dadjoke` `/advice` `/meme` `/cat` `/dog` `/eightball` `/roll` `/howgay` | Random fun |
| `/avatar` `/serverinfo` `/announce` | Info & utility |

> [!NOTE]
> Older versions had `/set`, `/automod`, `/verification`, and `/logsettings` as separate commands. Those are all panels inside `/setup` now — one front door, everything in one place.

---

## 🔧 Troubleshooting

**Installation issues:**

```bash
# If pip fails, try:
python -m pip install -r requirements.txt --upgrade

# Make sure pip is up to date:
python -m pip install --upgrade pip
```

**Common runtime issues:**

| Problem | Fix |
| --- | --- |
| `TOKEN not found` | Make sure your `.env` file is next to `main.py` (or mounted to `/app/.env` in Docker) and contains `TOKEN=...` |
| Bot doesn't respond to slash commands | Wait a minute for commands to sync, or re-invite the bot with the `applications.commands` scope |
| Settings disappear after a rebuild (Docker) | Your volume mount must target `/app/bot_data.db` — see [Running with Docker](#-running-with-docker) |
| `unable to open database file` (Docker) | Docker created a *folder* named `bot_data.db`; `rm -rf` it and `touch bot_data.db` |
| Role not assigned on verification | The bot's highest role must be **above** the Verified role in Server Settings → Roles |
| Log embeds never arrive | Run `/setup` — if the log channel shows ⚠️ or ❌, re-set it in Server Settings and check the channel permissions |
| Deleted message shows "not in cache" | The message was older than the bot's cache window — content is unrecoverable, but the delete is still logged |
| A panel button shows "❌ You need the Manage Server permission" | That's the click-time permission check — server admins can grant Manage Server, or use the panels themselves |

---

## 🐛 Bug Reports

If you find a bug, let me know via Discord (`@duziy`) or open an issue on GitHub. Please note that updates aren't frequent, so a fix might take some time to roll out!

---

## 📋 To-Do List

- [x] **Refactor to Cogs** — Converted the entire codebase over to Cogs for cleaner structure and organization.
- [x] **Unified Setup Hub** — One `/setup` front door: status board, Auto-Mod/Verification/Log Settings/Server Settings panels, Quick Setup, and ⬅️ Back navigation everywhere.
- [x] **Ticket System** — Support panel with categories, claiming + leaderboard, close-reason modal, HTML transcripts, blacklist, inactivity auto-close.
- [x] **Full Audit Logging** — Messages, channels, roles, bans/timeouts, invites, member changes — all toggleable.
- [x] **Auto-Moderation** — Words, invites, links, caps, mentions, spam, staff bypass, per-channel exemptions.
- [x] **Verification System** — Captcha or one-click, with logging, stats, and manual verification.
- [x] **Lockdown Command** — Configurable default channels, current-channel fallback, permission restoration.
- [x] **Temporary Voice Channels** — VoiceMaster-style hubs with control panels (lock, limit, rename, invite, kick).
- [x] **Database Maintenance** — API-confirmed stale cleanup, self-healing config rows, daily rolling backups, per-guild startup summary.
- [ ] **Music Integration** — Add support for Spotify and YouTube playback.

---

<div align="center">

Made with ❤️ by **duziy**

</div>
