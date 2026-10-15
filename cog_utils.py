import asyncio
import datetime
import json
import logging
import sqlite3

import aiohttp
import aiosqlite
import discord
from pathlib import Path

log = logging.getLogger("duziy-bot.db")

dir_path = Path(__file__).parent.absolute()
DB_FILE = dir_path / "bot_data.db"
BACKUP_DIR = dir_path / "backups"
BACKUP_KEEP = 7  # rolling number of backups to retain

# --- Shared HTTP session (one TCP/TLS pool instead of one per request) ---
_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _session


async def fetch_json(url, headers=None):
    try:
        request_headers = {'Accept': 'application/json'}
        if headers:
            request_headers.update(headers)
        async with get_session().get(url, headers=request_headers) as response:
            if response.status == 200:
                return await response.json()
            log.warning("Fetch %s returned HTTP %s", url, response.status)
            return None
    except (aiohttp.ClientError, TimeoutError) as e:
        log.warning("Fetch error for %s: %s", url, e)
        return None


# --- Generic settings helper: kills the repeated UPDATE boilerplate ---
COLUMNS = {
    "join_channel", "leave_channel", "join_message", "leave_message",
    "log_channel", "ticket_panel_channel", "transcripts_channel",
    "ticket_support_role", "ticket_category",
    # verification
    "verified_role", "verify_channel", "verification_enabled", "verify_mode",
    # auto-moderation
    "automod_enabled", "automod_caps_percent", "automod_max_mentions",
    "automod_block_invites", "automod_block_links", "automod_blocked_words",
    "automod_staff_bypass",
}


async def set_guild_setting(guild_id: str, column: str, value) -> None:
    if column not in COLUMNS:
        raise ValueError(f"Unknown setting column: {column}")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            f"UPDATE guild_settings SET {column} = ? WHERE guild_id = ?", (value, guild_id)
        )
        await db.commit()


