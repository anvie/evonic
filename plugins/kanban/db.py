"""
KanbanDB — SQLite storage for the Kanban board plugin.

All kanban task state lives in data/db/plugins/kanban.db (WAL mode).
Plugin DBs are stored globally so they survive plugin uninstall/reinstall.
On first run, existing tasks.json is imported and renamed to tasks.json.migrated.
"""

import sqlite3
import os
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Prefer shared/data/ when present (post-migration environments)
_shared_data = os.path.join(BASE_DIR, 'shared', 'data')
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, 'data')
PLUGIN_DB_DIR = os.path.join(_data_root, 'db', 'plugins')
DB_PATH = os.path.join(PLUGIN_DB_DIR, 'kanban.db')
# Directory where uploaded task attachment files are stored on disk.
# Lives next to the plugin DB so it survives plugin reinstall.
ATTACHMENTS_DIR = os.path.join(_data_root, 'uploads', 'kanban')
TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tasks.json')


def _now():
    return datetime.now(timezone.utc).isoformat()


class KanbanDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_tables()
        self._migrate_created_by()
        self._migrate_columns()
        self._migrate_remove_free_pick()
        self._migrate_archived_at()
        self._migrate_paused_at()
        self._migrate_started_at()
        self._migrate_task_dependencies()
        self._migrate_attachments()
        self._migrate_from_json()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that returns a SQLite connection for the Kanban database.
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
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'todo',
                    priority    TEXT NOT NULL DEFAULT 'low',
                    assignee    TEXT,
                    created_by  TEXT,
                    completed_at TEXT,
                    started_at  TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            # Comments table — per-task discussion
            conn.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    INTEGER NOT NULL,
                    content    TEXT NOT NULL,
                    author     TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)
            # Activity log table — tracks all task state changes
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id  INTEGER NOT NULL,
                    action   TEXT NOT NULL,
                    details  TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)
            # Process log table — stores full agent execution history per task
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_process_logs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    INTEGER NOT NULL,
                    agent_id   TEXT,
                    session_id TEXT,
                    messages   TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)
    def _migrate_columns(self):
        """Rename claimed_by/claimed_at → picked_by/picked_at if old columns still exist."""
        with self._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'claimed_by' in cols and 'picked_by' not in cols:
                conn.execute("ALTER TABLE tasks RENAME COLUMN claimed_by TO picked_by")
            if 'claimed_at' in cols and 'picked_at' not in cols:
                conn.execute("ALTER TABLE tasks RENAME COLUMN claimed_at TO picked_at")

    def _migrate_remove_free_pick(self):
        """Remove free_pick, picked_by, picked_at columns if they still exist.
        Copies picked_by → assignee where assignee is NULL to preserve data."""
        with self._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'free_pick' not in cols and 'picked_by' not in cols:
                return  # already migrated

            # Promote picked_by → assignee where assignee is null
            if 'picked_by' in cols:
                conn.execute("""
                    UPDATE tasks SET assignee = picked_by
                    WHERE picked_by IS NOT NULL AND (assignee IS NULL OR assignee = '')
                """)

            # Rebuild table without the old columns
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks_new (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'todo',
                    priority    TEXT NOT NULL DEFAULT 'low',
                    assignee    TEXT,
                    created_by  TEXT,
                    completed_at TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO tasks_new (id, title, description, status, priority,
                                       assignee, created_by, completed_at, created_at, updated_at)
                SELECT id, title, description, status, priority,
                       assignee, created_by, completed_at, created_at, updated_at
                FROM tasks
            """)
            conn.execute("DROP TABLE tasks")
            conn.execute("ALTER TABLE tasks_new RENAME TO tasks")

    def _migrate_created_by(self):
        """Add created_by column if it doesn't exist."""
        with self._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'created_by' not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN created_by TEXT")

    def _migrate_archived_at(self):
        """Add archived_at column if it doesn't exist."""
        with self._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'archived_at' not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")

    def _migrate_paused_at(self):
        """Add paused_at column if it doesn't exist."""
        with self._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'paused_at' not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN paused_at TEXT")

    def _migrate_started_at(self):
        """Add started_at column if it doesn't exist."""
        with self._connect() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'started_at' not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN started_at TEXT")

    def _migrate_task_dependencies(self):
        """Create task_dependencies table if it doesn't exist."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id    INTEGER NOT NULL,
                    depends_on INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, depends_on),
                    FOREIGN KEY (task_id)    REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)

    def _migrate_attachments(self):
        """Create attachments table if it doesn't exist.

        Each row references a task and points to a stored file on disk under
        the plugin's uploads directory. The stored filename is generated
        server-side (uuid prefix) so it is safe to use on any filesystem.
        """
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     INTEGER NOT NULL,
                    comment_id  INTEGER,
                    filename    TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    mime_type   TEXT,
                    size        INTEGER,
                    uploaded_by TEXT,
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
                )
            """)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()}
            if 'comment_id' not in cols:
                conn.execute("ALTER TABLE attachments ADD COLUMN comment_id INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_task_id ON attachments(task_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_comment_id ON attachments(comment_id)"
            )

    # ── Dependencies ─────────────────────────────────────────────────────────

    def _detect_cycle(self, task_id: int, new_dep_ids: list) -> bool:
        """Return True if adding new_dep_ids as dependencies of task_id would create a cycle.

        Uses BFS: starting from each new dep, walks the dependency graph.
        If we reach task_id, a cycle exists.
        """
        with self._connect() as conn:
            def deps_of(tid):
                rows = conn.execute(
                    "SELECT depends_on FROM task_dependencies WHERE task_id = ?", (tid,)
                ).fetchall()
                return [r[0] for r in rows]

            visited = set()
            queue = list(new_dep_ids)
            while queue:
                current = queue.pop()
                if current == task_id:
                    return True
                if current in visited:
                    continue
                visited.add(current)
                queue.extend(deps_of(current))
        return False

    def set_dependencies(self, task_id: int, dep_ids: list) -> None:
        """Replace all dependencies for task_id with dep_ids.

        Raises ValueError on cycle detection or if a dep task doesn't exist.
        """
        dep_ids = [int(d) for d in dep_ids]
        # Remove self-references
        dep_ids = [d for d in dep_ids if d != task_id]
        if not dep_ids:
            with self._connect() as conn:
                conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
            return
        # Validate all dep tasks exist
        with self._connect() as conn:
            for dep_id in dep_ids:
                row = conn.execute("SELECT id FROM tasks WHERE id = ?", (dep_id,)).fetchone()
                if not row:
                    raise ValueError(f"Dependency task #{dep_id} does not exist.")
        if self._detect_cycle(task_id, dep_ids):
            raise ValueError(f"Adding these dependencies would create a circular dependency.")
        now = _now()
        with self._connect() as conn:
            conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
            for dep_id in dep_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on, created_at) VALUES (?, ?, ?)",
                    (task_id, dep_id, now),
                )

    def get_dependencies(self, task_id: int) -> list:
        """Return list of task IDs that task_id depends on."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ? ORDER BY depends_on",
                (task_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_dependents(self, task_id: int) -> list:
        """Return list of task IDs that depend on task_id."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM task_dependencies WHERE depends_on = ? ORDER BY task_id",
                (task_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_unmet_dependencies(self, task_id: int) -> list:
        """Return list of dependency task dicts that are NOT in 'done' status."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT t.* FROM tasks t
                JOIN task_dependencies td ON td.depends_on = t.id
                WHERE td.task_id = ? AND t.status != 'done'
                ORDER BY t.id
            """, (task_id,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def has_unmet_dependencies(self, task_id: int) -> bool:
        """Return True if task_id has at least one dependency not in 'done' status."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT 1 FROM tasks t
                JOIN task_dependencies td ON td.depends_on = t.id
                WHERE td.task_id = ? AND t.status != 'done'
                LIMIT 1
            """, (task_id,)).fetchone()
        return row is not None

    def get_all_dependencies(self) -> dict:
        """Return {task_id: [dep_id, ...]} for all tasks that have dependencies."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, depends_on FROM task_dependencies ORDER BY task_id, depends_on"
            ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row[0], []).append(row[1])
        return result

    # ── Attachments ─────────────────────────────────────────────────────────

    def add_attachment(self, task_id: str, filename: str, stored_name: str,
                       mime_type: str = None, size: int = None,
                       uploaded_by: str = None, comment_id: int = None) -> Optional[dict]:
        """Record a file attachment for a task or task comment."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO attachments (task_id, comment_id, filename, stored_name, mime_type, size, uploaded_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, comment_id, filename, stored_name, mime_type, size, uploaded_by, _now()),
            )
            new_id = cur.lastrowid
        return self.get_attachment(new_id)

    def get_attachment(self, attachment_id: int) -> Optional[dict]:
        """Return a single attachment by ID, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_attachments(self, task_id: str) -> list:
        """Return all attachments for a task, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_attachments_for_comment(self, comment_id: int) -> list:
        """Return all attachments belonging to one comment, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE comment_id = ? ORDER BY id ASC",
                (comment_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_attachment(self, attachment_id: int) -> Optional[dict]:
        """Delete an attachment row. Returns the deleted row (for file cleanup) or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        return self._row_to_dict(row)

    def delete_attachments_for_comment(self, comment_id: int) -> list:
        """Delete comment attachment rows and return them for file cleanup."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE comment_id = ?", (comment_id,)
            ).fetchall()
            conn.execute("DELETE FROM attachments WHERE comment_id = ?", (comment_id,))
        return [self._row_to_dict(r) for r in rows]

    def delete_attachments_for_task(self, task_id: str) -> list:
        """Delete all attachment rows for a task. Returns the deleted rows for file cleanup."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE task_id = ?", (task_id,)
            ).fetchall()
            conn.execute("DELETE FROM attachments WHERE task_id = ?", (task_id,))
        return [self._row_to_dict(r) for r in rows]

    def _migrate_from_json(self):
        """Import tasks.json into DB on first run, then rename it."""
        if not os.path.isfile(TASKS_FILE):
            return
        try:
            with open(TASKS_FILE) as f:
                tasks = json.loads(f.read().strip() or '[]')
            if not isinstance(tasks, list):
                return
            with self._connect() as conn:
                for t in tasks:
                    conn.execute("""
                        INSERT OR IGNORE INTO tasks
                        (id, title, description, status, priority, assignee,
                         completed_at, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, (
                        t.get('id'), t.get('title', ''),
                        t.get('description', ''), t.get('status', 'todo'),
                        t.get('priority', 'low'), t.get('assignee'),
                        t.get('completed_at'),
                        t.get('created_at', _now()), t.get('updated_at', _now()),
                    ))
            os.rename(TASKS_FILE, TASKS_FILE + '.migrated')
        except Exception:
            pass

    @staticmethod
    def _row_to_dict(row) -> dict:
        return dict(row)

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_active_task_for_agent(self, agent_id: str) -> Optional[dict]:
        """Return the first in-progress task assigned to agent_id, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE assignee = ? AND status = 'in-progress' LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_all(self) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, task_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    # ── Write ────────────────────────────────────────────────────────────────

    def create(self, task: dict) -> dict:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO tasks
                (title, description, status, priority, assignee, created_by,
                 completed_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                task['title'], task.get('description', ''),
                task.get('status', 'todo'), task.get('priority', 'low'),
                task.get('assignee'), task.get('created_by'),
                task.get('completed_at'), task['created_at'], task['updated_at'],
            ))
            new_id = cur.lastrowid
        return self.get(new_id)

    def update(self, task_id: str, fields: dict) -> Optional[dict]:
        """Update specific fields. Returns updated task dict or None if not found."""
        allowed = {
            'title', 'description', 'status', 'priority', 'assignee',
            'completed_at', 'started_at', 'updated_at',
        }
        to_set = {k: v for k, v in fields.items() if k in allowed}
        if not to_set:
            return self.get(task_id)
        cols = ', '.join(f'{k} = ?' for k in to_set)
        vals = list(to_set.values()) + [task_id]
        with self._connect() as conn:
            conn.execute(f'UPDATE tasks SET {cols} WHERE id = ?', vals)
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0

    # ── Atomic operations ────────────────────────────────────────────────────

    def assign(self, task_id: str, agent_id: str) -> Optional[dict]:
        """Set assignee atomically. Returns updated task or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE tasks SET assignee = ?, updated_at = ? WHERE id = ?",
                (agent_id, _now(), task_id),
            )
        return self.get(task_id)

    # ─── Comments ──────────────────────────────────────────────────────────────

    def add_comment(self, task_id: str, content: str, author: str = None) -> Optional[dict]:
        """Add a comment to a task. Returns the comment dict or None."""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO comments (task_id, content, author, created_at) VALUES (?, ?, ?, ?)",
                (task_id, content, author, now),
            )
            row = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_comments(self, task_id: str) -> list:
        """Get all comments for a task, ordered by creation time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_comments_since(self, task_id: str, since: str, exclude_authors: list = None) -> list:
        """Get comments for a task created after `since` (ISO timestamp).

        Args:
            task_id: The task ID.
            since: ISO timestamp — only comments created strictly after this are returned.
            exclude_authors: Optional list of author names to exclude (e.g. the agent's own comments).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? AND created_at > ? ORDER BY created_at ASC",
                (task_id, since),
            ).fetchall()
        comments = [dict(r) for r in rows]
        if exclude_authors:
            comments = [c for c in comments if c.get('author') not in exclude_authors]
        return comments

    def get_last_comment(self, task_id: str) -> Optional[dict]:
        """Get the most recent comment for a task, or None if no comments exist."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_last_comment_before(self, task_id: str, before: str) -> Optional[dict]:
        """Get the most recent comment created strictly before `before` (ISO timestamp)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? AND created_at <= ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (task_id, before),
            ).fetchone()
        return dict(row) if row else None

    def get_comments_paginated(self, task_id: str, limit: int = 10, offset: int = 0) -> dict:
        """Get comments for a task with pagination, ordered DESC (newest first).

        Returns:
            dict with 'comments' (list of comment dicts) and 'total' (int).
        """
        with self._connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM comments WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            total = count_row[0] if count_row else 0

            rows = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            ).fetchall()
        return {
            'comments': [dict(r) for r in rows],
            'total': total,
        }

    def get_comment(self, comment_id: int) -> Optional[dict]:
        """Get a single comment by ID, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM comments WHERE id = ?",
                (comment_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_comment(self, comment_id: int) -> bool:
        """Delete a comment by ID. Returns True if deleted, False if not found."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
            return cursor.rowcount > 0


    # ─── Activity Log ──────────────────────────────────────────────────────────

    def add_activity(self, task_id: str, action: str, details: str = None) -> Optional[dict]:
        """Log an activity for a task. Returns the activity dict or None."""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO activity_log (task_id, action, details, created_at) VALUES (?, ?, ?, ?)",
                (task_id, action, details, now),
            )
            row = conn.execute(
                "SELECT * FROM activity_log WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_activity(self, task_id: str) -> list:
        """Get all activity log entries for a task, ordered by creation time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_log WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Convenience: log common task events ───────────────────────────────────

    def log_task_created(self, task_id: str):
        self.add_activity(task_id, 'created', 'Task created')

    def log_task_updated(self, task_id: str, changes: str):
        self.add_activity(task_id, 'updated', changes)

    def log_task_status_change(self, task_id: str, from_status: str, to_status: str):
        self.add_activity(task_id, 'status_changed', f'{from_status} → {to_status}')

    def log_task_deleted(self, task_id: str):
        self.add_activity(task_id, 'deleted', 'Task deleted')

    def archive_task(self, task_id: str) -> Optional[dict]:
        """Archive a task by setting archived_at. Returns updated task or None."""
        now = _now()
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET archived_at = ?, updated_at = ? WHERE id = ?", (now, now, task_id))
        return self.get(task_id)

    def unarchive_task(self, task_id: str) -> Optional[dict]:
        """Unarchive a task by clearing archived_at. Returns updated task or None."""
        now = _now()
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET archived_at = NULL, updated_at = ? WHERE id = ?", (now, task_id))
        return self.get(task_id)

    def get_archived(self) -> list:
        """Get all archived tasks, ordered by archived_at descending."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_archived(self) -> int:
        """Count all archived tasks."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE archived_at IS NOT NULL"
            ).fetchone()
        return row['cnt'] if row else 0

    def clear_archived(self) -> int:
        """Delete all archived tasks. Returns count of deleted tasks."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE archived_at IS NOT NULL").fetchone()
            count = row['cnt'] if row else 0
            conn.execute("DELETE FROM tasks WHERE archived_at IS NOT NULL")
        return count

    def get_archived_incomplete(self) -> list:
        """Get all archived tasks that are not in 'done' status (archived before completion)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE archived_at IS NOT NULL AND status != 'done' ORDER BY archived_at DESC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_archived_incomplete_count(self) -> int:
        """Count all archived tasks that are not in 'done' status."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE archived_at IS NOT NULL AND status != 'done'"
            ).fetchone()
        return row['cnt'] if row else 0


    def save_process_log(self, task_id: int, agent_id: str, session_id: str, messages: list) -> bool:
        """Save full agent execution process. Replaces existing log for the task."""
        try:
            messages_json = json.dumps(messages, ensure_ascii=False)
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM task_process_logs WHERE task_id = ?",
                    (task_id,)
                )
                conn.execute(
                    """INSERT INTO task_process_logs (task_id, agent_id, session_id, messages, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (task_id, agent_id, session_id, messages_json, _now())
                )
            return True
        except Exception:
            return False

    def get_process_log(self, task_id: int) -> Optional[dict]:
        """Get process log for a task. Returns dict with messages list or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_process_logs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,)
            ).fetchone()
        if not row:
            return None
        d = self._row_to_dict(row)
        try:
            d['messages'] = json.loads(d.get('messages', '[]'))
        except (json.JSONDecodeError, TypeError):
            d['messages'] = []
        return d

kanban_db = KanbanDB()
