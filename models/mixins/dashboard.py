import sqlite3
from typing import Dict, Any, List


class DashboardMixin:
    """Dashboard stats and analytics queries. Requires self._connect() and self._chat_db()
    from the host class."""

    def get_dashboard_stats(self, _conn=None) -> Dict[str, Any]:
        """Aggregate counts for dashboard stat cards"""
        if _conn:
            cursor = _conn.cursor()
        else:
            conn = self._connect().__enter__()
            cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM agents")
            agent_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM tools")
            tool_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM channels")
            channel_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1")
            active_channel_count = cursor.fetchone()[0]
            # Session count from cached column — single query, no N+1 per-agent DB opens
            cursor.execute("SELECT COALESCE(SUM(session_count), 0) FROM agents")
            session_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM evaluation_runs")
            eval_run_count = cursor.fetchone()[0]
            cursor.execute("SELECT total_points, max_points FROM evaluation_runs WHERE total_points IS NOT NULL ORDER BY started_at DESC LIMIT 1")
            row = cursor.fetchone()
            latest_eval_points = round(row[0], 1) if row else None
            latest_eval_max_points = round(row[1], 1) if row else None
            return {
                'agent_count': agent_count,
                'tool_count': tool_count,
                'channel_count': channel_count,
                'active_channel_count': active_channel_count,
                'session_count': session_count,
                'eval_run_count': eval_run_count,
                'latest_eval_points': latest_eval_points,
                'latest_eval_max_points': latest_eval_max_points,
            }
        finally:
            if not _conn:
                conn.__exit__(None, None, None)

    def get_recent_agents(self, limit: int = 5, _conn=None) -> List[Dict[str, Any]]:
        """Most recently created agents with tool, channel, and session counts"""
        if _conn:
            conn = _conn
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        else:
            conn = self._connect().__enter__()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT a.*,
                    COALESCE(t.cnt, 0) as tool_count,
                    COALESCE(c.cnt, 0) as channel_count
                FROM agents a
                LEFT JOIN (SELECT agent_id, COUNT(*) as cnt FROM agent_tools GROUP BY agent_id) t ON t.agent_id = a.id
                LEFT JOIN (SELECT agent_id, COUNT(*) as cnt FROM channels GROUP BY agent_id) c ON c.agent_id = a.id
                ORDER BY a.created_at DESC
                LIMIT ?
            """, (limit,))
            agents = [dict(row) for row in cursor.fetchall()]
            return agents
        finally:
            if not _conn:
                conn.__exit__(None, None, None)

    def get_recent_runs(self, limit: int = 5, _conn=None) -> List[Dict[str, Any]]:
        """Most recent evaluation runs for dashboard"""
        if _conn:
            conn = _conn
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        else:
            conn = self._connect().__enter__()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT run_id, model_name, overall_score, total_points, max_points, started_at, completed_at,
                    (SELECT COUNT(*) FROM individual_test_results WHERE run_id = e.run_id) as test_count,
                    (SELECT COUNT(*) FROM individual_test_results WHERE run_id = e.run_id AND status = 'passed') as passed_count
                FROM evaluation_runs e
                ORDER BY started_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            if not _conn:
                conn.__exit__(None, None, None)

    def get_latest_test_set_hash(self, cursor) -> Any:
        """Test set of the most recent completed run (the one the leaderboard defaults to).

        Runs from before point scoring only have a `legacy:` fingerprint, so they count
        only when no run has a real one yet.
        """
        cursor.execute(
            "SELECT test_set_hash FROM evaluation_runs "
            "WHERE completed_at IS NOT NULL AND test_set_hash IS NOT NULL "
            "ORDER BY test_set_hash LIKE 'legacy:%', started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def current_test_set_hash(self) -> Any:
        with self._connect() as conn:
            return self.get_latest_test_set_hash(conn.cursor())

    def list_test_sets(self) -> List[Dict[str, Any]]:
        """Distinct test sets that completed runs were made against, newest first"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT test_set_hash, COUNT(*) as run_count, MAX(started_at) as last_run,
                       MAX(tests_run) as tests_run, MAX(max_points) as max_points
                FROM evaluation_runs
                WHERE completed_at IS NOT NULL AND test_set_hash IS NOT NULL
                GROUP BY test_set_hash
                ORDER BY last_run DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_model_leaderboard(self, limit: int = 5, _conn=None, test_set_hash: str = None) -> List[Dict[str, Any]]:
        """Top models by best absolute points within one test set.

        Defaults to the test set of the latest completed run, so scores from
        different question sets are never ranked against each other.
        """
        if _conn:
            conn = _conn
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        else:
            conn = self._connect().__enter__()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        try:
            test_set_hash = test_set_hash or self.get_latest_test_set_hash(cursor)
            if not test_set_hash:
                return []
            cursor.execute("""
                SELECT g.model_name, g.best_points, g.run_count, g.last_run, ? as test_set_hash,
                    (SELECT run_id FROM evaluation_runs
                     WHERE model_name = g.model_name AND total_points = g.best_points
                     AND test_set_hash = ? AND completed_at IS NOT NULL
                     ORDER BY started_at DESC LIMIT 1) as best_run_id,
                    (SELECT max_points FROM evaluation_runs
                     WHERE model_name = g.model_name AND total_points = g.best_points
                     AND test_set_hash = ? AND completed_at IS NOT NULL
                     ORDER BY started_at DESC LIMIT 1) as max_points
                FROM (
                    SELECT model_name,
                        MAX(total_points) as best_points,
                        COUNT(*) as run_count,
                        MAX(started_at) as last_run
                    FROM evaluation_runs
                    WHERE total_points IS NOT NULL AND model_name IS NOT NULL
                        AND completed_at IS NOT NULL AND test_set_hash = ?
                    GROUP BY model_name
                    ORDER BY best_points DESC, run_count DESC
                    LIMIT ?
                ) g
            """, (test_set_hash, test_set_hash, test_set_hash, test_set_hash, limit))
            from evaluator.points import run_score
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row['best_score'] = run_score(row['best_points'])
            return rows
        finally:
            if not _conn:
                conn.__exit__(None, None, None)

    def get_model_usage(self, _conn=None) -> List[Dict[str, Any]]:
        """Count of agents per model for distribution display"""
        if _conn:
            conn = _conn
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        else:
            conn = self._connect().__enter__()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT COALESCE(m.name, a.model_id, 'default') as model, COUNT(*) as agent_count
                FROM agents a
                LEFT JOIN llm_models m ON m.id = a.model_id
                GROUP BY COALESCE(m.name, a.model_id, 'default')
                ORDER BY agent_count DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            if not _conn:
                conn.__exit__(None, None, None)