# --- Database Initialization ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # WAL: better concurrent read/write, no "database is locked" errors
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(  # guild_settings
            "CREATE TABLE IF NOT EXISTS guild_settings ("
            "guild_id TEXT PRIMARY KEY, join_channel INTEGER, leave_channel INTEGER, "
            "join_message TEXT DEFAULT 'Welcome {user} to the server! You are the {member_count}th member.', "
            "leave_message TEXT DEFAULT '{user} has left the server', "
            "log_channel INTEGER, ticket_panel_channel INTEGER, transcripts_channel INTEGER, "
            "ticket_support_role INTEGER, ticket_category INTEGER, "
            # verification system
            "verified_role INTEGER, verify_channel INTEGER, verification_enabled INTEGER DEFAULT 0, "
            "verify_mode TEXT DEFAULT 'captcha', "
            # auto-moderation (0 / NULL means a module is off)
            "automod_enabled INTEGER DEFAULT 0, automod_caps_percent INTEGER DEFAULT 0, "
            "automod_max_mentions INTEGER DEFAULT 0, automod_block_invites INTEGER DEFAULT 0, "
            "automod_block_links INTEGER DEFAULT 0, automod_blocked_words TEXT DEFAULT '[]', "
            "automod_staff_bypass INTEGER DEFAULT 1)"
        )
        await db.execute(  # tickets
            "CREATE TABLE IF NOT EXISTS tickets ("
            "channel_id TEXT PRIMARY KEY, guild_id TEXT, user_id TEXT, user_name TEXT, "
            "created_at TEXT, claimed_by TEXT)"
        )
        # Ensure columns exist if upgrading an older database
        await _ensure_columns(db, "tickets", {"claimed_by": "TEXT"})
        await _ensure_columns(db, "guild_settings", {
            "verified_role": "INTEGER", "verify_channel": "INTEGER",
            "verification_enabled": "INTEGER DEFAULT 0",
            "verify_mode": "TEXT DEFAULT 'captcha'",
            "automod_enabled": "INTEGER DEFAULT 0", "automod_caps_percent": "INTEGER DEFAULT 0",
            "automod_max_mentions": "INTEGER DEFAULT 0",
            "automod_block_invites": "INTEGER DEFAULT 0",
            "automod_block_links": "INTEGER DEFAULT 0",
            "automod_blocked_words": "TEXT DEFAULT '[]'",
            "automod_staff_bypass": "INTEGER DEFAULT 1",
        })

        await db.execute(  # ticket_blacklist
            "CREATE TABLE IF NOT EXISTS ticket_blacklist ("
            "guild_id TEXT, user_id TEXT, PRIMARY KEY (guild_id, user_id))"
        )
        await db.execute(  # ticket_claims
            "CREATE TABLE IF NOT EXISTS ticket_claims ("
            "guild_id TEXT, user_id TEXT, claim_count INTEGER DEFAULT 1, "
            "PRIMARY KEY (guild_id, user_id))"
        )
        await db.execute(  # ticket_metrics
            "CREATE TABLE IF NOT EXISTS ticket_metrics ("
            "guild_id TEXT PRIMARY KEY, closed_count INTEGER DEFAULT 0)"
        )
        await db.execute(  # mod_cases
            "CREATE TABLE IF NOT EXISTS mod_cases ("
            "case_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT, action TEXT, "
            "actor_id TEXT, target_id TEXT, reason TEXT, created_at TEXT)"
        )
        await db.execute(  # lockdown default channels (multi-select targets)
            "CREATE TABLE IF NOT EXISTS lockdown_channels ("
            "guild_id TEXT, channel_id TEXT, PRIMARY KEY (guild_id, channel_id))"
        )
        await db.execute(  # active lockdowns, so unlock restores the previous state
            "CREATE TABLE IF NOT EXISTS active_lockdowns ("
            "guild_id TEXT, channel_id TEXT, prev_send INTEGER, locked_by TEXT, "
            "locked_at TEXT, PRIMARY KEY (guild_id, channel_id))"
        )
        await db.execute(  # customizable log events (missing row = enabled)
            "CREATE TABLE IF NOT EXISTS log_settings ("
            "guild_id TEXT, event TEXT, enabled INTEGER DEFAULT 1, "
            "PRIMARY KEY (guild_id, event))"
        )
        await db.execute(  # verification attempts (success/failure tracking)
            "CREATE TABLE IF NOT EXISTS verification_attempts ("
            "guild_id TEXT, user_id TEXT, result TEXT, attempts INTEGER DEFAULT 1, "
            "last_at TEXT, PRIMARY KEY (guild_id, user_id, result))"
        )
        await db.execute(  # auto-mod exempt channels (invites/links allowed there)
            "CREATE TABLE IF NOT EXISTS automod_exempt ("
            "guild_id TEXT, channel_id TEXT, PRIMARY KEY (guild_id, channel_id))"
        )
        await db.execute(  # voice hubs: join these to spawn a temp channel
            "CREATE TABLE IF NOT EXISTS voice_hubs ("
            "guild_id TEXT, channel_id TEXT, name_template TEXT, "
            "PRIMARY KEY (guild_id, channel_id))"
        )
        await db.execute(  # active temp voice channels + their owner
            "CREATE TABLE IF NOT EXISTS voice_temps ("
            "channel_id TEXT PRIMARY KEY, guild_id TEXT, owner_id TEXT, "
            "hub_id TEXT, created_at TEXT)"
        )
        await _ensure_columns(db, "voice_temps", {"panel_message_id": "TEXT"})
        await db.commit()


async def _ensure_columns(db, table: str, columns: dict):
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# --- Event names used by the customizable logging system ---
LOG_EVENTS = {
    "mod": "Moderation & Punishments",
    "automod": "Auto-Moderation",
    "tickets": "Tickets",
    "verification": "Verification",
    "members": "Joins / Leaves / Profile",
    "channels": "Channel Created / Edited / Deleted",
    "roles": "Role Created / Edited / Deleted",
    "messages": "Message Edits / Deletes",
    "server": "Invites & Server Changes",
    "lockdown": "Lockdowns",
    "voice": "Voice Channels",
}

# Message IDs deleted by auto-mod, so the delete-logger doesn't double-log them.
# Capped loosely; old IDs are dropped when the set grows too large.
_automod_deleted: set[int] = set()


