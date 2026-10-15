# PyCord-Bot

<div align="center">

A multipurpose Discord bot coded in Python using [discord.py](https://github.com/Rapptz/discord.py).

![Python](https://img.shields.io/badge/Python-3.15%2B-blue?logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?logo=discord&logoColor=white)
![License](https://img.shields.io/badge/Status-Active%20Development-green)

</div>

> [!NOTE]
> This bot is in active development. Most of the code was put together with the help of AI, Stack Overflow, and YouTube tutorials!

---

## 📖 Table of Contents

- [Key Features](#-key-features)
- [Installation & Setup](#-installation--setup)
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
| ⚙️ **One-Click Server Setup** | `/setup auto` creates and configures channels, roles, and categories automatically — or use `/setup` to check what's missing and `/set` to configure individual settings. |
| 🎫 **Ticket System** | Full ticketing workflow — support panel with category selection, ticket claiming (with leaderboard), close-reason modal, automatic transcripts sent to a custom channel, blacklisting, and ticket statistics. |
| 📋 **Customizable Logging** | Every loggable event (moderation, auto-mod, tickets, verification, joins/leaves, lockdowns) can be toggled on or off per server with `/logsettings`. |
| 🛡️ **Auto-Moderation** | Configurable filters for blocked words (with comma-separated batch add/remove), Discord invites, external links, excessive caps, mass mentions, and message spam — with an optional staff permission bypass. |
| 🎟️ **Verification System** | Captcha or one-click verification with full success/failure logging, statistics, and manual verification for staff. |
| 🔒 **Lockdown System** | Lock channels instantly — pick defaults with `/lockdown defaults`, or fall back to the current channel. Previous permission states are restored on unlock. |
| 🎙️ **Temporary Voice Channels** | VoiceMaster-style hubs: join a hub channel to spawn your own temp channel (named `User's Lounge`), ownership transfers when you leave, and empty channels are deleted automatically. Use an existing channel or create a new hub — no templates needed. |
| 🧹 **Self-Maintaining Database** | Automatically removes stale entries (deleted channels, roles, guilds) on startup and every 6 hours — or run `/cleanupdb` anytime to see what was pruned. |
| 🔨 **Moderation Tools** | Mute, kick, ban, unban, unmute, message purge, and a persistent mod-case history. |

---

## 📦 Installation & Setup

### Prerequisites

- **Python 3.15** (recommended — the bot is developed and tested on it; 3.10 and newer also work) — check with `python --version`
- A Discord bot token from the [Discord Developer Portal](https://discord.com/developers/applications)

### 1️⃣ Install dependencies

Clone or download the project, then open a terminal in the project folder and run:

```bash
pip install -r requirements.txt --upgrade
```

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
> The database (`bot_data.db`) is created and migrated automatically on first run — no manual setup needed. Your data is preserved between updates, and stale references (deleted channels/roles) are cleaned up automatically every 6 hours. Use `/cleanupdb` to run a cleanup on demand.

---

## 🔧 Configuration

Once the bot is in your server, configure it with slash commands:

1. Run **`/setup auto`** to let the bot create channels, roles, and categories for you — or set things manually with **`/set`**.
2. Run **`/setup`** anytime to see a checklist of what's configured and what's missing.
3. Send the panels: **`/sendticketpanel`** and **`/sendverifypanel`**.
4. Fine-tune with **`/logsettings`**, **`/automod`**, **`/verification`**, and **`/lockdown defaults`**.

---

## 🗂️ Command Overview

### Utilities
| Command | Description |
| --- | --- |
| `/set` | Configure any bot setting (channels, roles, messages) |
| `/setup` | Show a configuration checklist for this server |
| `/setup auto` | Automatically create and configure channels/roles/categories |
| `/cleanupdb` | Remove database entries pointing at deleted channels/roles/guilds |
| `/help` | Show all available commands |

### Moderation
| Command | Description |
| --- | --- |
| `/mute` `/unmute` | Timeout or un-timeout a member |
| `/kick` `/ban` `/unban` | Remove or restore members |
| `/clear` | Purge up to 100 messages |
| `/cases` | View recent moderation cases |
| `/lockdown start/end/defaults` | Lock or unlock channels (with configurable defaults) |

### Auto-Moderation
| Command | Description |
| --- | --- |
| `/automod` | Configure filters: words, invites, links, caps, mentions, staff bypass |

### Verification
| Command | Description |
| --- | --- |
| `/verification` | Enable/disable, set the role, choose captcha or one-click mode |
| `/sendverifypanel` | Post the verification panel |
| `/verifyuser` | Manually verify a member |
| `/verificationstats` | Pass/fail statistics |

### Tickets
| Command | Description |
| --- | --- |
| `/sendticketpanel` | Post the ticket panel |
| `/adduser` `/removeuser` | Add or remove a user from a ticket |
| `/ticketblacklist` | Manage the ticket blacklist |
| `/ticketstats` | Ticket statistics |
| `/claimstats` | Claim leaderboard |

### Voice Channels
| Command | Description |
| --- | --- |
| `/voicehub [channel] [name]` | Create a hub, or turn an existing voice channel into one (by mention or ID). New hubs default to `➕ Join to Create` and can be renamed like any channel |
| `/voicehubremove` | Remove hub status from a channel |
| `/voicehubs` | List all hubs |
| `/voicecleanup` | Delete all empty temp channels |

### Logging
| Command | Description |
| --- | --- |
| `/logsettings` | Toggle which events are logged |
| `/logstatus` | Show current log settings |

---

## 🔧 Troubleshooting

**Installation issues:**

```bash
# If pip fails, try:
python -m pip install -r requirements.txt --upgrade

# Make sure pip is up to date:
python -m pip install --upgrade pip
```

**Still having issues?** Try completely uninstalling Python and grabbing the newest version from the [Python Downloads Page](https://www.python.org/downloads/).

**Common runtime issues:**

| Problem | Fix |
| --- | --- |
| `TOKEN not found` | Make sure your `.env` file is in the same folder as `main.py` and contains `TOKEN=...` |
| Bot doesn't respond to slash commands | Wait a minute for commands to sync, or re-invite the bot with the `applications.commands` scope |
| Role not assigned on verification | Make sure the bot's highest role is **above** the Verified role in Server Settings → Roles |

---

## 🐛 Bug Reports

If you find a bug, let me know via Discord (`@duziy`) or open an issue on GitHub. Please note that updates aren't frequent, so a fix might take some time to roll out!

---

## 📋 To-Do List

- [x] **Refactor to Cogs** — Converted the entire codebase over to Cogs for cleaner structure and organization.
- [x] **Setup Command** — Added a quick setup command for server configuration.
- [x] **Ticket System Updates** — Made progress expanding and refining the ticket system.
- [x] **Logging System** — Full expansion complete: auto-moderation logs, custom ticket transcript channels, verification logs, member join/leave logs, and per-event toggles.
- [x] **Auto-Moderation** — Fully customizable filters with logged events, batch word management, and a toggleable staff bypass.
- [x] **Verification System** — Tracks and logs successful/failed verifications, with captcha or one-click modes and statistics.
- [x] **Lockdown Command** — Overhauled: configurable default channels, current-channel fallback, and automatic restoration of previous permissions.
- [x] **Temporary Voice Channels** — VoiceMaster-style hubs: join to create your own channel, automatic ownership transfer, and deletion when empty.
- [ ] **Music Integration** — Add support for Spotify and YouTube playback.

---

<div align="center">

Made with ❤️ by **duziy**

</div>
