"""Background jobs an agent detached (tmux/screen/nohup/guard wrapper), persisted so they outlive a restart.

The in-memory registry in ``backend/agent_runtime/background_jobs.py`` writes through to this table and reloads it
on first use.
"""

import json
from typing import Any, Dict, Iterable, List

_COLUMNS = ('job_id', 'session_id', 'session_name', 'log_file', 'pid_file', 'command', 'kind', 'pgrep_pattern',
            'status', 'exit_code', 'started_at', 'finished_at')


class BackgroundJobsMixin:
    def upsert_background_job(self, job: Dict[str, Any]) -> None:
        values = [job.get(c) for c in _COLUMNS]
        ctx = job.get('backend_ctx')
        cols = ', '.join(_COLUMNS) + ', backend_ctx'
        marks = ', '.join('?' * (len(_COLUMNS) + 1))
        updates = ', '.join(f'{c} = excluded.{c}' for c in _COLUMNS[1:] + ('backend_ctx',))
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO background_jobs ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(job_id) DO UPDATE SET {updates}",
                (*values, json.dumps(ctx) if ctx is not None else None))

    def delete_background_jobs(self, job_ids: Iterable[str]) -> None:
        job_ids = list(job_ids)
        if not job_ids:
            return
        with self._connect() as conn:
            conn.executemany("DELETE FROM background_jobs WHERE job_id = ?", [(j,) for j in job_ids])

    def list_background_jobs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)}, backend_ctx FROM background_jobs").fetchall()
        out = []
        for r in rows:
            job = dict(zip(_COLUMNS, r[:-1]))
            try:
                job['backend_ctx'] = json.loads(r[-1]) if r[-1] else None
            except ValueError:
                job['backend_ctx'] = None
            out.append(job)
        return out
