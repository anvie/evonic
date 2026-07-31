from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS workflow_subjects (
 id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 subject_key TEXT NOT NULL, subject_masked TEXT NOT NULL, active_epoch INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked','submitted','closed')),
 failure_count INTEGER NOT NULL DEFAULT 0, first_failure_at TEXT, last_failure_at TEXT,
 locked_at TEXT, last_stage TEXT, last_reason_code TEXT, version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(policy_id,subject_key));
CREATE TABLE IF NOT EXISTS workflow_failure_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id TEXT NOT NULL REFERENCES workflow_subjects(id),
 policy_id TEXT NOT NULL, epoch INTEGER NOT NULL, attempt_key TEXT NOT NULL,
 stage TEXT NOT NULL, reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(subject_id,epoch,attempt_key,stage));
CREATE TABLE IF NOT EXISTS workflow_escalation_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id TEXT NOT NULL REFERENCES workflow_subjects(id),
 policy_id TEXT NOT NULL, epoch INTEGER NOT NULL, destination_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
 delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_status IN ('pending','delivered','failed')),
 retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, last_error_code TEXT,
 created_at TEXT NOT NULL, delivered_at TEXT);
CREATE TABLE IF NOT EXISTS workflow_case_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id TEXT NOT NULL REFERENCES workflow_subjects(id),
 action TEXT NOT NULL, actor_id TEXT NOT NULL, reason TEXT NOT NULL,
 previous_status TEXT NOT NULL, previous_count INTEGER NOT NULL,
 resulting_status TEXT NOT NULL, resulting_epoch INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS workflow_subject_status ON workflow_subjects(policy_id,status);
