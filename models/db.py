import sqlite3
import os
import threading
from contextlib import contextmanager
from typing import Generator
import config
from models.schema import SchemaMixin, _migrate_db_to_subdir
from models.mixins import (
    EvaluationMixin,
    TestingMixin,
    ToolsMixin,
    AgentMixin,
    ChannelMixin,
    ChatDelegationMixin,
    SettingsMixin,
    ScheduleMixin,
    DashboardMixin,
    ModelsMixin,
    ProvidersMixin,
    WorkplaceMixin,
    PortalMixin,
    SafetyRuleMixin,
    AttachmentsMixin,
    UserMixin,
    TransferJobMixin,
    SystemAlertMixin,
    EscalationMixin,
)


class Database(
    UserMixin,
    SchemaMixin,
    EvaluationMixin,
    TestingMixin,
    ToolsMixin,
    AgentMixin,
    ChannelMixin,
    ChatDelegationMixin,
    SettingsMixin,
    ScheduleMixin,
    DashboardMixin,
    ModelsMixin,
    ProvidersMixin,
    WorkplaceMixin,
    PortalMixin,
    SafetyRuleMixin,
    AttachmentsMixin,
    TransferJobMixin,
    SystemAlertMixin,
    EscalationMixin,
):
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _migrate_db_to_subdir(db_path)
        self._tls = threading.local()
        self._init_tables()
        # Keep one read-only connection alive for the process lifetime.
        # This prevents SQLite from running a WAL checkpoint every time the
        # last per-request connection closes, which causes random stalls.
        self._anchor = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._anchor.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Thread-local persistent connection.

        Each thread gets its own connection (PRAGMAs set once).
        SSE endpoint handlers MUST call db.close() before entering their
        generator loop so the long-lived thread doesn't leak a file descriptor.
        """
        conn = getattr(self._tls, 'conn', None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
            except Exception:
                conn = None

        if conn is None:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=rwc&busy_timeout=10000", uri=True)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.execute("PRAGMA cache_size=-8000")
            conn.execute("PRAGMA mmap_size=268435456")
            self._tls.conn = conn

        with conn:
            yield conn

    def close(self):
        """Close the thread-local connection (call from SSE handlers)."""
        conn = getattr(self._tls, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None


# Re-export chat classes for backward compatibility
from models.chat import AgentChatDB, AgentChatManager, agent_chat_manager  # noqa: F401

# Global singleton
db = Database()
