import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Generator

AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'agents')

# Non-human session identifiers (agent-to-agent, scheduler, system notifications).
_SYSTEM_EXTERNAL_USER_IDS = frozenset({'__scheduler__'})
_SYSTEM_EXTERNAL_USER_PREFIXES = ('__agent__', '__system__')


def is_human_facing_external_user_id(external_user_id: str) -> bool:
    """Return True when external_user_id denotes a human user session.

    Human sessions include web UI chats and channel users (Telegram, WhatsApp, etc.).
    Excludes agent-to-agent, scheduler, and system notification sessions.
    """
    euid = (external_user_id or '').strip()
    if not euid:
        return False
    if euid in _SYSTEM_EXTERNAL_USER_IDS:
        return False
    return not any(euid.startswith(prefix) for prefix in _SYSTEM_EXTERNAL_USER_PREFIXES)


def _migrate_session_id(cursor, old_id: str, new_id: str) -> None:
    """Rename a session ID and update all referencing tables."""
    cursor.execute("UPDATE chat_sessions SET id = ? WHERE id = ?", (new_id, old_id))
    cursor.execute("UPDATE chat_messages SET session_id = ? WHERE session_id = ?", (new_id, old_id))
    cursor.execute("UPDATE chat_summaries SET session_id = ? WHERE session_id = ?", (new_id, old_id))
    cursor.execute("UPDATE agent_state SET session_id = ? WHERE session_id = ?", (new_id, old_id))


