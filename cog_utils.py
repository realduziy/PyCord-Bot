import datetime
import logging

import aiohttp
import aiosqlite
import discord
from pathlib import Path

log = logging.getLogger("duziy-bot.db")

dir_path = Path(__file__).parent.absolute()
DB_FILE = dir_path / "bot_data.db"

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
            "ticket_support_role INTEGER, ticket_category INTEGER)"
        )
        await db.execute(  # tickets
            "CREATE TABLE IF NOT EXISTS tickets ("
            "channel_id TEXT PRIMARY KEY, guild_id TEXT, user_id TEXT, user_name TEXT, "
            "created_at TEXT, claimed_by TEXT)"
        )
        # Ensure 'claimed_by' column exists if upgrading an older database
        async with db.execute("PRAGMA table_info(tickets)") as cur:
            ticket_columns = [row[1] for row in await cur.fetchall()]
        if "claimed_by" not in ticket_columns:
            await db.execute("ALTER TABLE tickets ADD COLUMN claimed_by TEXT")

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


# --- Mod log ---
async def log_action(guild: discord.Guild, embed: discord.Embed) -> bool:
    """Send an embed to the guild's configured log channel. Returns True if sent."""
    cfg = await TicketConfig.fetch(str(guild.id))
    if not cfg.log_channel:
        return False
    channel = guild.get_channel(cfg.log_channel)
    if not channel:
        return False
    try:
        await channel.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


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
