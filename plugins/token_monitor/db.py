"""
UsageDB — SQLite storage for LLM token usage records.

One row per successful LLM completion, captured from the generic ``llm_usage``
event. All state lives in data/db/plugins/token_monitor.db (WAL mode).
Timestamps are UTC ISO8601, so time-bucketing uses plain string slicing.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_shared_data = os.path.join(BASE_DIR, 'shared', 'data')
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, 'data')
PLUGIN_DB_DIR = os.path.join(_data_root, 'db', 'plugins')
DB_PATH = os.path.join(PLUGIN_DB_DIR, 'token_monitor.db')

_SUBAGENT_RE = re.compile(r'_sub_\d+$')
_EXPLORER_RE = re.compile(r'_explorer_\d+$')
_ORGANIZER_RE = re.compile(r'_organizer_\d+$')
_TEST_RE = re.compile(r'^test_')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsageDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
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
                CREATE TABLE IF NOT EXISTS token_usage (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    source            TEXT NOT NULL DEFAULT 'other',
                    agent_id          TEXT,
                    agent_name        TEXT,
                    session_id        TEXT,
                    model             TEXT,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens      INTEGER NOT NULL DEFAULT 0,
                    estimated         INTEGER NOT NULL DEFAULT 0,
                    provider          TEXT NOT NULL DEFAULT '',
                    cached_tokens     INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
                    usage_details_available INTEGER NOT NULL DEFAULT 0,
                    duration_ms       INTEGER NOT NULL DEFAULT 0,
                    created_at        TEXT NOT NULL
                )
            """)
            # Additive migrations keep existing installations and historical rows intact.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(token_usage)").fetchall()}
            for name, definition in (
                ('provider', "TEXT NOT NULL DEFAULT ''"),
                ('cached_tokens', 'INTEGER NOT NULL DEFAULT 0'),
                ('reasoning_tokens', 'INTEGER NOT NULL DEFAULT 0'),
                ('usage_details_available', 'INTEGER NOT NULL DEFAULT 0'),
            ):
                if name not in columns:
                    conn.execute(f'ALTER TABLE token_usage ADD COLUMN {name} {definition}')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_created ON token_usage(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_created_source_model ON token_usage(created_at, source, provider, model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_agent   ON token_usage(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_source  ON token_usage(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_model   ON token_usage(model)")

    # ── Write ─────────────────────────────────────────────────────────
    def record(self, *, source: str, agent_id: Optional[str], agent_name: Optional[str],
               session_id: Optional[str], model: Optional[str],
               prompt_tokens: int, completion_tokens: int, total_tokens: int,
               estimated: bool = False, duration_ms: int = 0,
               provider: Optional[str] = None, cached_tokens: int = 0,
               reasoning_tokens: int = 0, usage_details_available: bool = False) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO token_usage (source, agent_id, agent_name, session_id, model,
                                         prompt_tokens, completion_tokens, total_tokens,
                                         estimated, provider, cached_tokens, reasoning_tokens,
                                         usage_details_available, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source or 'other', agent_id, agent_name, session_id, model or '',
                  int(prompt_tokens or 0), int(completion_tokens or 0), int(total_tokens or 0),
                  1 if estimated else 0, provider or '', int(cached_tokens or 0),
                  int(reasoning_tokens or 0), 1 if usage_details_available else 0,
                  int(duration_ms or 0), _now()))

    # ── Aggregations ──────────────────────────────────────────────────
    @staticmethod
    def _since_clause(since: Optional[str]) -> tuple:
        return (" WHERE created_at >= ?", (since,)) if since else ("", ())

    def overall_totals(self, since: Optional[str] = None) -> Dict[str, Any]:
        where, params = self._since_clause(since)
        with self._connect() as conn:
            row = conn.execute(f"""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                       COALESCE(SUM(estimated), 0)         AS estimated_calls
                FROM token_usage{where}
            """, params).fetchone()
            return dict(row)

    def snapshot(self, since: Optional[str] = None, *, rollup_subagents: bool = False,
                 bucket: str = 'hour', agent_limit: int = 30) -> Dict[str, Any]:
        """Return all dashboard aggregates from one filtered SQLite snapshot."""
        length = 13 if bucket == 'hour' else 10
        limit = max(1, min(int(agent_limit or 30), 100))
        where, params = self._since_clause(since)
        with self._connect() as conn:
            conn.execute('DROP TABLE IF EXISTS temp.token_monitor_snapshot')
            conn.execute(
                'CREATE TEMP TABLE token_monitor_snapshot AS '
                'SELECT * FROM token_usage' + where, params)
            totals = dict(conn.execute(
                '''SELECT COUNT(*) AS calls,
                          COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                          COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                          COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                          COALESCE(SUM(estimated), 0) AS estimated_calls,
                          COALESCE(SUM(usage_details_available), 0) AS detailed_calls
                   FROM token_monitor_snapshot''').fetchone())

            def grouped(column: str) -> List[Dict[str, Any]]:
                return [dict(row) for row in conn.execute(
                    f'''SELECT {column} AS key, COUNT(*) AS calls,
                               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                               COALESCE(SUM(total_tokens), 0) AS total_tokens,
                               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
                        FROM token_monitor_snapshot GROUP BY {column}
                        ORDER BY total_tokens DESC''').fetchall()]

            sources = grouped('source')
            models = grouped("CASE WHEN provider <> '' THEN provider || '/' || model ELSE model END")
            series = [dict(row) for row in conn.execute(
                f'''SELECT substr(created_at, 1, {length}) AS bucket,
                           COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                           COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                           COALESCE(SUM(total_tokens), 0) AS total_tokens,
                           COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                           COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
                    FROM token_monitor_snapshot GROUP BY bucket ORDER BY bucket''').fetchall()]
            raw_agents = [dict(row) for row in conn.execute(
                '''SELECT agent_id, MAX(agent_name) AS agent_name, COUNT(*) AS calls,
                          COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                          COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                          COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
                   FROM token_monitor_snapshot GROUP BY agent_id ORDER BY total_tokens DESC''').fetchall()]

        fields = ('calls', 'prompt_tokens', 'completion_tokens', 'total_tokens',
                  'cached_tokens', 'reasoning_tokens')
        merged: Dict[str, Dict[str, Any]] = {}
        standalone = []
        for row in raw_agents:
            aid = row.get('agent_id') or ''
            flags = {
                'is_explorer': bool(_EXPLORER_RE.search(aid)),
                'is_organizer': bool(_ORGANIZER_RE.search(aid)),
                'is_test': bool(_TEST_RE.match(aid)),
            }
            if _EXPLORER_RE.search(aid):
                key = _EXPLORER_RE.sub('', aid)
            elif _ORGANIZER_RE.search(aid):
                key = _ORGANIZER_RE.sub('', aid) + '_org'
            elif _TEST_RE.match(aid):
                key = 'test_*'
            elif rollup_subagents and _SUBAGENT_RE.search(aid):
                key = _SUBAGENT_RE.sub('', aid) or aid
            else:
                standalone.append(dict(row, **flags))
                continue
            target = merged.setdefault(key, {
                'agent_id': key, 'agent_name': row.get('agent_name'),
                'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                'total_tokens': 0, 'cached_tokens': 0, 'reasoning_tokens': 0,
                **flags,
            })
            for field in fields:
                target[field] += row.get(field, 0) or 0

        agents = sorted(list(merged.values()) + standalone,
                        key=lambda row: row['total_tokens'], reverse=True)
        agent_total = len(agents)
        if agent_total > limit:
            visible, hidden = agents[:limit], agents[limit:]
            other = {
                'agent_id': 'Other', 'agent_name': 'Other agents',
                'is_explorer': False, 'is_organizer': False, 'is_test': False,
            }
            for field in fields:
                other[field] = sum(row[field] for row in hidden)
            agents = visible + [other]
        return {
            'totals': totals,
            'sources': sources,
            'models': models,
            'agents': agents,
            'agent_total': agent_total,
            'series': series,
            'bucket': bucket,
        }

    def by_agent(self, since: Optional[str] = None, rollup_subagents: bool = False) -> List[Dict[str, Any]]:
        where, params = self._since_clause(since)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(f"""
                SELECT agent_id,
                       MAX(agent_name) AS agent_name,
                       COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM token_usage{where}
                GROUP BY agent_id
                ORDER BY total_tokens DESC
            """, params).fetchall()]
        for r in rows:
            aid = r.get('agent_id') or ''
            r['is_explorer'] = bool(_EXPLORER_RE.search(aid))
            r['is_organizer'] = bool(_ORGANIZER_RE.search(aid))
            r['is_test'] = bool(_TEST_RE.match(aid))
        merged: Dict[str, Dict[str, Any]] = {}
        standalone: List[Dict[str, Any]] = []
        for r in rows:
            aid = r.get('agent_id') or ''
            if _EXPLORER_RE.search(aid):
                delegator = _EXPLORER_RE.sub('', aid)
                acc = merged.setdefault(delegator, {
                    'agent_id': delegator,
                    'agent_name': r.get('agent_name'),
                    'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                    'total_tokens': 0, 'is_explorer': True, 'is_organizer': False,
                    'is_test': False,
                })
                for f in ('calls', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
                    acc[f] += r.get(f, 0)
            elif _ORGANIZER_RE.search(aid):
                delegator = _ORGANIZER_RE.sub('', aid)
                acc = merged.setdefault(delegator + '_org', {
                    'agent_id': delegator,
                    'agent_name': r.get('agent_name'),
                    'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                    'total_tokens': 0, 'is_explorer': False, 'is_organizer': True,
                    'is_test': False,
                })
                for f in ('calls', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
                    acc[f] += r.get(f, 0)
            elif _TEST_RE.match(aid):
                # Roll all test_* agents into a single "test_*" group
                acc = merged.setdefault('test_*', {
                    'agent_id': 'test_*',
                    'agent_name': 'test agents',
                    'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                    'total_tokens': 0, 'is_explorer': False, 'is_organizer': False,
                    'is_test': True,
                })
                for f in ('calls', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
                    acc[f] += r.get(f, 0)
            elif rollup_subagents and _SUBAGENT_RE.search(aid):
                # Roll regular sub-agent into parent: angga_sub_1 -> angga
                parent = _SUBAGENT_RE.sub('', aid) or aid
                acc = merged.setdefault(parent, {
                    'agent_id': parent,
                    'agent_name': r.get('agent_name'),
                    'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                    'total_tokens': 0, 'is_explorer': False, 'is_organizer': False,
                    'is_test': False,
                })
                for f in ('calls', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
                    acc[f] += r.get(f, 0)
            else:
                standalone.append(r)
        result = list(merged.values()) + standalone
        return sorted(result, key=lambda x: x['total_tokens'], reverse=True)

    def _group_by(self, column: str, since: Optional[str]) -> List[Dict[str, Any]]:
        where, params = self._since_clause(since)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT {column} AS key,
                       COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM token_usage{where}
                GROUP BY {column}
                ORDER BY total_tokens DESC
            """, params).fetchall()]

    def by_source(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._group_by('source', since)

    def by_model(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._group_by('model', since)

    def series(self, since: Optional[str] = None, bucket: str = 'hour') -> List[Dict[str, Any]]:
        # created_at is UTC ISO8601 → slice the prefix for the bucket key.
        # 'YYYY-MM-DDTHH' (13 chars) for hourly, 'YYYY-MM-DD' (10) for daily.
        length = 13 if bucket == 'hour' else 10
        where, params = self._since_clause(since)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT substr(created_at, 1, {length}) AS bucket,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM token_usage{where}
                GROUP BY bucket
                ORDER BY bucket ASC
            """, params).fetchall()]


# Singleton
usage_db = UsageDB()
