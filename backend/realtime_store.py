"""Durable realtime journal and active-turn projection.

The journal is the source of truth for browser replay.  The existing
``EventStream`` remains the in-process plugin bus; it records normalized public
events here before dispatching asynchronous listeners.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
import config


log = logging.getLogger(__name__)

RETENTION_MS = 24 * 60 * 60 * 1000
_CLEANUP_INTERVAL_MS = 60 * 60 * 1000
_ATTACHMENT_KEYS = frozenset({
    'attachment_id', 'filename', 'mime_type', 'size_bytes', 'is_image',
})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _public_attachment(value):
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in _ATTACHMENT_KEYS if key in value}


def _public_metadata(value) -> dict:
    """Remove browser-local URLs and backend paths from SSE payloads."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key in {'image_url', 'audio_url', 'video_url', 'file_path', 'path'}:
            continue
        if key == 'attachment_info':
            result[key] = _public_attachment(item)
        elif key == 'attachment_infos' and isinstance(item, list):
            result[key] = [clean for clean in map(_public_attachment, item) if clean]
        else:
            result[key] = item
    return result


def _public_message(value: str) -> str:
    # Attachment markers are part of the model prompt, but local paths do not
    # belong in browser broadcasts.
    return re.sub(
        r'(\[Attached:[^\]]*?)\s+path=[^\]]+(\])', r'\1\2', value or '',
    )


class RealtimeStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self._tls = threading.local()
        self._schema_lock = threading.Lock()
        self._schema_paths: set[str] = set()
        self._condition = threading.Condition()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup_ms = 0
        self._recovery_lock = threading.Lock()
        self._recovered: set[tuple[int, str]] = set()

    @contextmanager
    def _connect(self):
        db_path = self._resolve_db_path()
        self._ensure_schema(db_path)
        conn = getattr(self._tls, 'conn', None)
        if conn is not None and (
                getattr(self._tls, 'db_path', None) != db_path
                or getattr(self._tls, 'pid', None) != os.getpid()):
            conn.close()
            conn = None
        if conn is None:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(
                f'file:{db_path}?mode=rwc&busy_timeout=10000',
                uri=True, timeout=10,
            )
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._tls.conn = conn
            self._tls.db_path = db_path
            self._tls.pid = os.getpid()
        with conn:
            yield conn

    def _resolve_db_path(self) -> str:
        if self.db_path:
            return os.path.abspath(self.db_path)
        try:
            from models.db import db
            return os.path.abspath(db.db_path)
        except Exception:
            return os.path.abspath(config.DB_PATH)

    def _ensure_schema(self, db_path: str):
        if db_path in self._schema_paths:
            return
        with self._schema_lock:
            if db_path in self._schema_paths:
                return
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS realtime_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        occurred_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER,
                        channel TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        agent_id TEXT,
                        session_id TEXT,
                        workplace_id TEXT,
                        turn_id TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_realtime_session_id
                        ON realtime_events(session_id, id);
                    CREATE INDEX IF NOT EXISTS idx_realtime_channel_id
                        ON realtime_events(channel, id);
                    CREATE INDEX IF NOT EXISTS idx_realtime_workplace_id
                        ON realtime_events(workplace_id, id);
                    CREATE INDEX IF NOT EXISTS idx_realtime_turn_id
                        ON realtime_events(turn_id, id);
                    CREATE INDEX IF NOT EXISTS idx_realtime_expiry
                        ON realtime_events(expires_at_ms);

                    CREATE TABLE IF NOT EXISTS active_turns (
                        turn_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('queued', 'running')),
                        queued_at_ms INTEGER NOT NULL,
                        started_at_ms INTEGER,
                        updated_at_ms INTEGER NOT NULL,
                        owner_pid INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_active_turns_agent
                        ON active_turns(agent_id, updated_at_ms);
                    CREATE INDEX IF NOT EXISTS idx_active_turns_session
                        ON active_turns(session_id, updated_at_ms);
                """)
                conn.commit()
            finally:
                conn.close()
            self._schema_paths.add(db_path)

    def close(self):
        conn = getattr(self._tls, 'conn', None)
        if conn is not None:
            conn.close()
            self._tls.conn = None
            self._tls.db_path = None
            self._tls.pid = None

    def high_water(self) -> int:
        with self._connect() as conn:
            row = conn.execute('SELECT COALESCE(MAX(id), 0) AS id FROM realtime_events').fetchone()
        return int(row['id'])

    def last_session_clear_id(self, session_id: str, up_to_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT COALESCE(MAX(id), 0) AS id FROM realtime_events
                WHERE session_id = ? AND event_type = 'session_clear'
                    AND id <= ?
                    AND (expires_at_ms IS NULL OR expires_at_ms > ?)
            """, (session_id, up_to_id, _now_ms())).fetchone()
        return int(row['id'])

    def publish(self, channel: str, event_type: str, payload: dict, *,
                agent_id: str | None = None, session_id: str | None = None,
                workplace_id: str | None = None, turn_id: str | None = None,
                occurred_at_ms: int | None = None) -> int:
        now = occurred_at_ms or _now_ms()
        expires_at = None if turn_id else now + RETENTION_MS
        body = json.dumps(payload, separators=(',', ':'), default=_json_default)
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO realtime_events (
                    occurred_at_ms, expires_at_ms, channel, event_type,
                    agent_id, session_id, workplace_id, turn_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (now, expires_at, channel, event_type, agent_id, session_id,
                  workplace_id, turn_id, body))
            event_id = int(cursor.lastrowid)
        with self._condition:
            self._condition.notify_all()
        self._maybe_cleanup(now)
        return event_id

    def events_after(self, after_id: int, channels: set[str], *,
                     session_id: str | None = None,
                     agent_id: str | None = None,
                     workplace_id: str | None = None,
                     up_to_id: int | None = None,
                     active_only: bool = False,
                     limit: int = 500) -> list[dict]:
        clauses = ['e.id > ?', '(e.expires_at_ms IS NULL OR e.expires_at_ms > ?)']
        params: list = [after_id, _now_ms()]
        if up_to_id is not None:
            clauses.append('e.id <= ?')
            params.append(up_to_id)
        if active_only:
            clauses.append('EXISTS (SELECT 1 FROM active_turns a WHERE a.turn_id = e.turn_id)')

        channel_clauses = []
        global_channels = channels - {'chat', 'workplace'}
        if global_channels:
            placeholders = ','.join('?' for _ in global_channels)
            channel_clauses.append(f'e.channel IN ({placeholders})')
            params.extend(sorted(global_channels))
        if 'chat' in channels and session_id:
            # Session-scoped approval/status events share the chat stream so a
            # chat-only consumer still sees queued/busy/approval state.
            channel_clauses.append(
                "(e.session_id = ? AND e.channel IN ('chat','status','approvals'))"
            )
            params.append(session_id)
        if 'workplace' in channels and workplace_id:
            channel_clauses.append("(e.channel = 'workplace' AND e.workplace_id = ?)")
            params.append(workplace_id)
        if not channel_clauses:
            return []
        clauses.append('(' + ' OR '.join(channel_clauses) + ')')

        if session_id and 'chat' in channels:
            clauses.append("(e.channel != 'chat' OR e.session_id = ?)")
            params.append(session_id)
        if agent_id and 'chat' in channels:
            clauses.append("(e.channel != 'chat' OR e.agent_id IS NULL OR e.agent_id = ?)")
            params.append(agent_id)

        params.append(limit)
        sql = f"""
            SELECT e.* FROM realtime_events e
            WHERE {' AND '.join(clauses)}
            ORDER BY e.id ASC LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_event(row) for row in rows]

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> dict:
        payload = json.loads(row['payload_json'])
        payload.update({
            'event_id': row['id'],
            'seq': row['id'],
            'timestamp': row['occurred_at_ms'],
            'channel': row['channel'],
        })
        if row['agent_id']:
            payload.setdefault('agent_id', row['agent_id'])
        if row['session_id']:
            payload.setdefault('session_id', row['session_id'])
        if row['workplace_id']:
            payload.setdefault('workplace_id', row['workplace_id'])
        if row['turn_id']:
            payload['turn_id'] = row['turn_id']
        return {
            'id': row['id'],
            'timestamp': row['occurred_at_ms'],
            'channel': row['channel'],
            'event': row['event_type'],
            'agent_id': row['agent_id'],
            'session_id': row['session_id'],
            'workplace_id': row['workplace_id'],
            'turn_id': row['turn_id'],
            'data': payload,
        }

    def wait_for_events(self, after_id: int, timeout: float = 15) -> None:
        with self._condition:
            self._condition.wait_for(
                lambda: self.high_water() > after_id,
                timeout,
            )

    def queue_turn(self, agent_id: str, session_id: str,
                   turn_id: str | None = None) -> tuple[str, bool]:
        """Create queued state, reusing an existing debounced queue entry."""
        now = _now_ms()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute("""
                SELECT turn_id FROM active_turns
                WHERE session_id = ? AND state = 'queued'
                ORDER BY queued_at_ms ASC LIMIT 1
            """, (session_id,)).fetchone()
            if existing:
                return existing['turn_id'], False
            turn_id = turn_id or uuid.uuid4().hex
            conn.execute("""
                INSERT INTO active_turns (
                    turn_id, agent_id, session_id, state, queued_at_ms,
                    started_at_ms, updated_at_ms, owner_pid
                ) VALUES (?, ?, ?, 'queued', ?, NULL, ?, ?)
            """, (turn_id, agent_id, session_id, now, now, os.getpid()))
        active = self.busy_agents().get(agent_id, {})
        self.publish('status', 'turn_queued', {
            'agent_id': agent_id,
            'session_id': session_id,
            'busy': True,
            'state': 'queued',
        }, agent_id=agent_id, session_id=session_id, turn_id=turn_id,
           occurred_at_ms=now)
        self.publish('status', 'agent_busy_changed', {
            'agent_id': agent_id,
            'session_id': active.get('session_id', session_id),
            'session_ids': active.get('session_ids', [session_id]),
            'active_count': active.get('active_count', 1),
            'busy': True,
            'state': 'queued',
        }, agent_id=agent_id, session_id=session_id, turn_id=turn_id,
           occurred_at_ms=now)
        return turn_id, True

    def start_turn(self, turn_id: str) -> dict | None:
        now = _now_ms()
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE active_turns SET state = 'running', started_at_ms = ?,
                    updated_at_ms = ?, owner_pid = ?
                WHERE turn_id = ? AND state = 'queued'
            """, (now, now, os.getpid(), turn_id))
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                'SELECT * FROM active_turns WHERE turn_id = ?', (turn_id,),
            ).fetchone()
        with self._condition:
            self._condition.notify_all()
        return dict(row) if row else None

    def cancel_queued_turns(self, session_id: str,
                            turn_id: str | None = None) -> list[dict]:
        """Atomically remove queued work so a racing worker cannot start it."""
        now = _now_ms()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            clauses = ["session_id = ?", "state = 'queued'"]
            params = [session_id]
            if turn_id:
                clauses.append('turn_id = ?')
                params.append(turn_id)
            rows = conn.execute(
                f"SELECT * FROM active_turns WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            turn_ids = [row['turn_id'] for row in rows]
            if turn_ids:
                placeholders = ','.join('?' for _ in turn_ids)
                conn.execute(
                    f'DELETE FROM active_turns WHERE turn_id IN ({placeholders}) '
                    "AND state = 'queued'",
                    turn_ids,
                )
                conn.execute(
                    f'UPDATE realtime_events SET expires_at_ms = ? '
                    f'WHERE turn_id IN ({placeholders})',
                    [now + RETENTION_MS, *turn_ids],
                )
        if rows:
            with self._condition:
                self._condition.notify_all()
        return [dict(row) for row in rows]

    def finish_turn(self, turn_id: str) -> None:
        now = _now_ms()
        with self._connect() as conn:
            conn.execute(
                'UPDATE realtime_events SET expires_at_ms = ? WHERE turn_id = ?',
                (now + RETENTION_MS, turn_id),
            )
            conn.execute('DELETE FROM active_turns WHERE turn_id = ?', (turn_id,))
        with self._condition:
            self._condition.notify_all()

    def current_turn_id(self, session_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT turn_id FROM active_turns WHERE session_id = ?
                ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END,
                         updated_at_ms DESC LIMIT 1
            """, (session_id,)).fetchone()
        return row['turn_id'] if row else None

    def active_turns(self, *, agent_id: str | None = None,
                     session_id: str | None = None) -> list[dict]:
        clauses = []
        params = []
        if agent_id:
            clauses.append('agent_id = ?')
            params.append(agent_id)
        if session_id:
            clauses.append('session_id = ?')
            params.append(session_id)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM active_turns' + where + ' ORDER BY queued_at_ms',
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def busy_agents(self) -> dict:
        now = _now_ms()
        result = {}
        for turn in self.active_turns():
            entry = result.setdefault(turn['agent_id'], {
                'session_id': turn['session_id'],
                'session_ids': [],
                'active_count': 0,
                'started_at': (turn['started_at_ms'] or turn['queued_at_ms']) / 1000,
                'state': turn['state'],
            })
            if turn['session_id'] not in entry['session_ids']:
                entry['session_ids'].append(turn['session_id'])
            entry['active_count'] += 1
            if turn['state'] == 'running':
                entry['state'] = 'running'
                entry['session_id'] = turn['session_id']
                entry['started_at'] = (turn['started_at_ms'] or turn['queued_at_ms']) / 1000
        for entry in result.values():
            entry['elapsed'] = round(now / 1000 - entry['started_at'], 1)
        return result

    def purge_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                DELETE FROM realtime_events
                WHERE session_id = ? AND (
                    turn_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM active_turns a
                        WHERE a.turn_id = realtime_events.turn_id
                    )
                )
            """, (session_id,))
        with self._condition:
            self._condition.notify_all()

    def purge_all(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                DELETE FROM realtime_events
                WHERE turn_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM active_turns a
                    WHERE a.turn_id = realtime_events.turn_id
                )
            """)
        with self._condition:
            self._condition.notify_all()

    def interrupt_stale_turns(self) -> list[dict]:
        key = (os.getpid(), self._resolve_db_path())
        with self._recovery_lock:
            if key in self._recovered:
                return []
            self._maybe_cleanup(_now_ms())
            stale = self.active_turns()
            for turn in stale:
                payload = {
                    'agent_id': turn['agent_id'],
                    'session_id': turn['session_id'],
                    'response': '',
                    'interrupted': True,
                    'is_error': True,
                    'reason': 'server_restart',
                }
                self.publish('chat', 'done', payload,
                             agent_id=turn['agent_id'], session_id=turn['session_id'],
                             turn_id=turn['turn_id'])
                self.finish_turn(turn['turn_id'])
                remaining = self.busy_agents().get(turn['agent_id'])
                self.publish('status', 'agent_busy_changed', {
                    'agent_id': turn['agent_id'],
                    'session_id': remaining.get('session_id', turn['session_id']) if remaining else turn['session_id'],
                    'session_ids': remaining.get('session_ids', []) if remaining else [],
                    'active_count': remaining.get('active_count', 0) if remaining else 0,
                    'state': remaining.get('state', 'idle') if remaining else 'idle',
                    'busy': bool(remaining),
                    'interrupted': True,
                }, agent_id=turn['agent_id'], session_id=turn['session_id'])
            self._recovered.add(key)
            return stale

    def _maybe_cleanup(self, now_ms: int) -> None:
        if now_ms - self._last_cleanup_ms < _CLEANUP_INTERVAL_MS:
            return
        if not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            if now_ms - self._last_cleanup_ms < _CLEANUP_INTERVAL_MS:
                return
            with self._connect() as conn:
                conn.execute(
                    'DELETE FROM realtime_events WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= ?',
                    (now_ms,),
                )
            self._last_cleanup_ms = now_ms
        finally:
            self._cleanup_lock.release()

realtime_store = RealtimeStore()


def record_internal_event(event_name: str, data: dict) -> list[int]:
    """Normalize one raw runtime event into durable public SSE events."""
    session_id = data.get('session_id') or None
    agent_id = data.get('agent_id') or None
    workplace_id = data.get('workplace_id') or None
    turn_id = (
        data.get('turn_id') if 'turn_id' in data
        else (realtime_store.current_turn_id(session_id) if session_id else None)
    )
    metadata = _public_metadata(data.get('metadata'))
    specs: list[tuple[str, str, dict]] = []

    if event_name == 'turn_begin':
        specs.append(('chat', 'turn_begin', {'ts': data.get('ts', _now_ms())}))
    elif event_name == 'llm_thinking':
        specs.append(('chat', 'thinking', {'content': data.get('thinking', '')}))
    elif event_name == 'tool_call_started':
        specs.append(('chat', 'tool_call_started', {
            'tool': data.get('tool_name', ''), 'args': data.get('tool_args', {}),
            'param_types': data.get('param_types', {}),
        }))
    elif event_name == 'tool_executed':
        specs.append(('chat', 'tool_executed', {
            'tool': data.get('tool_name', ''), 'args': data.get('tool_args', {}),
            'result': data.get('tool_result', {}), 'error': data.get('has_error', False),
        }))
    elif event_name in {'state:changed', 'tasks:auto_transition', 'tasks:stale'}:
        keys = ('mode', 'plan_file', 'tasks', 'loaded_skills', 'task_ids')
        specs.append(('chat', event_name, {key: data[key] for key in keys if key in data}))
    elif event_name == 'llm_response_chunk':
        specs.append(('chat', 'response_chunk', {
            'content': data.get('content', ''), 'is_final': data.get('is_final', False),
            'send_as_message': data.get('send_as_message', False),
        }))
    elif event_name == 'turn_complete':
        done = {
            'thinking_duration': data.get('thinking_duration'),
            'response': data.get('response', ''),
            'slash_command': data.get('slash_command', False),
            'attachment_info': _public_attachment(data.get('attachment_info')),
            'message_id': data.get('message_id'),
            'is_error': data.get('is_error', False),
            'interrupted': data.get('interrupted', False),
            'reason': data.get('reason'),
        }
        specs.append(('chat', 'done', done))
        if data.get('response') and not data.get('is_error'):
            specs.append(('status', 'agent_turn_complete', {
                'agent_id': agent_id or '',
                'agent_name': data.get('agent_name', ''),
                'response': data.get('response', ''),
                'session_id': session_id or '',
                'external_user_id': data.get('external_user_id', ''),
            }))
    elif event_name == 'agent_busy_changed':
        specs.append(('status', 'agent_busy_changed', {
            'agent_id': agent_id or '', 'busy': data.get('busy', False),
            'session_id': session_id or '',
            'session_ids': data.get('session_ids', [session_id] if session_id else []),
            'active_count': data.get('active_count', 1 if data.get('busy') else 0),
            'state': data.get('state', 'running' if data.get('busy') else 'idle'),
        }))
    elif event_name == 'message_received':
        specs.append(('chat', 'message_received', {
            'message': _public_message(data.get('message', '')),
            'content': _public_message(data.get('message', '')),
            'role': data.get('role', 'user'),
            'message_id': data.get('message_id'),
            'client_message_id': data.get('client_message_id') or metadata.get('client_message_id'),
            'metadata': metadata,
            'external_user_id': data.get('external_user_id', ''),
            'sender': data.get('sender') or data.get('external_user_id', ''),
        }))
    elif event_name in {'message_injected', 'message_injection_applied'}:
        specs.append(('chat', event_name, {
            'message': data.get('message', ''), 'content': data.get('content', ''),
            'count': data.get('count', 1),
        }))
    elif event_name == 'llm_retry':
        specs.append(('chat', 'retry', {
            'retry_count': data.get('retry_count', 0),
            'max_retries': data.get('max_retries', 0),
            'error_type': data.get('error_type', ''),
            'message': data.get('user_message', ''),
        }))
    elif event_name in {'approval_required', 'approval_resolved'}:
        if event_name == 'approval_required':
            payload = {
                'approval_id': data.get('approval_id', ''),
                'agent_id': agent_id or '',
                'source_agent_id': data.get('source_agent_id', ''),
                'source_agent_name': data.get('source_agent_name', ''),
                'tool': data.get('tool_name', ''), 'args': data.get('tool_args', {}),
                'approval_info': data.get('approval_info', {}),
                'reasons': data.get('reasons', []), 'score': data.get('score'),
            }
        else:
            payload = {
                'approval_id': data.get('approval_id', ''),
                'decision': data.get('decision', ''),
                'timed_out': data.get('timed_out', False),
            }
        specs.append(('approvals', event_name, payload))
    elif event_name in {'whatsapp_bridge_status', 'panel_updated'}:
        specs.append(('status', event_name, {
            key: data.get(key, '') for key in ('agent_id', 'channel_id', 'status')
            if key in data
        }))
    elif event_name in {
        'connector_connected', 'connector_disconnected', 'connector_paired',
        'workplace_status_changed',
    }:
        specs.append(('workplace', event_name, {
            key: value for key, value in data.items() if not key.startswith('_')
        }))
    elif event_name in {'update_status', 'update_done'}:
        specs.append(('update', event_name, {
            key: value for key, value in data.items() if not key.startswith('_')
        }))
    elif event_name == 'whatsapp_restriction_warning':
        specs.append(('chat', event_name, {
            'content': data.get('content', ''), 'metadata': metadata,
        }))
    elif event_name == 'session_clear':
        specs.append(('chat', event_name, {
            'session_id': session_id or '', 'agent_id': agent_id or '',
        }))
    elif event_name == 'turn_split':
        specs.append(('chat', event_name, {}))
    elif event_name == 'evonic:agent-state-changed':
        specs.append(('chat', 'state_changed', {
            'agent_id': agent_id or '', 'session_id': session_id or '',
        }))

    ids = []
    for channel, public_name, payload in specs:
        ids.append(realtime_store.publish(
            channel, public_name, payload,
            agent_id=agent_id, session_id=session_id,
            workplace_id=workplace_id, turn_id=turn_id,
        ))
    return ids
