import sqlite3
from typing import Optional


class SettingsMixin:
    """App-level key-value settings. Requires self._connect() from the host class."""

    # ---------------------------------------------------------------
    # In-memory cache so repeated reads (e.g. sidebar on every page
    # load) don't hit the DB. Invalidated automatically on write.
    # ---------------------------------------------------------------
    _settings_cache: dict = {}

    @classmethod
    def invalidate_settings_cache(cls, key: str = None):
        """Clear a single key or the entire settings cache."""
        if key is None:
            cls._settings_cache.clear()
        else:
            cls._settings_cache.pop(key, None)

    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        """Get an app-level setting by key. Cached in-memory after first read."""
        if key in self._settings_cache:
            return self._settings_cache[key]

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cursor.fetchone()

        value = row[0] if row else default
        self._settings_cache[key] = value
        return value

    def get_settings_by_prefix(self, prefix: str) -> dict:
        """Get all settings whose key starts with prefix, in a single query.

        Returns {full_key: value}. LIKE wildcards in the prefix are escaped
        so ids containing '_' or '%' can't cross-match other keys.
        """
        escaped = (prefix.replace('\\', '\\\\')
                         .replace('%', '\\%')
                         .replace('_', '\\_'))
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE ? ESCAPE '\\'",
                (escaped + '%',)
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def set_setting(self, key: str, value: str):
        """Set an app-level setting. Invalidates the cache for this key."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value)
            )
            conn.commit()
        self._settings_cache[key] = value

    def consume_setting(self, key: str, default: str = None) -> Optional[str]:
        """Atomically return a setting value and clear it."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            value = row[0] if row else default
            if row:
                conn.execute(
                    "UPDATE app_settings SET value = '' WHERE key = ?", (key,)
                )
            conn.commit()
        self._settings_cache[key] = '' if row else default
        return value
