"""Persistence helpers for bidirectional user escalations."""

import json
import sqlite3
import time
from typing import Any, Dict, Optional


class EscalationMixin:
    """Store and atomically consume pending human escalation correlations."""

    def create_user_escalation(
        self,
        escalation_id: str,
        requesting_agent_id: str,
        requesting_session_id: str,
        originating_agent_id: str,
        originating_session_id: str,
        delivery_session_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
        expires_in_seconds: int = 86400,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            # A plain reply cannot disambiguate multiple outstanding questions in
            # one session. The newest escalation therefore supersedes older ones.
            conn.execute(
                """UPDATE user_escalations
                   SET status = 'cancelled', updated_at = ?
                   WHERE originating_session_id = ?
                     AND status IN ('delivering', 'pending')""",
                (now, originating_session_id),
            )
            conn.execute(
                """INSERT INTO user_escalations (
                       id, requesting_agent_id, requesting_session_id,
                       originating_agent_id, originating_session_id,
                       delivery_session_id, external_user_id, channel_id,
                       status, metadata, created_at, updated_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'delivering', ?, ?, ?, ?)""",
                (
                    escalation_id,
                    requesting_agent_id,
                    requesting_session_id,
                    originating_agent_id,
                    originating_session_id,
                    delivery_session_id,
                    external_user_id,
                    channel_id,
                    json.dumps(metadata or {}),
                    now,
                    now,
                    now + max(1, expires_in_seconds),
                ),
            )

    def mark_user_escalation_delivered(self, escalation_id: str) -> bool:
        now = time.time()
        with self._connect() as conn:
            updated = conn.execute(
                """UPDATE user_escalations
                   SET status = 'pending', updated_at = ?
                   WHERE id = ? AND status = 'delivering'""",
                (now, escalation_id),
            )
            return updated.rowcount == 1

    def cancel_user_escalation(self, escalation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE user_escalations
                   SET status = 'cancelled', updated_at = ?
                   WHERE id = ? AND status IN ('delivering', 'pending')""",
                (time.time(), escalation_id),
            )

    def consume_pending_user_escalation(
        self, originating_session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Atomically mark and return the newest live escalation for a session."""
        now = time.time()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # IMMEDIATE obtains the write reservation before selection so two
            # inbound threads cannot both read the same pending correlation.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE user_escalations
                   SET status = 'expired', updated_at = ?
                   WHERE status = 'pending' AND expires_at <= ?""",
                (now, now),
            )
            row = conn.execute(
                """SELECT * FROM user_escalations
                   WHERE originating_session_id = ? AND status = 'pending'
                     AND expires_at > ?
                   ORDER BY created_at DESC LIMIT 1""",
                (originating_session_id, now),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE user_escalations
                   SET status = 'answered', updated_at = ?, answered_at = ?
                   WHERE id = ?""",
                (now, now, row['id']),
            )
            result = dict(row)
            try:
                result['metadata'] = json.loads(result.get('metadata') or '{}')
            except (TypeError, json.JSONDecodeError):
                result['metadata'] = {}
            result['status'] = 'answered'
            result['answered_at'] = now
            return result
