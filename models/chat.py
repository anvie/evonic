import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Generator, Tuple

AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'agents')
SUB_AGENTS_TMP_DIR = "/tmp/evonic-sub-agents"

# Maximum number of messages fetchable in a single paginated request.
# Prevents O(N) cost on sessions with thousands of messages.
MAX_LIMIT = 500

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
    cursor.execute("UPDATE session_state SET session_id = ? WHERE session_id = ?", (new_id, old_id))


class AgentChatDB:
    """SQLite database per agent for chat sessions and messages."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        # Sub-agents store their chat DB in a temp directory (they are ephemeral)
        # so they don't pollute the persistent agents/ directory.
        from backend.subagent_manager import subagent_manager
        if subagent_manager.is_subagent(agent_id):
            agent_dir = os.path.join(SUB_AGENTS_TMP_DIR, agent_id)
        else:
            agent_dir = os.path.join(AGENTS_DIR, agent_id)
        os.makedirs(agent_dir, exist_ok=True)
        self.db_path = os.path.join(agent_dir, 'chat.db')
        self._conn = None
        self._lock = threading.Lock()
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the single persistent connection, creating it if needed."""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except Exception:
                self._conn = None
        conn = sqlite3.connect(f"file:{self.db_path}?mode=rwc&busy_timeout=10000", uri=True)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA mmap_size=268435456")
        self._conn = conn
        return conn

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager returning a shared persistent connection.

        One connection per AgentChatDB instance (not per-thread) keeps FD
        count bounded to ~42 (one per agent) while avoiding the PRAGMA
        overhead and WAL checkpoint-on-last-close penalty of open/close
        per request.
        """
        with self._lock:
            conn = self._get_conn()
            with conn:
                yield conn

    def close(self):
        """Explicitly close the persistent connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

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
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_created ON chat_messages(session_id, created_at DESC)")
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
            # Migration: add user_id column for UserMixin integration
            try:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN user_id TEXT REFERENCES users(id)")
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
            # Per-session state table -- stores mode/tasks/plan_file/states/auto_trivial per session_id.
            # focus/focus_reason remain in agent_state (global) for cross-session busy rejection.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_state (
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
            # Migration: add dimension column for semantic conflict detection
            try:
                cursor.execute("ALTER TABLE memories ADD COLUMN dimension TEXT")
            except sqlite3.OperationalError:
                pass
            # Migration: add superseded_by column for temporal state tracking
            try:
                cursor.execute("ALTER TABLE memories ADD COLUMN superseded_by INTEGER REFERENCES memories(id)")
            except sqlite3.OperationalError:
                pass
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_dimension ON memories(dimension)")
            conn.commit()

    def get_session_id(self, agent_id: str, external_user_id: str,
                        channel_id: str = None) -> Optional[str]:
        """Read-only session lookup. Returns session_id if it exists, else None."""
        from models.chatlog import session_slug
        slug = f"{agent_id}-{session_slug(external_user_id, agent_id=agent_id)}"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if channel_id:
                row = conn.execute("""
                    SELECT id FROM chat_sessions
                    WHERE agent_id = ? AND channel_id = ? AND external_user_id = ?
                    AND (archived IS NULL OR archived = 0)
                """, (agent_id, channel_id, external_user_id)).fetchone()
            else:
                row = conn.execute("""
                    SELECT id FROM chat_sessions
                    WHERE agent_id = ? AND channel_id IS NULL AND external_user_id = ?
                    AND (archived IS NULL OR archived = 0)
                """, (agent_id, external_user_id)).fetchone()
            return row['id'] if row else None

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
                # The INSERT hit the primary key: a session with this slug
                # already exists. The slug is derived from agent_id +
                # external_user_id only (channel-agnostic), so the SAME
                # user+agent seen under a DIFFERENT channel_id collides here —
                # the shared-channel case where a channel was recreated or
                # re-paired and got a new channel_id, plus the concurrent-insert
                # race. Recover by the actual colliding key (id = slug) and
                # point the session at the current channel so the next lookup
                # finds it directly. (Previously this re-queried by the new
                # channel_id, found nothing, and crashed on row['id'] = None.)
                cursor.execute("SELECT id FROM chat_sessions WHERE id = ?", (slug,))
                row = cursor.fetchone()
                if not row:
                    # Not a slug collision after all — re-raise the real error
                    # instead of masking it with a None subscript.
                    raise
                cursor.execute(
                    "UPDATE chat_sessions SET channel_id = ?, archived = 0, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (channel_id, slug))
                conn.commit()
                return slug

    def get_session_messages(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, session_id, role, content, tool_calls, tool_call_id, metadata, created_at FROM chat_messages WHERE session_id = ?
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

    def get_latest_agent_request_metadata(self, session_id: str, sender_agent_id: str = None) -> Optional[dict]:
        """Return the newest routable agent-request metadata in a session.

        Only user-role rows are candidates. Invalid JSON, non-agent messages,
        sender mismatches, and requests without ``report_to_id`` are skipped so
        later background notifications cannot shadow a routable delegation.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT metadata FROM chat_messages
                WHERE session_id = ? AND role = 'user' AND metadata IS NOT NULL
                ORDER BY created_at DESC, id DESC
            """, (session_id,))
            for row in rows:
                try:
                    metadata = json.loads(row['metadata'])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(metadata, dict) or metadata.get('agent_message') is not True:
                    continue
                if sender_agent_id and metadata.get('from_agent_id') != sender_agent_id:
                    continue
                if metadata.get('report_to_id'):
                    return metadata
        return None

    def add_chat_message(self, session_id: str, role: str, content: str = None,
                          tool_calls=None, tool_call_id: str = None,
                          metadata: dict = None) -> int:
        tc_json = json.dumps(tool_calls) if tool_calls else None
        meta_json = json.dumps(metadata, default=str) if metadata else None
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

    def get_agent_state(self) -> Optional[str]:
        # A cached handle can outlive its DB file (an ephemeral sub-agent whose
        # /tmp chat dir was rmtree'd on destroy). No file → no state.
        if not os.path.exists(self.db_path):
            return None
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

    # -- Per-session state --

    def upsert_session_state(self, session_id: str, content: str):
        """Save session-level state (mode/tasks/plan_file/states/auto_trivial)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_state (session_id, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (session_id, content))
            conn.commit()

    def get_session_state(self, session_id: str) -> Optional[str]:
        """Get session-level state for a specific session_id.

        If no session_state exists yet, performs one-time migration from the
        global agent_state (__agent__): copies mode/tasks/plan_file/states/auto_trivial
        to session_state while leaving focus/focus_reason in agent_state.
        """
        if not os.path.exists(self.db_path):
            return None  # DB removed (e.g. destroyed ephemeral sub-agent)
        with self._connect() as conn:
            # Try session-specific state first
            row = conn.execute(
                "SELECT content FROM session_state WHERE session_id = ?",
                (session_id,)).fetchone()
            if row:
                return row[0]

            # One-time migration: copy per-session fields from global agent_state
            row = conn.execute(
                "SELECT content FROM agent_state WHERE session_id = ?",
                (self._AGENT_STATE_KEY,)).fetchone()
            if row:
                try:
                    import json
                    data = json.loads(row[0])
                    # Extract only per-session fields
                    session_data = {
                        'mode': data.get('mode', 'plan'),
                        'tasks': data.get('tasks', []),
                        'next_task_id': data.get('next_task_id', 1),
                        'plan_file': data.get('plan_file'),
                        'states': data.get('states', {}),
                        'auto_trivial': data.get('auto_trivial', False),
                    }
                    session_content = json.dumps(session_data)
                    # Save to session_state
                    conn.execute(
                        "INSERT OR REPLACE INTO session_state (session_id, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (session_id, session_content))
                    conn.commit()
                    return session_content
                except (json.JSONDecodeError, TypeError):
                    pass
        return None

    def clear_session(self, session_id: str):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM chat_summaries WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM session_state WHERE session_id = ?", (session_id,))
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

    def get_last_message_timestamp(self, session_id: str) -> Optional[float]:
        """Return the unix timestamp of the most recent message in a session.

        Returns None if the session has no messages.

        Performance: the composite index (session_id, created_at DESC)
        lets SQLite seek directly to the newest row — O(log n), no sort.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT strftime('%s', created_at) FROM chat_messages "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return float(row[0])
            return None

    # ---- Summarization ----

    def get_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, session_id, summary, last_message_id, message_count, created_at, updated_at, last_message_ts FROM chat_summaries WHERE session_id = ?", (session_id,))
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
                SELECT id, session_id, role, content, tool_calls, tool_call_id, metadata, created_at FROM chat_messages WHERE session_id = ? AND id > ?
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
                SELECT id, session_id, role, content, tool_calls, tool_call_id, metadata, created_at FROM chat_messages WHERE session_id = ? AND id > ? AND id <= ?
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

    def archive_sessions_by_agent_id(self, agent_id: str) -> tuple[int, list[str]]:
        """Archive all non-archived sessions whose agent_id matches.

        Used to clean up sub-agent sessions when the sub-agent is destroyed.
        Returns a tuple of (count_archived, list_of_session_ids).
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
            return len(session_ids), session_ids

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
            cursor.execute("SELECT id, agent_id, channel_id, external_user_id, bot_enabled, archived, created_at, updated_at FROM chat_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_session_messages_full(self, session_id: str, limit: int = 200, before_id: Optional[int] = None) -> Tuple[List[Dict[str, Any]], bool]:
        """Get messages for a session with pagination support.

        When only 'limit' is provided, returns the N most recent messages.
        When 'before_id' is also provided, returns up to 'limit' messages older than that id.

        Returns:
            Tuple of (messages, has_more) where has_more is True if there are older messages
            that were not fetched.
        """
        limit = max(1, min(limit, MAX_LIMIT))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            base_where = """role IN ('user', 'assistant') AND content IS NOT NULL
                          AND content != '' AND tool_calls IS NULL"""
            fetch_limit = limit + 1  # fetch one extra to detect has_more

            if before_id is not None:
                # Cursor-based: fetch older messages before the given id
                cursor.execute(f"""
                    SELECT id, role, content, metadata, created_at FROM chat_messages
                    WHERE session_id = ? AND id < ? AND {base_where}
                    ORDER BY id DESC
                    LIMIT ?
                """, (session_id, before_id, fetch_limit))
            else:
                # Default: fetch the N most recent messages
                cursor.execute(f"""
                    SELECT id, role, content, metadata, created_at FROM chat_messages
                    WHERE session_id = ? AND {base_where}
                    ORDER BY id DESC
                    LIMIT ?
                """, (session_id, fetch_limit))

            rows = [dict(r) for r in cursor.fetchall()]
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            # Re-sort in ascending order for consistent display
            rows.reverse()

            for r in rows:
                if r.get('metadata'):
                    try:
                        r['metadata'] = json.loads(r['metadata'])
                    except (json.JSONDecodeError, TypeError):
                        r['metadata'] = None
            return rows, has_more

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
                SELECT id, agent_id, channel_id, external_user_id, bot_enabled, archived, created_at, updated_at FROM chat_sessions
                WHERE agent_id = ? AND (archived IS NULL OR archived = 0)
                  AND external_user_id NOT LIKE '__agent__%'
                  AND external_user_id != '__scheduler__'
                  AND channel_id IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
            """, (agent_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            # Priority 2: fallback to web session (no channel), exclude system-only IDs.
            # web_test IS a valid web session — intentionally NOT excluded here.
            cursor.execute("""
                SELECT id, agent_id, channel_id, external_user_id, bot_enabled, archived, created_at, updated_at FROM chat_sessions
                WHERE agent_id = ? AND (archived IS NULL OR archived = 0)
                  AND external_user_id NOT LIKE '__agent__%'
                  AND external_user_id != '__scheduler__'
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
                SELECT id, agent_id, channel_id, external_user_id, bot_enabled, archived, created_at, updated_at FROM chat_sessions
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
                   source_session_id: str = None, dimension: str = None) -> int:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO memories (content, category, source_session_id, dimension) VALUES (?, ?, ?, ?)",
                (content, category, source_session_id, dimension))
            conn.commit()
            return cursor.lastrowid

    def update_memory(self, memory_id: int, content: str, category: str = None,
                      dimension: str = None):
        with self._connect() as conn:
            if category and dimension:
                conn.execute(
                    "UPDATE memories SET content=?, category=?, dimension=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (content, category, dimension, memory_id))
            elif category:
                conn.execute(
                    "UPDATE memories SET content=?, category=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (content, category, memory_id))
            elif dimension:
                conn.execute(
                    "UPDATE memories SET content=?, dimension=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (content, dimension, memory_id))
            else:
                conn.execute(
                    "UPDATE memories SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (content, memory_id))
            conn.commit()

    def search_memories(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """FTS5 BM25 keyword search over non-expired, non-superseded memories."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.* FROM memories m
                JOIN memories_fts ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                AND m.expired = 0
                AND m.superseded_by IS NULL
                ORDER BY rank
                LIMIT ?
            """, (query, limit))
            return [dict(r) for r in cursor.fetchall()]

    def get_all_memories(self, include_expired: bool = False) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if include_expired:
                cursor.execute("SELECT id, content, category, source_session_id, created_at, updated_at, expired, dimension, superseded_by FROM memories ORDER BY updated_at DESC LIMIT 10000")
            else:
                cursor.execute("SELECT id, content, category, source_session_id, created_at, updated_at, expired, dimension, superseded_by FROM memories WHERE expired=0 ORDER BY updated_at DESC LIMIT 10000")
            return [dict(r) for r in cursor.fetchall()]

    def get_recent_memories(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, content, category, source_session_id, created_at, updated_at, expired, dimension, superseded_by FROM memories WHERE expired=0 AND superseded_by IS NULL ORDER BY updated_at DESC LIMIT ?",
                (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def get_memories_by_dimension(self, dimension: str) -> List[Dict[str, Any]]:
        """Get all active, non-superseded memories with the given dimension."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, content, category, source_session_id, created_at, updated_at, expired, dimension, superseded_by FROM memories WHERE dimension = ? AND expired = 0 AND superseded_by IS NULL ORDER BY updated_at DESC LIMIT 10000",
                (dimension,)
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_null_dimension_memories(self) -> List[Dict[str, Any]]:
        """Get all active, non-superseded memories that have no dimension assigned."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, content, category, source_session_id, created_at, updated_at, expired, dimension, superseded_by FROM memories WHERE dimension IS NULL AND expired = 0 AND superseded_by IS NULL ORDER BY updated_at DESC LIMIT 10000")
            return [dict(r) for r in cursor.fetchall()]

    def get_active_dimensions(self, limit: int = 50) -> List[str]:
        """Distinct dimensions (keys) of active memories, most recently updated first."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT dimension, MAX(updated_at) AS mu FROM memories "
                "WHERE dimension IS NOT NULL AND expired = 0 AND superseded_by IS NULL "
                "GROUP BY dimension ORDER BY mu DESC LIMIT ?",
                (limit,))
            return [r[0] for r in cursor.fetchall()]

    def supersede_memory(self, old_memory_id: int, new_memory_id: int):
        """Mark old_memory_id as superseded by new_memory_id."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_memory_id, old_memory_id))
            conn.commit()

    def expire_memory(self, memory_id: int):
        """Soft-delete a memory."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET expired=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (memory_id,))
            conn.commit()

    def clear_all_memories(self) -> int:
        """Delete all memories. Returns the number of rows deleted."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories")
            conn.commit()
            return cursor.rowcount


class AgentChatManager:
    """Manages per-agent AgentChatDB instances."""

    def __init__(self):
        self._dbs: Dict[str, AgentChatDB] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> AgentChatDB:
        if agent_id not in self._dbs:
            with self._lock:
                # Double-check: another thread may have created it while we waited
                if agent_id not in self._dbs:
                    self._dbs[agent_id] = AgentChatDB(agent_id)
        return self._dbs[agent_id]

    def drop(self, agent_id: str) -> None:
        """Drop a cached AgentChatDB (e.g. when the agent is deleted).

        Closes the persistent connection first so it cannot keep reading a
        chat.db inode that is about to be unlinked from disk. Without this,
        recreating an agent with the same id returns the stale cached
        instance whose connection points at the deleted file → queries
        fail with 'no such table: chat_sessions'.
        """
        with self._lock:
            db = self._dbs.pop(agent_id, None)
        if db is not None:
            db.close()


agent_chat_manager = AgentChatManager()
