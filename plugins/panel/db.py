"""
PanelDB — SQLite storage for the Agent Panel plugin.

All panel action state and execution logs live in data/db/plugins/panel.db (WAL mode).
Plugin DBs are stored globally so they survive plugin uninstall/reinstall.
SQLite is authoritative — file log is best-effort.
"""

import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional, Dict, Any, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Prefer shared/data/ when present (post-migration environments)
_shared_data = os.path.join(BASE_DIR, 'shared', 'data')
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, 'data')
PLUGIN_DB_DIR = os.path.join(_data_root, 'db', 'plugins')
DB_PATH = os.path.join(PLUGIN_DB_DIR, 'panel.db')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PanelDB:
    """Per-agent panel action CRUD and execution logging.

    Instantiate with an agent_id to scope all operations to that agent.

    Usage:
        db = PanelDB('my_agent')
        actions = db.list_actions()
    """

    VALID_ACTION_TYPES = ('script', 'prompt')

    def __init__(self, agent_id: str, db_path: str = DB_PATH):
        self.agent_id = agent_id
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that returns a SQLite connection.

        The connection is opened on entry and closed on exit to prevent leaks.
        Includes automatic transaction management (commit/rollback).
        """
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_tables(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS panel_actions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id        TEXT NOT NULL,
                    label           TEXT NOT NULL,
                    action_type     TEXT NOT NULL CHECK(action_type IN ('script', 'prompt')),
                    content         TEXT NOT NULL DEFAULT '',
                    params          TEXT NOT NULL DEFAULT '[]',
                    sort_order      INTEGER NOT NULL DEFAULT 0,
                    enabled         INTEGER NOT NULL DEFAULT 1,
                    confirm_dialog  INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
            """)
            # Migration: add confirm_dialog column for existing databases
            try:
                conn.execute("ALTER TABLE panel_actions ADD COLUMN confirm_dialog INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS panel_execution_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id      TEXT NOT NULL,
                    action_id     INTEGER,
                    action_label  TEXT NOT NULL DEFAULT '',
                    action_type   TEXT NOT NULL DEFAULT '',
                    params_used   TEXT NOT NULL DEFAULT '{}',
                    result        TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL CHECK(status IN ('success', 'error')),
                    executed_at   TEXT NOT NULL
                )
            """)
            # Indexes for common queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_panel_actions_agent
                ON panel_actions(agent_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_panel_execution_log_agent
                ON panel_execution_log(agent_id)
            """)

    # ─── Action CRUD ───────────────────────────────────────────────────

    def list_actions(self) -> List[Dict[str, Any]]:
        """Return all actions for this agent, ordered by sort_order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM panel_actions WHERE agent_id = ? ORDER BY sort_order ASC, id ASC",
                (self.agent_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_action(self, action_id: int) -> Optional[Dict[str, Any]]:
        """Return a single action by id, scoped to this agent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM panel_actions WHERE id = ? AND agent_id = ?",
                (action_id, self.agent_id)
            ).fetchone()
            return dict(row) if row else None

    def add_action(self, label: str, action_type: str, content: str = '',
                   params: str = '[]', sort_order: int = 0,
                   enabled: bool = True, confirm_dialog: bool = False) -> Dict[str, Any]:
        """Create a new action. Returns the created row as a dict."""
        if action_type not in self.VALID_ACTION_TYPES:
            raise ValueError(f"Invalid action_type: {action_type!r}. Must be one of {self.VALID_ACTION_TYPES}")

        now = _now()
        enabled_int = 1 if enabled else 0
        confirm_int = 1 if confirm_dialog else 0
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO panel_actions
                   (agent_id, label, action_type, content, params, sort_order, enabled, confirm_dialog, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.agent_id, label, action_type, content, params, sort_order, enabled_int, confirm_int, now, now)
            )
            row = conn.execute(
                "SELECT * FROM panel_actions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row)

    def update_action(self, action_id: int, **fields) -> Optional[Dict[str, Any]]:
        """Update an action. Supports partial updates. Returns the updated row.

        Allowed fields: label, action_type, content, params, sort_order, enabled.
        """
        allowed = {'label', 'action_type', 'content', 'params', 'sort_order', 'enabled', 'confirm_dialog'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_action(action_id)

        if 'action_type' in updates and updates['action_type'] not in self.VALID_ACTION_TYPES:
            raise ValueError(
                f"Invalid action_type: {updates['action_type']!r}. Must be one of {self.VALID_ACTION_TYPES}"
            )

        if 'enabled' in updates:
            updates['enabled'] = 1 if updates['enabled'] else 0

        if 'confirm_dialog' in updates:
            updates['confirm_dialog'] = 1 if updates['confirm_dialog'] else 0

        updates['updated_at'] = _now()

        set_clause = ', '.join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [action_id, self.agent_id]

        with self._connect() as conn:
            conn.execute(
                f"UPDATE panel_actions SET {set_clause} WHERE id = ? AND agent_id = ?",
                values
            )
            row = conn.execute(
                "SELECT * FROM panel_actions WHERE id = ?", (action_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_action(self, action_id: int) -> bool:
        """Delete an action. Returns True if deleted, False if not found."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM panel_actions WHERE id = ? AND agent_id = ?",
                (action_id, self.agent_id)
            )
            return cursor.rowcount > 0

    # ─── Execution Logging ─────────────────────────────────────────────

    def log_execution(self, action_id: Optional[int], action_label: str,
                      action_type: str, params_used: dict,
                      result: str, status: str) -> Dict[str, Any]:
        """Record an execution log entry. Returns the created row.

        SQLite is authoritative. File log is best-effort.
        """
        if status not in ('success', 'error'):
            raise ValueError(f"Invalid status: {status!r}. Must be 'success' or 'error'.")

        now = _now()
        params_str = json.dumps(params_used) if params_used else '{}'

        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO panel_execution_log
                   (agent_id, action_id, action_label, action_type, params_used, result, status, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.agent_id, action_id, action_label, action_type, params_str,
                 result, status, now)
            )
            row = conn.execute(
                "SELECT * FROM panel_execution_log WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row)

    def get_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return execution log entries for this agent, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM panel_execution_log
                   WHERE agent_id = ?
                   ORDER BY executed_at DESC
                   LIMIT ?""",
                (self.agent_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── Utility ───────────────────────────────────────────────────────

    def _updated_at_stamp(self) -> Optional[str]:
        """Return the latest updated_at across all actions for ETag caching."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) AS max_updated FROM panel_actions WHERE agent_id = ?",
                (self.agent_id,)
            ).fetchone()
            return row['max_updated'] if row else None


# ---------------------------------------------------------------------------
# Factory — used by routes.py to get a per-agent PanelDB instance
# ---------------------------------------------------------------------------

def panel_db_factory(agent_id: str) -> PanelDB:
    """Return a PanelDB instance for the given agent_id."""
    return PanelDB(agent_id)