CREATE INDEX IF NOT EXISTS workflow_outbox_status ON workflow_escalation_outbox(delivery_status,next_retry_at);
"""


def now(): return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with self.connect() as conn: conn.executescript(SCHEMA)

    @contextmanager
    def connect(self, immediate=False):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if immediate: conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally: conn.close()

    def subject(self, policy, subject_key, masked, create=True):
        with self.connect(immediate=create) as conn:
            row = conn.execute('SELECT * FROM workflow_subjects WHERE policy_id=? AND subject_key=?', (policy['policy_id'], subject_key)).fetchone()
            if not row and create:
                ts, sid = now(), str(uuid.uuid4())
                conn.execute('INSERT INTO workflow_subjects(id,policy_id,agent_id,subject_key,subject_masked,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (sid, policy['policy_id'], policy['agent_id'], subject_key, masked, ts, ts))
                row = conn.execute('SELECT * FROM workflow_subjects WHERE id=?', (sid,)).fetchone()
            return dict(row) if row else None

    def record_failure(self, policy, subject_key, masked, attempt_key, stage, reason, shadow=False):
        threshold, ts = int(policy['threshold']), now()
        with self.connect(immediate=True) as conn:
            row = conn.execute('SELECT * FROM workflow_subjects WHERE policy_id=? AND subject_key=?', (policy['policy_id'], subject_key)).fetchone()
            if not row:
                sid = str(uuid.uuid4())
                conn.execute('INSERT INTO workflow_subjects(id,policy_id,agent_id,subject_key,subject_masked,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (sid, policy['policy_id'], policy['agent_id'], subject_key, masked, ts, ts))
                row = conn.execute('SELECT * FROM workflow_subjects WHERE id=?', (sid,)).fetchone()
            if row['status'] == 'locked':
                duplicate = conn.execute('SELECT 1 FROM workflow_failure_events WHERE subject_id=? AND epoch=? AND attempt_key=? AND stage=?', (row['id'], row['active_epoch'], attempt_key, stage)).fetchone() is not None
                return {'locked': True, 'count': row['failure_count'], 'duplicate': duplicate,
                        'subject_id': row['id']}
            cur = conn.execute('INSERT OR IGNORE INTO workflow_failure_events(subject_id,policy_id,epoch,attempt_key,stage,reason_code,created_at) VALUES(?,?,?,?,?,?,?)', (row['id'], policy['policy_id'], row['active_epoch'], attempt_key, stage, reason, ts))
            if cur.rowcount == 0: return {'locked': False, 'count': row['failure_count'], 'duplicate': True}
            count = row['failure_count'] + 1
            locked = count >= threshold and not shadow
            conn.execute("UPDATE workflow_subjects SET failure_count=?, first_failure_at=COALESCE(first_failure_at,?), last_failure_at=?, last_stage=?, last_reason_code=?, status=CASE WHEN ? THEN 'locked' ELSE status END, locked_at=CASE WHEN ? THEN ? ELSE locked_at END, version=version+1, updated_at=? WHERE id=?", (count, ts, ts, stage, reason, locked, locked, ts, ts, row['id']))
            if locked:
                payload = {'case_reference': row['id'], 'policy_id': policy['policy_id'], 'agent_id': policy['agent_id'], 'subject_masked': masked, 'failure_count': count, 'last_stage': stage, 'last_reason_code': reason, 'first_failure_at': row['first_failure_at'] or ts, 'last_failure_at': ts, 'locked_at': ts}
                key = f"{row['id']}:{row['active_epoch']}:threshold-lock"
                conn.execute('INSERT OR IGNORE INTO workflow_escalation_outbox(subject_id,policy_id,epoch,destination_id,idempotency_key,payload_json,created_at) VALUES(?,?,?,?,?,?,?)', (row['id'], policy['policy_id'], row['active_epoch'], policy.get('escalation_destination','staff'), key, json.dumps(payload, ensure_ascii=False), ts))
            return {'locked': locked, 'count': count, 'duplicate': False, 'subject_id': row['id']}

    def mark_submitted(self, policy_id, subject_key):
        with self.connect(immediate=True) as conn:
            conn.execute("UPDATE workflow_subjects SET status='submitted',updated_at=?,version=version+1 WHERE policy_id=? AND subject_key=? AND status='open'", (now(), policy_id, subject_key))

    def list_subjects(self, status=None):
        with self.connect() as conn:
            sql = 'SELECT id,policy_id,agent_id,subject_masked,active_epoch,status,failure_count,first_failure_at,last_failure_at,locked_at,last_stage,last_reason_code,updated_at FROM workflow_subjects' + (' WHERE status=?' if status else '') + ' ORDER BY updated_at DESC'
            return [dict(r) for r in conn.execute(sql, (status,) if status else ()).fetchall()]

    def list_outbox(self, limit=100):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT id,subject_id,policy_id,epoch,destination_id,delivery_status,retry_count,next_retry_at,last_error_code,created_at,delivered_at FROM workflow_escalation_outbox ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

    def reopen(self, subject_id, actor, reason):
        if not actor or not reason: raise ValueError('actor and reason are required')
        with self.connect(immediate=True) as conn:
            row = conn.execute('SELECT * FROM workflow_subjects WHERE id=?', (subject_id,)).fetchone()
            if not row: raise KeyError(subject_id)
            epoch, ts = row['active_epoch'] + 1, now()
            conn.execute('UPDATE workflow_subjects SET active_epoch=?,status=\'open\',failure_count=0,first_failure_at=NULL,last_failure_at=NULL,locked_at=NULL,last_stage=NULL,last_reason_code=NULL,version=version+1,updated_at=? WHERE id=?', (epoch, ts, subject_id))
            conn.execute('INSERT INTO workflow_case_audit(subject_id,action,actor_id,reason,previous_status,previous_count,resulting_status,resulting_epoch,created_at) VALUES(?,?,?,?,?,?,?,?,?)', (subject_id, 'reopen', actor, reason, row['status'], row['failure_count'], 'open', epoch, ts))
            return {'subject_id': subject_id, 'status': 'open', 'epoch': epoch}

    def pending_outbox(self, limit=20):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM workflow_escalation_outbox WHERE delivery_status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY id LIMIT ?", (now(), limit)).fetchall()]

    def delivery_result(self, row_id, delivered, error_code=None, next_retry_at=None):
        with self.connect(immediate=True) as conn:
            if delivered:
                conn.execute("UPDATE workflow_escalation_outbox SET delivery_status='delivered',delivered_at=?,last_error_code=NULL WHERE id=?", (now(), row_id))
            else:
                conn.execute("UPDATE workflow_escalation_outbox SET retry_count=retry_count+1,next_retry_at=?,last_error_code=?,delivery_status=CASE WHEN retry_count+1>=8 THEN 'failed' ELSE 'pending' END WHERE id=?", (next_retry_at, error_code, row_id))
