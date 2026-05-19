import sqlite3
import uuid
from typing import Dict, Any, List, Optional


class TransferJobMixin:
    """Transfer job CRUD. Requires self._connect() from the host class."""

    def create_transfer_job(self, data: Dict[str, Any]) -> str:
        job_id = data.get("id") or uuid.uuid4().hex[:12]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO transfer_jobs (id, agent_id, session_id, source_path,
                    dest_path, source_backend_type, dest_backend_type, total_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    data["agent_id"],
                    data["session_id"],
                    data["source_path"],
                    data["dest_path"],
                    data["source_backend_type"],
                    data["dest_backend_type"],
                    data.get("total_bytes", 0),
                ),
            )
            conn.commit()
        return job_id

    def get_transfer_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_transfer_job(self, job_id: str, data: Dict[str, Any]) -> bool:
        allowed = {"status", "bytes_transferred", "error_msg", "completed_at"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE transfer_jobs SET {set_clause} WHERE id = ?",
                values,
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_agent_transfer_jobs(self, agent_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM transfer_jobs WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]
