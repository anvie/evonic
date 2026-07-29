"""SQLite storage for Muktamar photo validation results."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_shared_data = os.path.join(BASE_DIR, "shared", "data")
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(_data_root, "db", "plugins", "muktamar_api.db")


class PhotoValidationCache:
    """Persistent SHA-1 keyed cache scoped to the muktamar_api plugin."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_table()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS photo_validation_cache (
                    image_sha1 TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, image_sha1: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM photo_validation_cache WHERE image_sha1 = ?",
                (image_sha1,),
            ).fetchone()
        if not row:
            return None
        try:
            result = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    def put(self, image_sha1: str, result: dict[str, Any]) -> None:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO photo_validation_cache
                    (image_sha1, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(image_sha1) DO UPDATE SET
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (image_sha1, serialized, now, now),
            )
