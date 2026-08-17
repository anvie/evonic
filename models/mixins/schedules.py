import sqlite3
import json
from datetime import datetime
from typing import Dict, Any, List, Optional


class ScheduleMixin:
    """Schedule CRUD operations. Requires self._connect() from the host class."""

    def create_schedule(self, schedule_id: str, name: str, owner_type: str,
                        owner_id: str, trigger_type: str, trigger_config: dict,
                        action_type: str, action_config: dict,
                        max_runs: int = None, metadata: dict = None) -> dict:
        now = datetime.now().isoformat()
        row = {
            'id': schedule_id, 'name': name, 'owner_type': owner_type,
            'owner_id': owner_id, 'trigger_type': trigger_type,
            'trigger_config': json.dumps(trigger_config),
            'action_type': action_type, 'action_config': json.dumps(action_config),
            'enabled': 1, 'created_at': now, 'run_count': 0,
            'max_runs': max_runs,
            'metadata': json.dumps(metadata) if metadata else None,
        }
        with self._connect() as conn:
            cols = ', '.join(row.keys())
            placeholders = ', '.join(['?'] * len(row))
            conn.execute(f"INSERT INTO schedules ({cols}) VALUES ({placeholders})",
                         list(row.values()))
            conn.commit()
        return row

    def get_schedule(self, schedule_id: str) -> Optional[Dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT id, name, owner_type, owner_id, trigger_type, trigger_config, action_type, action_config, enabled, created_at, next_run_at, last_run_at, run_count, max_runs, metadata FROM schedules WHERE id = ?",
                               (schedule_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['trigger_config'] = json.loads(d['trigger_config'])
            d['action_config'] = json.loads(d['action_config'])
            if d.get('metadata'):
                d['metadata'] = json.loads(d['metadata'])
            return d

    def get_schedules(self, owner_type: str = None, owner_id: str = None,
                      enabled_only: bool = False) -> List[Dict]:
        clauses = ["1=1"]
        params = []
        if owner_type:
            clauses.append("owner_type = ?"); params.append(owner_type)
        if owner_id:
            clauses.append("owner_id = ?"); params.append(owner_id)
        if enabled_only:
            clauses.append("enabled = 1")
        # Column names are compile-time constants; values use ? placeholders.
        where = " AND ".join(clauses)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, owner_type, owner_id, trigger_type, trigger_config, action_type, action_config, enabled, created_at, next_run_at, last_run_at, run_count, max_runs, metadata FROM schedules WHERE " + where + " ORDER BY created_at DESC", params
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d['trigger_config'] = json.loads(d['trigger_config'])
                d['action_config'] = json.loads(d['action_config'])
                if d.get('metadata'):
                    d['metadata'] = json.loads(d['metadata'])
                result.append(d)
            return result

    def update_schedule(self, schedule_id: str, **kwargs) -> bool:
        allowed = {'name', 'enabled', 'next_run_at', 'last_run_at',
                    'run_count', 'max_runs', 'metadata', 'trigger_config', 'action_config',
                    'trigger_type', 'action_type'}
        updates = {}
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k in ('trigger_config', 'action_config', 'metadata') and isinstance(v, (dict, list)):
                v = json.dumps(v)
            updates[k] = v
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with self._connect() as conn:
            conn.execute(f"UPDATE schedules SET {set_clause} WHERE id = ?",
                         list(updates.values()) + [schedule_id])
            conn.commit()
        return True

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Schedule execution logs
    # ------------------------------------------------------------------

    def create_schedule_log(self, log_id: str, schedule_id: str, executed_at: str,
                            duration_ms: int, status: str, action_type: str,
                            action_summary: str = None, error_message: str = None,
                            action_output: str = None) -> dict:
        row = {
            'id': log_id, 'schedule_id': schedule_id, 'executed_at': executed_at,
            'duration_ms': duration_ms, 'status': status, 'error_message': error_message,
            'action_type': action_type, 'action_summary': action_summary,
            'action_output': action_output,
        }
        with self._connect() as conn:
            cols = ', '.join(row.keys())
            placeholders = ', '.join(['?'] * len(row))
            conn.execute(f"INSERT INTO schedule_logs ({cols}) VALUES ({placeholders})",
                         list(row.values()))
            conn.commit()
        return row

    def update_schedule_log(self, log_id: str, **kwargs) -> bool:
        """Update fields on an existing schedule log (e.g. status after running)."""
        allowed = {'status', 'duration_ms', 'error_message',
                    'action_summary', 'action_output'}
        updates = {}
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            updates[k] = v
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with self._connect() as conn:
            conn.execute(f"UPDATE schedule_logs SET {set_clause} WHERE id = ?",
                          list(updates.values()) + [log_id])
            conn.commit()
        return True

    def get_schedule_logs(self, schedule_id: str, limit: int = 50, offset: int = 0) -> List[Dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, schedule_id, executed_at, duration_ms, status, error_message, action_type, action_summary, action_output FROM schedule_logs WHERE schedule_id = ? "
                "ORDER BY executed_at DESC LIMIT ? OFFSET ?",
                (schedule_id, limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_schedule_logs(self, schedule_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM schedule_logs WHERE schedule_id = ?", (schedule_id,))
            conn.commit()
            return cursor.rowcount

    def cleanup_old_schedule_logs(self, schedule_id: str, keep: int = 100):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM schedule_logs WHERE schedule_id = ? AND id NOT IN ("
                "  SELECT id FROM schedule_logs WHERE schedule_id = ? "
                "  ORDER BY executed_at DESC LIMIT ?"
                ")",
                (schedule_id, schedule_id, keep)
            )
            conn.commit()
