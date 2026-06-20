"""Regression test for get_model_usage on a fresh-install agents table.

A fresh agents table has model_id but no legacy 'model' column. The query must
group by model_id, otherwise it raises OperationalError: no such column: model
and 500s the dashboard.
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager

_DASHBOARD_PY = os.path.join(
    os.path.dirname(__file__), '..', 'models', 'mixins', 'dashboard.py'
)

# Load dashboard.py directly (it only depends on sqlite3 + typing) so the test
# runs even when full project deps are not installed.
_spec = importlib.util.spec_from_file_location('_dashboard_under_test', _DASHBOARD_PY)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DashboardMixin = _mod.DashboardMixin


class _FreshInstallDB(DashboardMixin):
    """Minimal host exposing _connect(), backed by a fresh-install agents table."""

    def __init__(self, db_path):
        self.db_path = db_path
        # One persistent connection, mirroring Database._connect (commits but
        # does not close on context exit, so get_model_usage's no-arg path works).
        self._conn = sqlite3.connect(db_path)
        # Mirror the v0.8.0 fresh CREATE TABLE: model_id exists, 'model' does NOT.
        self._conn.execute(
            """CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                default_model_id TEXT,
                model_id TEXT
            )"""
        )
        self._conn.executemany(
            "INSERT INTO agents (id, name, model_id) VALUES (?, ?, ?)",
            [('a1', 'A1', 'gpt-x'), ('a2', 'A2', 'gpt-x'), ('a3', 'A3', None)],
        )
        self._conn.commit()

    @contextmanager
    def _connect(self):
        with self._conn:
            yield self._conn

    def close(self):
        self._conn.close()


class TestDashboardModelUsageFreshInstall(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.db = _FreshInstallDB(self.path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_no_model_column_on_fresh_agents_table(self):
        with self.db._connect() as conn:
            cols = {row[1] for row in conn.execute('PRAGMA table_info(agents)')}
        self.assertNotIn('model', cols, "fresh agents table must not have legacy 'model'")
        self.assertIn('model_id', cols)

    def test_get_model_usage_groups_by_model_id(self):
        # Must not raise OperationalError: no such column: model
        rows = self.db.get_model_usage()
        usage = {r['model']: r['agent_count'] for r in rows}
        self.assertEqual(usage, {'gpt-x': 2, 'default': 1})
        # Response field name stays 'model' so the dashboard JS contract is intact.
        self.assertIn('model', rows[0])


if __name__ == '__main__':
    unittest.main()
