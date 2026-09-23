"""Plugin-owned SQLite store.

Holds what Evonic has no table for: per-agent metadata from the console,
token limits, pairing codes, linked WhatsApp numbers and the callback outbox.
Agents, channels and token usage stay where they already live.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .common import PLUGIN_ID, iso, now


def _db_path() -> str:
    """Next to the other plugin databases (Evonic's PLUGIN_DB_DIR)."""
    override = os.environ.get("TIYASANCLOUD_PLUGIN_DB")
    if override:
        return override
    try:
        from backend.plugin_sdk import PLUGIN_DB_DIR
    except Exception:  # pragma: no cover - only outside Evonic
        from config import BASE_DIR
        PLUGIN_DB_DIR = os.path.join(BASE_DIR, "data", "db", "plugins")
    return os.path.join(PLUGIN_DB_DIR, f"{PLUGIN_ID}.db")


_lock = threading.Lock()
# Which database file the schema was created in (the path can change in tests).
_ready_path: Optional[str] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_meta (
    agent_id TEXT PRIMARY KEY,
    locale TEXT NOT NULL DEFAULT 'en',
    skillset TEXT,
    account TEXT,
    instance TEXT,
    profile TEXT,
    owner TEXT,
    callback_url TEXT,
    hosting_hash TEXT,
    profile_hash TEXT,
    -- 1 = registered, waiting for its connector; the worker enables it once.
    activate_pending INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS limits (
    agent_id TEXT PRIMARY KEY,
    tokens_monthly INTEGER,
    soft_pct INTEGER NOT NULL DEFAULT 80,
    hard INTEGER NOT NULL DEFAULT 1,
    suspended INTEGER NOT NULL DEFAULT 0,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    rollup_subagents INTEGER NOT NULL DEFAULT 1,
    messages TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pair_codes (
    code TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    -- The shared channel whose number the link points at.
    channel_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_pair_codes_agent ON pair_codes(agent_id);

CREATE TABLE IF NOT EXISTS linked_users (
    phone TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    name TEXT,
    -- WhatsApp LID of the chat, when it is not addressed by phone number.
    alias TEXT,
    linked_at TEXT NOT NULL
);

-- Last "how to connect" pointer sent to an unknown sender.
CREATE TABLE IF NOT EXISTS hints (
    sender TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linked_agent ON linked_users(agent_id);

-- One callback per state change per period (spec section 4, rule 5).
CREATE TABLE IF NOT EXISTS quota_notices (
    agent_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    state TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (agent_id, period_start, state)
);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    url TEXT NOT NULL,
    body TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Failed pairing attempts, for the per-sender cool-off.
CREATE TABLE IF NOT EXISTS pair_failures (
    phone TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    first_at TEXT NOT NULL,
    blocked_until TEXT
);
"""


@contextmanager
def connect():
    global _ready_path
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with _lock:
            if _ready_path != path:
                conn.executescript("PRAGMA journal_mode=WAL;\n" + SCHEMA)
                _migrate(conn)
                conn.commit()
                _ready_path = path
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a database was first created."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_meta)")}
    if "activate_pending" not in columns:
        conn.execute("ALTER TABLE agent_meta ADD COLUMN activate_pending INTEGER NOT NULL DEFAULT 0")


def _loads(value: Optional[str], fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except ValueError:
        return fallback


# ---------------------------------------------------------------- agent meta

def save_meta(agent_id: str, **fields: Any) -> None:
    """Upsert the console-owned metadata of an agent."""
    columns = ("locale", "skillset", "account", "instance", "profile", "owner",
               "callback_url", "hosting_hash", "profile_hash", "activate_pending")
    values = {}
    for key in columns:
        if key in fields:
            value = fields[key]
            values[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
    stamp = iso(now())
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM agent_meta WHERE agent_id = ?", (agent_id,)).fetchone()
        if exists:
            if values:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(f"UPDATE agent_meta SET {sets}, updated_at = ? WHERE agent_id = ?",
                             (*values.values(), stamp, agent_id))
        else:
            keys = list(values)
            conn.execute(
                f"INSERT INTO agent_meta (agent_id, created_at, updated_at{''.join(', ' + k for k in keys)}) "
                f"VALUES (?, ?, ?{', ?' * len(keys)})",
                (agent_id, stamp, stamp, *values.values()),
            )
        conn.commit()


def get_meta(agent_id: str) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent_meta WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        return {}
    meta = dict(row)
    for key in ("account", "instance", "profile", "owner"):
        meta[key] = _loads(meta.get(key), None)
    return meta


def pending_activation() -> List[str]:
    with connect() as conn:
        rows = conn.execute("SELECT agent_id FROM agent_meta WHERE activate_pending = 1").fetchall()
    return [row["agent_id"] for row in rows]


# -------------------------------------------------------------------- limits

def save_limits(agent_id: str, data: Dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO limits (agent_id, tokens_monthly, soft_pct, hard, suspended,
                                   period_start, period_end, rollup_subagents, messages, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                   tokens_monthly = excluded.tokens_monthly,
                   soft_pct = excluded.soft_pct,
                   hard = excluded.hard,
                   suspended = excluded.suspended,
                   period_start = excluded.period_start,
                   period_end = excluded.period_end,
                   rollup_subagents = excluded.rollup_subagents,
                   messages = excluded.messages,
                   updated_at = excluded.updated_at""",
            (agent_id, data["tokens_monthly"], data["soft_pct"], 1 if data["hard"] else 0,
             1 if data["suspended"] else 0, data["period_start"], data["period_end"],
             1 if data["rollup_subagents"] else 0, json.dumps(data.get("messages") or {}), iso(now())),
        )
        conn.commit()


def get_limits(agent_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM limits WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    limits = dict(row)
    limits["hard"] = bool(limits["hard"])
    limits["suspended"] = bool(limits["suspended"])
    limits["rollup_subagents"] = bool(limits["rollup_subagents"])
    limits["messages"] = _loads(limits.get("messages"), {}) or {}
    return limits


# --------------------------------------------------------------- pair codes

def add_pair_code(code: str, agent_id: str, channel_id: str, expires_at: str) -> None:
    with connect() as conn:
        # One live code per agent: the previous one expires right away. The row
        # stays so the hourly rate limit still counts it.
        conn.execute("UPDATE pair_codes SET expires_at = ? WHERE agent_id = ? AND used_at IS NULL "
                     "AND expires_at > ?", (iso(now()), agent_id, iso(now())))
        conn.execute(
            "INSERT INTO pair_codes (code, agent_id, channel_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (code, agent_id, channel_id, iso(now()), expires_at),
        )
        conn.commit()


def get_pair_code(code: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pair_codes WHERE code = ?", (code.upper(),)).fetchone()
    return dict(row) if row else None


def pending_code(agent_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pair_codes WHERE agent_id = ? AND used_at IS NULL AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id, iso(now())),
        ).fetchone()
    return dict(row) if row else None


def codes_issued_since(agent_id: str, since_iso: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pair_codes WHERE agent_id = ? AND created_at >= ?",
            (agent_id, since_iso),
        ).fetchone()
    return int(row["n"]) if row else 0


def mark_code_used(code: str, phone: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE pair_codes SET used_at = ?, used_by = ? WHERE code = ?",
                     (iso(now()), phone, code.upper()))
        conn.commit()


def purge_expired_codes() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pair_codes WHERE used_at IS NOT NULL AND used_at < datetime('now', '-30 days')")
        conn.commit()


# ------------------------------------------------------------- linked users

def add_link(phone: str, agent_id: str, channel_id: str, name: Optional[str],
             alias: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO linked_users (phone, agent_id, channel_id, name, alias, linked_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET agent_id = excluded.agent_id, channel_id = excluded.channel_id, "
            "name = excluded.name, alias = excluded.alias, linked_at = excluded.linked_at",
            (phone, agent_id, channel_id, name, alias, iso(now())),
        )
        conn.commit()


def get_link(phone: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM linked_users WHERE phone = ?", (phone,)).fetchone()
    return dict(row) if row else None


def hint_due(sender: str, every_hours: int) -> bool:
    """True (and recorded) when this sender has not had a pointer recently."""
    from datetime import timedelta
    cutoff = iso(now() - timedelta(hours=every_hours))
    with connect() as conn:
        row = conn.execute("SELECT at FROM hints WHERE sender = ?", (sender,)).fetchone()
        if row and row["at"] > cutoff:
            return False
        conn.execute("INSERT INTO hints (sender, at) VALUES (?, ?) "
                     "ON CONFLICT(sender) DO UPDATE SET at = excluded.at", (sender, iso(now())))
        conn.commit()
    return True


def remove_link(phone: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM linked_users WHERE phone = ?", (phone,))
        conn.commit()


def links_of(agent_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT phone, channel_id, name, alias, linked_at FROM linked_users WHERE agent_id = ? ORDER BY linked_at",
            (agent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def links_of_all() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT phone, agent_id, channel_id, name, linked_at FROM linked_users").fetchall()
    return [dict(r) for r in rows]


def link_owner(phone: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT agent_id FROM linked_users WHERE phone = ?", (phone,)).fetchone()
    return row["agent_id"] if row else None


# ------------------------------------------------------------ quota notices

def notice_seen(agent_id: str, period_start: str, state: str) -> bool:
    """True when this state was already reported for this period (and marks it)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM quota_notices WHERE agent_id = ? AND period_start = ? AND state = ?",
            (agent_id, period_start, state),
        ).fetchone()
        if row:
            return True
        conn.execute("INSERT INTO quota_notices (agent_id, period_start, state, at) VALUES (?, ?, ?, ?)",
                     (agent_id, period_start, state, iso(now())))
        conn.commit()
    return False


# -------------------------------------------------------------------- outbox

def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:24]}"


def queue_callback(event_id: str, agent_id: str, url: str, body: str) -> str:
    with connect() as conn:
        conn.execute(
            "INSERT INTO outbox (id, agent_id, url, body, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, agent_id, url, body, iso(now()), iso(now())),
        )
        conn.commit()
    return event_id


def due_callbacks(limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE next_attempt_at <= ? ORDER BY created_at LIMIT ?",
            (iso(now()), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def drop_callback(event_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM outbox WHERE id = ?", (event_id,))
        conn.commit()


def defer_callback(event_id: str, attempts: int, next_at: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE outbox SET attempts = ?, next_attempt_at = ? WHERE id = ?",
                     (attempts, next_at, event_id))
        conn.commit()


# ------------------------------------------------------------ pair failures

def record_failure(phone: str, max_tries: int, window_minutes: int, block_minutes: int) -> bool:
    """Count a wrong code. Returns True when the sender is now in cool-off."""
    from datetime import timedelta
    current = now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM pair_failures WHERE phone = ?", (phone,)).fetchone()
        if not row:
            conn.execute("INSERT INTO pair_failures (phone, count, first_at) VALUES (?, 1, ?)",
                         (phone, iso(current)))
            conn.commit()
            return False
        first_at = row["first_at"]
        count = int(row["count"]) + 1
        window_start = iso(current - timedelta(minutes=window_minutes))
        if first_at < window_start:
            conn.execute("UPDATE pair_failures SET count = 1, first_at = ?, blocked_until = NULL WHERE phone = ?",
                         (iso(current), phone))
            conn.commit()
            return False
        blocked_until = iso(current + timedelta(minutes=block_minutes)) if count >= max_tries else row["blocked_until"]
        conn.execute("UPDATE pair_failures SET count = ?, blocked_until = ? WHERE phone = ?",
                     (count, blocked_until, phone))
        conn.commit()
    return bool(blocked_until and blocked_until > iso(current))


def is_blocked(phone: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT blocked_until FROM pair_failures WHERE phone = ?", (phone,)).fetchone()
    return bool(row and row["blocked_until"] and row["blocked_until"] > iso(now()))


def clear_failures(phone: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pair_failures WHERE phone = ?", (phone,))
        conn.commit()