def mark_automod_deleted(message_id: int):
    if len(_automod_deleted) > 5000:
        _automod_deleted.clear()
    _automod_deleted.add(message_id)


def consume_automod_deleted(message_id: int) -> bool:
    """True once per ID — lets the message-delete logger skip auto-mod deletions."""
    if message_id in _automod_deleted:
        _automod_deleted.discard(message_id)
        return True
    return False


async def is_log_event_enabled(guild_id: str, event: str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT enabled FROM log_settings WHERE guild_id = ? AND event = ?",
            (guild_id, event),
        ) as cursor:
            row = await cursor.fetchone()
    return row is None or bool(row[0])  # default: enabled


async def set_log_event(guild_id: str, event: str, enabled: bool):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO log_settings (guild_id, event, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, event) DO UPDATE SET enabled = excluded.enabled",
            (guild_id, event, int(enabled)),
        )
        await db.commit()


async def ensure_guild_config(guild_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
            (guild_id,)
        )
        await db.commit()


# --- Typed config row (kills repeated SELECT boilerplate) ---
class TicketConfig:
    def __init__(self, row):
        self.guild_id = row["guild_id"]
        self.join_channel = row["join_channel"]
        self.leave_channel = row["leave_channel"]
        self.join_message = row["join_message"]
        self.leave_message = row["leave_message"]
        self.log_channel = row["log_channel"]
        self.ticket_panel_channel = row["ticket_panel_channel"]
        self.transcripts_channel = row["transcripts_channel"]
        self.ticket_support_role = row["ticket_support_role"]
        self.ticket_category = row["ticket_category"]
        # verification
        self.verified_role = row["verified_role"]
        self.verify_channel = row["verify_channel"]
        self.verification_enabled = bool(row["verification_enabled"])
        self.verify_mode = row["verify_mode"] or "captcha"  # "captcha" or "click"
        # auto-moderation
        self.automod_enabled = bool(row["automod_enabled"])
        self.automod_caps_percent = row["automod_caps_percent"] or 0
        self.automod_max_mentions = row["automod_max_mentions"] or 0
        self.automod_block_invites = bool(row["automod_block_invites"])
        self.automod_block_links = bool(row["automod_block_links"])
        # whether users with manage_messages bypass auto-mod (default: yes)
        self.automod_staff_bypass = bool(row["automod_staff_bypass"]) if row["automod_staff_bypass"] is not None else True
        try:
            self.automod_blocked_words = json.loads(row["automod_blocked_words"] or "[]")
        except (json.JSONDecodeError, TypeError):
            self.automod_blocked_words = []

    @classmethod
    async def fetch(cls, guild_id: str):
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
        if not row:
            await ensure_guild_config(guild_id)
            return await cls.fetch(guild_id)
        return cls(row)

    async def save_automod_words(self, words: list):
        await set_guild_setting(self.guild_id, "automod_blocked_words", json.dumps(words))


# --- Logging ---
async def log_event(guild: discord.Guild, event: str, embed: discord.Embed) -> bool:
    """Send an embed to the guild's log channel, respecting per-event toggles.

    Failures are logged to the console (not silently swallowed) so a
    misconfigured log channel is easy to spot.
    """
    guild_id = str(guild.id)
    if not await is_log_event_enabled(guild_id, event):
        return False
    cfg = await TicketConfig.fetch(guild_id)
    if not cfg.log_channel:
        log.warning("log_event (%s): no log channel configured for guild %s", event, guild_id)
        return False
    channel = guild.get_channel(cfg.log_channel)
    if not channel:
        log.warning("log_event (%s): configured log channel %s no longer exists in guild %s",
                    event, cfg.log_channel, guild_id)
        return False
    try:
        await channel.send(embed=embed)
        return True
    except discord.Forbidden:
        log.warning("log_event (%s): missing permissions in log channel %s (guild %s) — "
                    "the bot needs Send Messages and Embed Links",
                    event, channel.id, guild_id)
        return False
    except discord.HTTPException as e:
        log.warning("log_event (%s): failed to send to log channel %s (guild %s): %s",
                    event, channel.id, guild_id, e)
        return False


