# PyCord-Bot

A simple multipurpose Discord bot coded in Python using [discord.py](https://github.com/Rapptz/discord.py).

> [!NOTE]
> This bot is in active development. Most of the code was put together with the help of AI, StackOverflow, and YouTube tutorials!

---

## 🚀 About & Credits

* **Developer:** Duziy
* **Discord:** `@duziy`
* **Contributions:** Feel free to slide into my DMs with suggestions or submit a pull request on GitHub, and I'll take a look at it when I get the chance.

---

## ⚡ Key Features

* **Modular Cog Architecture:** The codebase is fully organized into Cogs for cleaner code and easier maintenance.
* **Interactive Setup Command:** Includes an easy-to-use setup command to quickly configure the bot for your server.
* **Ticket System:** Built-in support for ticketing workflows with ongoing feature improvements.

---

## 📦 Installation & Setup

Make sure you have **Python 3.15** installed on your system. 

1. Place your `requirements.txt` file in the project folder.
2. Open your terminal in that folder and run:

pip install -r requirements.txt --upgrade

### Troubleshooting Installation

* **If the first command fails, try:**

python -m pip install -r requirements.txt --upgrade

* **Make sure pip is fully updated:**

python -m pip install --upgrade pip

* **Still having issues?** Try completely uninstalling Python and grabbing the newest version directly from the [Python Downloads Page](https://www.python.org/downloads/).

---

## 🐛 Bug Reports

If you find a bug, let me know via Discord (`@duziy`) or open an issue on GitHub. Please note that updates aren't frequent, so a fix might take some time to roll out!

---

## 📋 To-Do List

- [x] **Refactor to Cogs:** Converted the entire codebase over to Cogs for cleaner structure and organization.
- [x] **Setup Command:** Added a quick setup command for server configuration.
- [x] **Ticket System Updates:** Made progress expanding and refining the ticket system.
- [ ] **Logging System:** Basic version is done. Expand it for Log Auto-Moderation, custom Ticket transcript channels, and Verification logs.
- [ ] **Auto-Moderation:** Make logged events fully customizable.
- [ ] **Verification System:** Track and log successful/failed verifications.
- [ ] **Lockdown Command:** Overhaul it so you can select default channels to lockdown (defaults to the current channel if none are selected).
- [ ] **Music Integration:** Add support for Spotify and YouTube playback.
