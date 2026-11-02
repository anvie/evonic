"""Per-session flags: a session that must never become memory, and one that expires.

Used for disposable sessions (a plugin or system briefing an agent): ``save_memory=False`` keeps the exchange out
of the knowledge the memory pipeline writes, and ``expires_at`` lets the session be removed after a while.
"""

import time
from typing import Dict, List, Optional


class SessionFlagsMixin:
    def set_session_flags(self, session_id: str, agent_id: str, *, save_memory: bool = True,
                          expires_at: Optional[float] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_flags (session_id, agent_id, no_memory, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "no_memory = MAX(no_memory, excluded.no_memory), "
                "expires_at = COALESCE(excluded.expires_at, expires_at)",
                (session_id, agent_id, 0 if save_memory else 1, expires_at, time.time()))

    def get_session_flags(self, session_id: str) -> Dict[str, object]:
        with self._connect() as conn:
            row = conn.execute("SELECT no_memory, expires_at FROM session_flags WHERE session_id = ?",
                               (session_id,)).fetchone()
        return {'no_memory': bool(row[0]), 'expires_at': row[1]} if row else {'no_memory': False, 'expires_at': None}

    def expired_session_flags(self, now: Optional[float] = None) -> List[Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT session_id, agent_id FROM session_flags WHERE expires_at IS NOT NULL "
                                "AND expires_at <= ?", (now if now is not None else time.time(),)).fetchall()
        return [{'session_id': r[0], 'agent_id': r[1]} for r in rows]

    def drop_session_flags(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM session_flags WHERE session_id = ?", (session_id,))