async def log_action(guild: discord.Guild, embed: discord.Embed) -> bool:
    """Backward-compatible wrapper: moderation-flavored events."""
    return await log_event(guild, "mod", embed)


# --- Mod cases ---
async def log_case(guild_id: str, action: str, actor_id: str, target_id: str, reason: str | None) -> int:
    """Record a moderation action. Returns the case number."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "INSERT INTO mod_cases (guild_id, action, actor_id, target_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, action, actor_id, target_id, reason, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_recent_cases(guild_id: str, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mod_cases WHERE guild_id = ? ORDER BY case_id DESC LIMIT ?",
            (guild_id, limit),
        ) as cursor:
            return await cursor.fetchall()


# --- Blacklist / stats helpers ---
async def is_blacklisted(guild_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT 1 FROM ticket_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def get_blacklisted_users(guild_id: str) -> list[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id FROM ticket_blacklist WHERE guild_id = ? ORDER BY user_id",
            (guild_id,)
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def set_blacklisted(guild_id: str, user_id: str, blocked: bool):
    async with aiosqlite.connect(DB_FILE) as db:
        if blocked:
            await db.execute(
                "INSERT OR IGNORE INTO ticket_blacklist (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id)
            )
        else:
            await db.execute(
                "DELETE FROM ticket_blacklist WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
        await db.commit()


async def increment_claim(guild_id: str, user_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO ticket_claims (guild_id, user_id, claim_count) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET claim_count = claim_count + 1",
            (guild_id, user_id)
        )
        await db.commit()


async def increment_closed(guild_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO ticket_metrics (guild_id, closed_count) VALUES (?, 0)", (guild_id,)
        )
        await db.execute(
            "UPDATE ticket_metrics SET closed_count = closed_count + 1 WHERE guild_id = ?", (guild_id,)
        )
        await db.commit()


# --- Lockdown helpers ---
async def get_lockdown_channels(guild_id: str) -> list[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT channel_id FROM lockdown_channels WHERE guild_id = ? ORDER BY channel_id",
            (guild_id,)
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def add_lockdown_channel(guild_id: str, channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO lockdown_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        await db.commit()


async def remove_lockdown_channel(guild_id: str, channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM lockdown_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        await db.commit()


async def clear_lockdown_channels(guild_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM lockdown_channels WHERE guild_id = ?", (guild_id,))
        await db.commit()


async def record_lockdown(guild_id: str, channel_id: str, prev_send, locked_by: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO active_lockdowns "
            "(guild_id, channel_id, prev_send, locked_by, locked_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, channel_id, prev_send, locked_by,
             datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
        await db.commit()


async def get_active_lockdown(guild_id: str, channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT prev_send, locked_by, locked_at FROM active_lockdowns "
            "WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ) as cursor:
            return await cursor.fetchone()


async def clear_active_lockdown(guild_id: str, channel_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM active_lockdowns WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_all_active_lockdowns(guild_id: str) -> list[str]:
    """All channel IDs currently under an active lockdown in this guild."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT channel_id FROM active_lockdowns WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