class AgentChatDB:
    """SQLite database per agent for chat sessions and messages."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        agent_dir = os.path.join(AGENTS_DIR, agent_id)
        os.makedirs(agent_dir, exist_ok=True)
        self.db_path = os.path.join(agent_dir, 'chat.db')
        self._init_tables()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that returns a SQLite connection for this agent's database.
        The connection is opened on entry and closed on exit to prevent file descriptor leaks.
        Includes automatic transaction management (commit/rollback).
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_tables(self):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    channel_id TEXT,
                    external_user_id TEXT NOT NULL,
                    bot_enabled BOOLEAN DEFAULT 1,
                    archived BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
            """)
            # Migration: add metadata column if missing
            try:
                cursor.execute("ALTER TABLE chat_messages ADD COLUMN metadata TEXT")
            except sqlite3.OperationalError:
                pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    summary TEXT NOT NULL,
                    last_message_id INTEGER NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_lookup ON chat_sessions(agent_id, channel_id, external_user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id)")
            # Migration: add last_message_ts column to chat_summaries (JSONL watermark)
            try:
                cursor.execute("ALTER TABLE chat_summaries ADD COLUMN last_message_ts INTEGER")
            except sqlite3.OperationalError:
                pass
            # Migration: add archived column if missing
            try:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN archived BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_state (
                    session_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
            """)
            # Long-term memory table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    source_session_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expired INTEGER DEFAULT 0
                )
            """)
            # FTS5 virtual table for BM25 keyword search over memories
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    category,
                    content='memories',
                    content_rowid='id'
                )
            """)
            # Triggers to keep FTS5 index in sync with memories table
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, category)
                    VALUES (new.id, new.content, new.category);
                END
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, category)
                    VALUES ('delete', old.id, old.content, old.category);
                END
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, category)
                    VALUES ('delete', old.id, old.content, old.category);
                    INSERT INTO memories_fts(rowid, content, category)
                    VALUES (new.id, new.content, new.category);
                END
            """)
            conn.commit()

    def get_or_create_session(self, agent_id: str, external_user_id: str,
                               channel_id: str = None,
                               channel_type: str = None) -> str:
        from models.chatlog import session_slug
        slug = f"{agent_id}-{session_slug(external_user_id, agent_id=agent_id)}"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if channel_id:
                cursor.execute("""
                    SELECT id FROM chat_sessions
                    WHERE agent_id = ? AND channel_id = ? AND external_user_id = ?
                    AND (archived IS NULL OR archived = 0)
                """, (agent_id, channel_id, external_user_id))
            else:
                cursor.execute("""
                    SELECT id FROM chat_sessions
                    WHERE agent_id = ? AND channel_id IS NULL AND external_user_id = ?
                    AND (archived IS NULL OR archived = 0)
                """, (agent_id, external_user_id))
            row = cursor.fetchone()
            if row:
                old_id = row['id']
                if old_id != slug:
                    _migrate_session_id(cursor, old_id, slug)
                    conn.commit()
                    return slug
                cursor.execute(
                    "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row['id'],))
                conn.commit()
                return row['id']
            # No active session found — check for archived session to reuse
            if channel_id:
                cursor.execute("""
                    SELECT id FROM chat_sessions
                    WHERE agent_id = ? AND channel_id = ? AND external_user_id = ?
                    AND archived = 1
                """, (agent_id, channel_id, external_user_id))
            else:
                cursor.execute("""
                    SELECT id FROM chat_sessions
                    WHERE agent_id = ? AND channel_id IS NULL AND external_user_id = ?
                    AND archived = 1
                """, (agent_id, external_user_id))
            archived_row = cursor.fetchone()
            if archived_row:
                old_id = archived_row['id']
                if old_id != slug:
                    _migrate_session_id(cursor, old_id, slug)
                    conn.commit()
                    return slug
                cursor.execute(
                    "UPDATE chat_sessions SET archived = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (archived_row['id'],))
                conn.commit()
                return archived_row['id']
            try:
                cursor.execute("""
                    INSERT INTO chat_sessions (id, agent_id, channel_id, external_user_id)
                    VALUES (?, ?, ?, ?)
                """, (slug, agent_id, channel_id, external_user_id))
                conn.commit()
                return slug
            except sqlite3.IntegrityError:
                # Race condition: another request inserted the same slug concurrently
                if channel_id:
                    cursor.execute("""
                        SELECT id FROM chat_sessions
                        WHERE agent_id = ? AND channel_id = ? AND external_user_id = ?
                    """, (agent_id, channel_id, external_user_id))
                else:
                    cursor.execute("""
                        SELECT id FROM chat_sessions
                        WHERE agent_id = ? AND channel_id IS NULL AND external_user_id = ?
                    """, (agent_id, external_user_id))
                row = cursor.fetchone()
                old_id = row['id']
                if old_id != slug:
                    _migrate_session_id(cursor, old_id, slug)
                    conn.commit()
                return slug

    def get_session_messages(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM chat_messages WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, (session_id, limit))
            rows = [dict(r) for r in cursor.fetchall()]
            rows.reverse()
            for r in rows:
                if r.get('tool_calls'):
                    try:
                        r['tool_calls'] = json.loads(r['tool_calls'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if r.get('metadata'):
                    try:
                        r['metadata'] = json.loads(r['metadata'])
                    except (json.JSONDecodeError, TypeError):
                        r['metadata'] = None
            return rows

    def get_first_agent_request_metadata(self, session_id: str) -> dict | None:
        """Return metadata of the first user message with agent_message=true in the session.

        Used by auto-forward to locate report_to_id even when the originating
        message falls outside the recent-message window.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT metadata FROM chat_messages
                WHERE session_id = ? AND role = 'user' AND metadata LIKE '%"agent_message"%'
                ORDER BY created_at ASC LIMIT 1
            """, (session_id,))
            row = cursor.fetchone()
            if not row or not row['metadata']:
                return None
            try:
                return json.loads(row['metadata'])
            except (json.JSONDecodeError, TypeError):
                return None

    def add_chat_message(self, session_id: str, role: str, content: str = None,
                          tool_calls=None, tool_call_id: str = None,
                          metadata: dict = None) -> int:
        tc_json = json.dumps(tool_calls) if tool_calls else None
        meta_json = json.dumps(metadata) if metadata else None
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_messages (session_id, role, content, tool_calls, tool_call_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, role, content, tc_json, tool_call_id, meta_json))
            # Un-archive the session so it reappears in the session list
            cursor.execute(
                "UPDATE chat_sessions SET archived = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,))
            conn.commit()
            return cursor.lastrowid

    # Fixed key for per-agent global state — one row per agent DB, not per session.
    _AGENT_STATE_KEY = '__agent__'

    def upsert_agent_state(self, content: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_state (session_id, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (self._AGENT_STATE_KEY, content))
            conn.commit()

    def get_agent_state(self) -> str | None:
        with self._connect() as conn:
            # Try global key first
            row = conn.execute(
                "SELECT content FROM agent_state WHERE session_id = ?",
                (self._AGENT_STATE_KEY,)).fetchone()
            if row:
                return row[0]
            # One-time migration: promote the latest per-session state to global key
            row = conn.execute(
                "SELECT content FROM agent_state WHERE session_id != ? ORDER BY updated_at DESC LIMIT 1",
                (self._AGENT_STATE_KEY,)).fetchone()
            if row:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_state (session_id, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (self._AGENT_STATE_KEY, row[0]))
                conn.commit()
                return row[0]
        return None

    def clear_session(self, session_id: str):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM chat_summaries WHERE session_id = ?", (session_id,))
            # Do NOT delete agent_state — it is global per-agent, not per-session
            conn.commit()

    def clear_all(self):
        """Delete all sessions, messages, and summaries in this agent's chat DB."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages")
            cursor.execute("DELETE FROM chat_summaries")
            cursor.execute("DELETE FROM chat_sessions")
            conn.commit()

    # ---- Summarization ----

    def get_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM chat_summaries WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def upsert_summary(self, session_id: str, summary: str,
                        last_message_id: int, message_count: int,
                        last_message_ts: int = None):
        with self._connect() as conn:
            conn.cursor().execute("""
                INSERT INTO chat_summaries (session_id, summary, last_message_id, message_count, last_message_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    message_count = excluded.message_count,
                    last_message_ts = excluded.last_message_ts,
                    updated_at = CURRENT_TIMESTAMP
            """, (session_id, summary, last_message_id, message_count, last_message_ts))
            conn.commit()

    def get_agent_summaries(self, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """List all session summaries for this agent with optional keyword filter.

        Returns list of dicts containing session metadata and summary text.
        Filtered to non-archived sessions only, sorted by most recently updated.

        Args:
            query: Optional keyword filter (searches summary text via LIKE).
                   Empty string returns all sessions.
            limit: Maximum number of results (max 50).
        """
        limit = min(limit, 50)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if query:
                like_pattern = f"%{query}%"
                cursor.execute("""
                    SELECT
                        cs.id AS session_id,
                        cs.channel_id,
                        cs.external_user_id,
                        cs.created_at,
                        cs.updated_at,
                        COALESCE(csm.summary, '') AS summary,
                        COALESCE(csm.message_count, 0) AS message_count
                    FROM chat_sessions cs
                    LEFT JOIN chat_summaries csm ON cs.id = csm.session_id
                    WHERE cs.agent_id = ?
                      AND (csm.summary IS NOT NULL AND csm.summary != '')
                      AND csm.summary LIKE ?
                      AND (cs.archived IS NULL OR cs.archived = 0)
                    ORDER BY cs.updated_at DESC
                    LIMIT ?
                """, (self.agent_id, like_pattern, limit))
            else:
                cursor.execute("""
                    SELECT
                        cs.id AS session_id,
                        cs.channel_id,
                        cs.external_user_id,
                        cs.created_at,
                        cs.updated_at,
                        COALESCE(csm.summary, '') AS summary,
                        COALESCE(csm.message_count, 0) AS message_count
                    FROM chat_sessions cs
                    LEFT JOIN chat_summaries csm ON cs.id = csm.session_id
                    WHERE cs.agent_id = ?
                      AND (csm.summary IS NOT NULL AND csm.summary != '')
                      AND (cs.archived IS NULL OR cs.archived = 0)
                    ORDER BY cs.updated_at DESC
                    LIMIT ?
                """, (self.agent_id, limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_messages_after(self, session_id: str, after_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM chat_messages WHERE session_id = ? AND id > ?
                ORDER BY created_at ASC
            """, (session_id, after_id))
            rows = [dict(r) for r in cursor.fetchall()]
            for r in rows:
                if r.get('tool_calls'):
                    try:
                        r['tool_calls'] = json.loads(r['tool_calls'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if r.get('metadata'):
                    try:
                        r['metadata'] = json.loads(r['metadata'])
                    except (json.JSONDecodeError, TypeError):
                        r['metadata'] = None
            return rows

    def get_messages_between(self, session_id: str, after_id: int,
                              up_to_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM chat_messages WHERE session_id = ? AND id > ? AND id <= ?
                ORDER BY created_at ASC
            """, (session_id, after_id, up_to_id))
            rows = [dict(r) for r in cursor.fetchall()]
            for r in rows:
                if r.get('tool_calls'):
                    try:
                        r['tool_calls'] = json.loads(r['tool_calls'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if r.get('metadata'):
                    try:
                        r['metadata'] = json.loads(r['metadata'])
                    except (json.JSONDecodeError, TypeError):
                        r['metadata'] = None
            return rows

    def get_message_count(self, session_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,))
            return cursor.fetchone()[0]

    def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM chat_sessions WHERE id = ? AND (archived IS NULL OR archived = 0)",
                (session_id,))
            if not cursor.fetchone():
                return False
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM chat_summaries WHERE session_id = ?", (session_id,))
            cursor.execute(
                "UPDATE chat_sessions SET archived = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,))
            conn.commit()
            return True

    def archive_sessions_by_agent_id(self, agent_id: str) -> int:
        """Archive all non-archived sessions whose agent_id matches.

        Used to clean up sub-agent sessions when the sub-agent is destroyed.
        Returns the number of sessions archived.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM chat_sessions WHERE agent_id = ? AND (archived IS NULL OR archived = 0)",
                (agent_id,))
            session_ids = [row[0] for row in cursor.fetchall()]
            for sid in session_ids:
                cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (sid,))
                cursor.execute("DELETE FROM chat_summaries WHERE session_id = ?", (sid,))
                cursor.execute(
                    "UPDATE chat_sessions SET archived = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (sid,))
            conn.commit()
            return len(session_ids)

    def has_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM chat_sessions WHERE id = ? AND (archived IS NULL OR archived = 0)",
                (session_id,))
            return cursor.fetchone() is not None

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_session_messages_full(self, session_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, role, content, metadata, created_at FROM chat_messages
                WHERE session_id = ? AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' AND tool_calls IS NULL
                ORDER BY id ASC
            """, (session_id,))
            rows = [dict(r) for r in cursor.fetchall()]
            for r in rows:
                if r.get('metadata'):
                    try:
                        r['metadata'] = json.loads(r['metadata'])
                    except (json.JSONDecodeError, TypeError):
                        r['metadata'] = None
            return rows

    def get_new_messages(self, session_id: str, after_id: int) -> List[Dict[str, Any]]:
        """Get messages with id > after_id for real-time polling."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, role, content, metadata, created_at FROM chat_messages
                WHERE session_id = ? AND id > ? AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' AND tool_calls IS NULL
                ORDER BY id ASC
            """, (session_id, after_id))
            rows = [dict(r) for r in cursor.fetchall()]
            for r in rows:
                if r.get('metadata'):
                    try:
                        r['metadata'] = json.loads(r['metadata'])
                    except (json.JSONDecodeError, TypeError):
                        r['metadata'] = None
            return rows

    def get_last_assistant_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent assistant message in a session, or None."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, role, content, metadata, created_at FROM chat_messages
                WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL AND content != ''
                AND tool_calls IS NULL
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            if result.get('metadata'):
                try:
                    result['metadata'] = json.loads(result['metadata'])
                except (json.JSONDecodeError, TypeError):
                    result['metadata'] = None
            return result

    def set_session_bot_enabled(self, session_id: str, enabled: bool):
        with self._connect() as conn:
            conn.cursor().execute(
                "UPDATE chat_sessions SET bot_enabled = ? WHERE id = ?",
                (1 if enabled else 0, session_id))
            conn.commit()

    def is_session_bot_enabled(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT bot_enabled FROM chat_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            return bool(row[0]) if row else True

    def get_latest_human_session(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent non-archived session belonging to a human user.

        Priority:
          1. Sessions with a real channel (channel_id IS NOT NULL) — Telegram, etc.
          2. Fallback to web sessions (channel_id IS NULL) excluding test/system users.

        Excludes __agent__ and __scheduler__ system user IDs.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Priority 1: find a session with a real channel (Telegram, etc.)
            cursor.execute("""
                SELECT * FROM chat_sessions
                WHERE agent_id = ? AND (archived IS NULL OR archived = 0)
                  AND external_user_id NOT LIKE '__agent__%'
                  AND external_user_id != '__scheduler__'
                  AND channel_id IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
            """, (agent_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            # Priority 2: fallback to web session (no channel), exclude test/system
            cursor.execute("""
                SELECT * FROM chat_sessions
                WHERE agent_id = ? AND (archived IS NULL OR archived = 0)
                  AND external_user_id NOT LIKE '__agent__%'
                  AND external_user_id != '__scheduler__'
                  AND external_user_id != 'web_test'
                  AND external_user_id != '__system__'
                ORDER BY updated_at DESC LIMIT 1
            """, (agent_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_sessions_with_preview(self) -> List[Dict[str, Any]]:
        """Get all non-archived sessions with message count and last message preview."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                WITH msg_counts AS (
                    SELECT session_id, COUNT(*) AS cnt
                    FROM chat_messages
                    GROUP BY session_id
                ),
                last_msg AS (
                    SELECT session_id, content, role,
                        ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at DESC) AS rn
                    FROM chat_messages
                    WHERE role IN ('user', 'assistant') AND content IS NOT NULL
                )
                SELECT s.*,
                    COALESCE(mc.cnt, 0) AS message_count,
                    lm.content AS last_message,
                    lm.role AS last_message_role
                FROM chat_sessions s
                LEFT JOIN msg_counts mc ON mc.session_id = s.id
                LEFT JOIN last_msg lm ON lm.session_id = s.id AND lm.rn = 1
                WHERE s.archived = 0
                ORDER BY s.updated_at DESC
            """)
            return [dict(r) for r in cursor.fetchall()]

    def get_counts(self) -> tuple:
        """Return (session_count, message_count)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM chat_sessions")
            sc = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM chat_messages")
            mc = cursor.fetchone()[0]
            return sc, mc

    def get_web_fallback_session(self, agent_id: str,
                                 exclude_session_id: str = None) -> Optional[Dict[str, Any]]:
        """Return the most recent web session (channel_id IS NULL) for a human user.

        Used by escalate_to_user as a secondary delivery target so the user
        can also see escalated messages in the web UI.

        Excludes __agent__, __scheduler__, and __system__ user IDs.
        web_test is intentionally NOT excluded here — it IS the valid web
        session for the user chatting via browser.
        Optionally excludes a specific session_id (e.g., the primary channel session).
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT * FROM chat_sessions
                WHERE agent_id = ? AND (archived IS NULL OR archived = 0)
                  AND external_user_id NOT LIKE '__agent__%'
                  AND external_user_id != '__scheduler__'
                  AND external_user_id != '__system__'
                  AND channel_id IS NULL
            """
            params = [agent_id]
            if exclude_session_id:
                query += " AND id != ?"
                params.append(exclude_session_id)
            query += " ORDER BY updated_at DESC LIMIT 1"
            cursor.execute(query, params)
            row = cursor.fetchone()
            return dict(row) if row else None

    # ---- Long-term Memory ----

    def add_memory(self, content: str, category: str = 'general',
                   source_session_id: str = None) -> int:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO memories (content, category, source_session_id) VALUES (?, ?, ?)",
                (content, category, source_session_id))
            conn.commit()
            return cursor.lastrowid

    def update_memory(self, memory_id: int, content: str, category: str = None):
        with self._connect() as conn:
            if category:
                conn.execute(
                    "UPDATE memories SET content=?, category=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (content, category, memory_id))
            else:
                conn.execute(
                    "UPDATE memories SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (content, memory_id))
            conn.commit()

    def search_memories(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """FTS5 BM25 keyword search over non-expired memories."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.* FROM memories m
                JOIN memories_fts ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                AND m.expired = 0
                ORDER BY rank
                LIMIT ?
            """, (query, limit))
            return [dict(r) for r in cursor.fetchall()]

    def get_all_memories(self, include_expired: bool = False) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if include_expired:
                cursor.execute("SELECT * FROM memories ORDER BY updated_at DESC")
            else:
                cursor.execute("SELECT * FROM memories WHERE expired=0 ORDER BY updated_at DESC")
            return [dict(r) for r in cursor.fetchall()]

    def get_recent_memories(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM memories WHERE expired=0 ORDER BY updated_at DESC LIMIT ?",
                (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def expire_memory(self, memory_id: int):
        """Soft-delete a memory."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET expired=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (memory_id,))
            conn.commit()


class AgentChatManager:
    """Manages per-agent AgentChatDB instances."""

    def __init__(self):
        self._dbs: Dict[str, AgentChatDB] = {}

    def get(self, agent_id: str) -> AgentChatDB:
        if agent_id not in self._dbs:
            self._dbs[agent_id] = AgentChatDB(agent_id)
        return self._dbs[agent_id]


agent_chat_manager = AgentChatManager()
