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
            cursor.execute("SELECT overall_score FROM evaluation_runs WHERE overall_score IS NOT NULL ORDER BY started_at DESC LIMIT 1")
            row = cursor.fetchone()
            latest_eval_score = round(row[0] * 100, 1) if row else None
            return {
                'agent_count': agent_count,
                'tool_count': tool_count,
                'channel_count': channel_count,
                'active_channel_count': active_channel_count,
                'session_count': session_count,
                'eval_run_count': eval_run_count,
                'latest_eval_score': latest_eval_score,
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
                SELECT run_id, model_name, overall_score, started_at, completed_at,
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

    def get_model_leaderboard(self, limit: int = 5, _conn=None) -> List[Dict[str, Any]]:
        """Top models by best evaluation score"""
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
                SELECT g.model_name, g.best_score, g.run_count, g.last_run,
                    (SELECT run_id FROM evaluation_runs
                     WHERE model_name = g.model_name AND overall_score = g.best_score
                     AND completed_at IS NOT NULL
                     ORDER BY started_at DESC LIMIT 1) as best_run_id
                FROM (
                    SELECT model_name,
                        MAX(overall_score) as best_score,
                        COUNT(*) as run_count,
                        MAX(started_at) as last_run
                    FROM evaluation_runs
                    WHERE overall_score IS NOT NULL AND model_name IS NOT NULL
                        AND completed_at IS NOT NULL
                    GROUP BY model_name
                    ORDER BY best_score DESC, run_count DESC
                    LIMIT ?
                ) g
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
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
                SELECT COALESCE(model_id, 'default') as model, COUNT(*) as agent_count
                FROM agents
                GROUP BY COALESCE(model_id, 'default')
                ORDER BY agent_count DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            if not _conn:
                conn.__exit__(None, None, None)