# --- Auto-mod exemptions ---
async def get_automod_exempt(guild_id: str) -> list[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT channel_id FROM automod_exempt WHERE guild_id = ? ORDER BY channel_id",
            (guild_id,)
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def set_automod_exempt(guild_id: str, channel_id: str, exempt: bool):
    async with aiosqlite.connect(DB_FILE) as db:
        if exempt:
            await db.execute(
                "INSERT OR IGNORE INTO automod_exempt (guild_id, channel_id) VALUES (?, ?)",
                (guild_id, channel_id))
        else:
            await db.execute(
                "DELETE FROM automod_exempt WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id))
        await db.commit()


# --- Database backups ---
async def backup_database() -> Path | None:
    """Snapshot bot_data.db using SQLite's online backup API (safe while running).

    Keeps the newest BACKUP_KEEP snapshots and prunes the rest.
    Returns the backup path, or None on failure.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"bot_data_{stamp}.db"

    def _copy():
        source = sqlite3.connect(DB_FILE)
        try:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    try:
        await asyncio.to_thread(_copy)
    except sqlite3.Error as e:
        log.error("Database backup failed: %s", e)
        return None

    # Prune old backups, keep the newest BACKUP_KEEP
    backups = sorted(BACKUP_DIR.glob("bot_data_*.db"), reverse=True)
    for old in backups[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


# --- Stale reference cleanup ---
# Channel/role columns in guild_settings that get nulled out when the target is gone
_SETTINGS_REFS = {
    "join_channel", "leave_channel", "log_channel", "ticket_panel_channel",
    "transcripts_channel", "verify_channel", "ticket_category",
}
_SETTINGS_ROLE_REFS = {"ticket_support_role", "verified_role"}


async def cleanup_stale_entries(bot) -> dict:
    """Remove DB rows/refs pointing at channels, roles or guilds that no longer exist.

    Runs on startup and periodically. Returns {table: rows_removed} for reporting.
    """
    removed = {}

    def bump(table, n):
        if n:
            removed[table] = removed.get(table, 0) + n

    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row

        # --- Tables keyed by channel_id ---
        for table in ("voice_temps", "active_lockdowns"):
            async with db.execute(f"SELECT channel_id, guild_id FROM {table}") as cur:
                rows = await cur.fetchall()
            for row in rows:
                guild = bot.get_guild(int(row["guild_id"]))
                if guild is None or guild.get_channel(int(row["channel_id"])) is None:
                    await db.execute(f"DELETE FROM {table} WHERE channel_id = ?",
                                     (row["channel_id"],))
                    bump(table, 1)

        # --- Tables keyed by (guild_id, channel_id) ---
        for table in ("voice_hubs", "lockdown_channels"):
            async with db.execute(f"SELECT guild_id, channel_id FROM {table}") as cur:
                rows = await cur.fetchall()
            for row in rows:
                guild = bot.get_guild(int(row["guild_id"]))
                if guild is None or guild.get_channel(int(row["channel_id"])) is None:
                    await db.execute(
                        f"DELETE FROM {table} WHERE guild_id = ? AND channel_id = ?",
                        (row["guild_id"], row["channel_id"]))
                    bump(table, 1)

        # --- Tickets whose channel is gone ---
        async with db.execute("SELECT channel_id, guild_id FROM tickets") as cur:
            rows = await cur.fetchall()
        for row in rows:
            guild = bot.get_guild(int(row["guild_id"]))
            if guild is None or guild.get_channel(int(row["channel_id"])) is None:
                await db.execute("DELETE FROM tickets WHERE channel_id = ?",
                                 (row["channel_id"],))
                bump("tickets", 1)

        # --- guild_settings pointing at deleted channels/roles/categories ---
        async with db.execute("SELECT * FROM guild_settings") as cur:
            rows = await cur.fetchall()
        for row in rows:
            guild = bot.get_guild(int(row["guild_id"]))
            if guild is None:
                continue  # bot was removed from the guild; leave the row
            for column in _SETTINGS_REFS:
                value = row[column]
                if value and guild.get_channel(value) is None:
                    await db.execute(
                        f"UPDATE guild_settings SET {column} = NULL "
                        f"WHERE guild_id = ?", (row["guild_id"],))
                    bump("guild_settings", 1)
            for column in _SETTINGS_ROLE_REFS:
                value = row[column]
                if value and guild.get_role(value) is None:
                    await db.execute(
                        f"UPDATE guild_settings SET {column} = NULL "
                        f"WHERE guild_id = ?", (row["guild_id"],))
                    bump("guild_settings", 1)

        await db.commit()
    return removed


# --- Verification tracking ---
async def record_verification_attempt(guild_id: str, user_id: str, success: bool):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = "success" if success else "failure"
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO verification_attempts (guild_id, user_id, result, attempts, last_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(guild_id, user_id, result) DO UPDATE SET "
            "attempts = attempts + 1, last_at = excluded.last_at",
            (guild_id, user_id, result, now),
        )
        await db.commit()


async def get_verification_stats(guild_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT result, SUM(attempts) FROM verification_attempts "
            "WHERE guild_id = ? GROUP BY result",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    stats = {"success": 0, "failure": 0}
    for result, count in rows:
        if result in stats:
            stats[result] = count
    return stats