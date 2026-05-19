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
    WorkplaceMixin,
    PortalMixin,
    SafetyRuleMixin,
    AttachmentsMixin,
    UserMixin,
    TransferJobMixin,
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
    WorkplaceMixin,
    PortalMixin,
    SafetyRuleMixin,
    AttachmentsMixin,
    TransferJobMixin,
):
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _migrate_db_to_subdir(db_path)
        self._init_tables()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that returns a SQLite connection with WAL mode and busy timeout.
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


# Re-export chat classes for backward compatibility
from models.chat import AgentChatDB, AgentChatManager, agent_chat_manager  # noqa: F401

# Global singleton
db = Database()
