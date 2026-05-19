"""
TokenDB — SQLite storage for Model Router bearer tokens.

All token state lives in shared/data/db/plugins/model-router.db (WAL mode).
Tokens are stored as SHA-256 hashes; the plaintext token is returned
only once at creation time.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Generator

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_shared_data = os.path.join(BASE_DIR, 'shared', 'data')
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, 'data')
PLUGIN_DB_DIR = os.path.join(_data_root, 'db', 'plugins')
DB_PATH = os.path.join(PLUGIN_DB_DIR, 'model-router.db')

DEFAULT_ALLOWED_MODELS = '*'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_midnight_utc() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
    return midnight.isoformat()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def token_prefix(token: str) -> str:
    return token[:8]


class TokenDB:
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
                CREATE TABLE IF NOT EXISTS tokens (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT NOT NULL,
                    token_hash      TEXT NOT NULL UNIQUE,
                    token_prefix    TEXT NOT NULL,
                    quota_limit     INTEGER,
                    quota_used      INTEGER NOT NULL DEFAULT 0,
                    quota_reset_at  TEXT,
                    status          TEXT NOT NULL DEFAULT 'active',
                    expires_at      TEXT,
                    allowed_models  TEXT NOT NULL DEFAULT '*',
                    last_used_at    TEXT,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_usage_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id            INTEGER NOT NULL,
                    model               TEXT NOT NULL,
                    session_id          TEXT,
                    prompt_tokens       INTEGER DEFAULT 0,
                    completion_tokens   INTEGER DEFAULT 0,
                    duration_ms         INTEGER DEFAULT 0,
                    error               TEXT,
                    created_at          TEXT NOT NULL,
                    FOREIGN KEY (token_id) REFERENCES tokens(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_token_created
                ON api_usage_log(token_id, created_at DESC)
            """)

    # ─── CRUD ───────────────────────────────────────────────────────────

    def create_token(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        token = generate_token()
        token_h = hash_token(token)
        prefix = token_prefix(token)

        name = data.get('name', '').strip()
        if not name:
            return None

        quota_limit = data.get('quota_limit')
        expires_at = data.get('expires_at')
        allowed_models = data.get('allowed_models', DEFAULT_ALLOWED_MODELS)

        if isinstance(allowed_models, list):
            allowed_models = json.dumps(allowed_models)
        elif allowed_models is None or allowed_models == '':
            allowed_models = DEFAULT_ALLOWED_MODELS

        quota_reset_at = _next_midnight_utc() if quota_limit is not None else None

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tokens (name, token_hash, token_prefix, quota_limit,
                                    quota_used, quota_reset_at, status, expires_at,
                                    allowed_models, created_at)
                VALUES (?, ?, ?, ?, 0, ?, 'active', ?, ?, ?)
            """, (name, token_h, prefix, quota_limit, quota_reset_at,
                  expires_at, allowed_models, _now()))
            conn.commit()
            row = self._get_by_id(cursor.lastrowid)
            if row:
                row['token'] = token
            return row

    def get_token_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tokens WHERE token_hash = ?", (token_hash,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def _get_by_id(self, token_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_tokens(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT * FROM tokens WHERE status = ? ORDER BY created_at DESC",
                    (status,)
                )
            else:
                cursor.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]

    def update_token(self, token_id: int, data: Dict[str, Any]) -> bool:
        allowed = {'name', 'quota_limit', 'status', 'expires_at', 'allowed_models'}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False

        if 'allowed_models' in updates:
            am = updates['allowed_models']
            if isinstance(am, list):
                updates['allowed_models'] = json.dumps(am)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [token_id]

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE tokens SET {set_clause} WHERE id = ?",
                values
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_token(self, token_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            conn.commit()
            return cursor.rowcount > 0

    def reset_token(self, token_id: int) -> Optional[str]:
        token = generate_token()
        token_h = hash_token(token)
        prefix = token_prefix(token)

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tokens SET token_hash = ?, token_prefix = ?
                WHERE id = ?
            """, (token_h, prefix, token_id))
            conn.commit()
            if cursor.rowcount == 0:
                return None
        return token

    # ─── Quota ──────────────────────────────────────────────────────────

    def reset_quota_if_needed(self, token: Dict[str, Any]) -> bool:
        quota_reset_at = token.get('quota_reset_at')
        quota_limit = token.get('quota_limit')

        if quota_limit is None or not quota_reset_at:
            return False

        now = datetime.now(timezone.utc)
        reset_time = datetime.fromisoformat(quota_reset_at)

        if now < reset_time:
            return False

        next_reset = _next_midnight_utc()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tokens SET quota_used = 0, quota_reset_at = ?
                WHERE id = ?
            """, (next_reset, token['id']))
            conn.commit()
        token['quota_used'] = 0
        token['quota_reset_at'] = next_reset
        return True

    def increment_quota(self, token: Dict[str, Any]) -> None:
        self.reset_quota_if_needed(token)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tokens SET quota_used = quota_used + 1,
                                  last_used_at = ?
                WHERE id = ?
            """, (_now(), token['id']))
            conn.commit()
        token['quota_used'] = token.get('quota_used', 0) + 1
        token['last_used_at'] = _now()

    def is_token_expired(self, token: Dict[str, Any]) -> bool:
        expires_at = token.get('expires_at')
        if not expires_at:
            return False
        return datetime.now(timezone.utc) > datetime.fromisoformat(expires_at)

    # ─── Model scope ────────────────────────────────────────────────────

    @staticmethod
    def token_can_access_model(token: Dict[str, Any], model: str) -> bool:
        allowed_raw = token.get('allowed_models', DEFAULT_ALLOWED_MODELS)
        if allowed_raw == '*':
            return True
        try:
            allowed = json.loads(allowed_raw) if isinstance(allowed_raw, str) else allowed_raw
            return model in allowed
        except (json.JSONDecodeError, TypeError):
            return False

    @staticmethod
    def token_visible_models(token: Dict[str, Any], available_models: List[str]) -> List[str]:
        allowed_raw = token.get('allowed_models', DEFAULT_ALLOWED_MODELS)
        if allowed_raw == '*':
            return available_models
        try:
            allowed = json.loads(allowed_raw) if isinstance(allowed_raw, str) else allowed_raw
            return [m for m in available_models if m in allowed]
        except (json.JSONDecodeError, TypeError):
            return []

    # ─── Usage log ──────────────────────────────────────────────────────

    def log_usage(self, token_id: int, model: str, session_id: Optional[str] = None,
                  prompt_tokens: int = 0, completion_tokens: int = 0,
                  duration_ms: int = 0, error: Optional[str] = None) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO api_usage_log (token_id, model, session_id,
                                           prompt_tokens, completion_tokens,
                                           duration_ms, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (token_id, model, session_id,
                  prompt_tokens, completion_tokens, duration_ms, error, _now()))
            conn.commit()

    def get_token_stats(self, token_id: int) -> Optional[Dict[str, Any]]:
        token = self._get_by_id(token_id)
        if not token:
            return None
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as total_calls,
                       COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                       COALESCE(SUM(duration_ms), 0) as total_duration_ms
                FROM api_usage_log WHERE token_id = ?
            """, (token_id,))
            row = cursor.fetchone()
            stats = dict(row) if row else {}
            stats.update({
                'id': token.get('id'),
                'name': token.get('name'),
                'token_prefix': token.get('token_prefix'),
                'quota_limit': token.get('quota_limit'),
                'quota_used': token.get('quota_used'),
                'status': token.get('status'),
                'expires_at': token.get('expires_at'),
                'allowed_models': token.get('allowed_models'),
                'last_used_at': token.get('last_used_at'),
                'created_at': token.get('created_at'),
            })
            return stats

    def get_usage_logs(self, token_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM api_usage_log
                WHERE token_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (token_id, limit, offset))
            return [dict(row) for row in cursor.fetchall()]


# Singleton
token_db = TokenDB()


def init_plugin_config():
    """Auto-populate MODEL_MODEL_MAP from installed models if not already set."""
    try:
        from models.db import db
        plugin_id = 'model_router'
        config_key = f'plugin_config:{plugin_id}:MODEL_MODEL_MAP'
        
        # Check if config already exists
        existing = db.get_setting(config_key)
        if existing is not None and existing.strip() and existing.strip() != '{}':
            return  # Already configured
        
        # Get enabled models
        models = db.get_llm_models()
        enabled_models = [m['model_name'] for m in models if m.get('enabled', 1)]
        
        if enabled_models:
            # Create a map where alias == model_name
            model_map = {m: m for m in enabled_models}
            db.set_setting(config_key, json.dumps(model_map))
    except Exception:
        pass  # Non-critical, user can configure manually
