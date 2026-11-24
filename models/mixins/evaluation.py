import sqlite3
import json
from datetime import datetime
from typing import Dict, Any, List, Optional


class EvaluationMixin:
    """Evaluation runs and improvement cycle CRUD. Requires self._connect() from the host class."""

    def create_evaluation_run(self, model_name: str, selected_domains: list = None) -> int:
        """Create a new evaluation run and return run_id"""
        started_at = datetime.now()
        domains_json = json.dumps(selected_domains) if selected_domains else None

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO evaluation_runs (started_at, model_name, status, selected_domains) VALUES (?, ?, 'running', ?)",
                (started_at, model_name, domains_json)
            )
            run_id = cursor.lastrowid
            conn.commit()

        return run_id

    def update_test_result(self, run_id: int, domain: str, level: int, **kwargs):
        """Update test result with various fields"""
        allowed_fields = {
            'model_name', 'prompt', 'response', 'expected', 'score',
            'status', 'details', 'duration_ms'
        }

        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return

        set_clause = ", ".join(f"{field} = ?" for field in updates.keys())
        values = list(updates.values()) + [run_id, domain, level]

        with self._connect() as conn:
            cursor = conn.cursor()

            # Check if row exists
            cursor.execute(
                "SELECT id FROM test_results WHERE run_id = ? AND domain = ? AND level = ?",
                (run_id, domain, level)
            )

            if cursor.fetchone():
                # Update existing
                cursor.execute(
                    f"UPDATE test_results SET {set_clause} WHERE run_id = ? AND domain = ? AND level = ?",
                    values
                )
            else:
                # Insert new
                columns = ['run_id', 'domain', 'level'] + list(updates.keys())
                placeholders = ', '.join(['?'] * len(columns))
                insert_values = [run_id, domain, level] + list(updates.values())

                cursor.execute(
                    f"INSERT INTO test_results ({', '.join(columns)}) VALUES ({placeholders})",
                    insert_values
                )

            conn.commit()

    def complete_evaluation_run(self, run_id: int, summary: str, overall_score: float,
                                 total_tokens: int = 0, total_duration_ms: int = 0):
        """Mark evaluation run as completed"""
        completed_at = datetime.now()

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE evaluation_runs
                   SET completed_at = ?, summary = ?, overall_score = ?,
                       total_tokens = ?, total_duration_ms = ?, status = 'completed'
                   WHERE run_id = ?""",
                (completed_at, summary, overall_score, total_tokens, total_duration_ms, run_id)
            )
            conn.commit()

    def set_run_overall_score(self, run_id: int, overall_score: float):
        with self._connect() as conn:
            conn.execute("UPDATE evaluation_runs SET overall_score = ? WHERE run_id = ?", (overall_score, run_id))
            conn.commit()

    def refresh_run_points(self, run_id: int) -> Dict[str, Any]:
        """Recompute a run's point totals and test-set hash from its individual results."""
        from evaluator.points import calculate_points, run_set_hash
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT test_id, domain, level, status, score, weight, max_points, points, definition_hash "
                "FROM individual_test_results WHERE run_id = ?",
                (run_id,)
            )
            rows = [dict(r) for r in cursor.fetchall()]
            totals = calculate_points(rows)
            counted = [r for r in rows if r['status'] != 'skipped']
            cursor.execute(
                "UPDATE evaluation_runs SET total_points = ?, max_points = ?, tests_run = ?, test_set_hash = ? WHERE run_id = ?",
                (totals['total'], totals['max_points'], totals['count'],
                 run_set_hash(counted) if counted else None, run_id)
            )
            conn.commit()
            return totals

    def mark_run_incomplete(self, run_id: int) -> bool:
        """Mark an evaluation run as interrupted/incomplete (preserving all test data for resume)"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE evaluation_runs SET status = 'interrupted' WHERE run_id = ?",
                (run_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_incomplete_runs(self) -> List[Dict[str, Any]]:
        """Get evaluation runs that were interrupted and can be resumed"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """SELECT run_id, started_at, completed_at, model_name, summary, overall_score, total_tokens, total_duration_ms, notes, status, selected_domains FROM evaluation_runs
                   WHERE status = 'interrupted'
                   ORDER BY started_at DESC"""
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_evaluation_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Get evaluation run details"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT run_id, started_at, completed_at, model_name, summary, overall_score, total_tokens, total_duration_ms, notes, status, selected_domains, total_points, max_points, tests_run, test_set_hash FROM evaluation_runs WHERE run_id = ?",
                (run_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_test_results(self, run_id: int) -> List[Dict[str, Any]]:
        """Get all test results for a run"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, run_id, timestamp, model_name, domain, level, prompt, response, expected, score, status, details, duration_ms FROM test_results WHERE run_id = ? ORDER BY domain, level",
                (run_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_all_runs(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """Get evaluation runs with pagination, ordered by most recent first"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """SELECT e.*,
                    (SELECT COUNT(*) FROM test_results WHERE run_id = e.run_id) as test_count,
                    (SELECT COUNT(*) FROM test_results WHERE run_id = e.run_id AND status = 'passed') as passed_count
                FROM evaluation_runs e
                ORDER BY e.started_at DESC LIMIT ? OFFSET ?""",
                (limit, offset)
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_run(self, run_id: int) -> bool:
        """Delete an evaluation run and all related data"""
        with self._connect() as conn:
            cursor = conn.cursor()
            # Delete in dependency order
            cursor.execute("""
                DELETE FROM generated_training_data WHERE cycle_id IN
                (SELECT cycle_id FROM improvement_cycles
                 WHERE base_run_id = ? OR improved_run_id = ?)
            """, (run_id, run_id))
            cursor.execute(
                "DELETE FROM improvement_cycles WHERE base_run_id = ? OR improved_run_id = ?",
                (run_id, run_id))
            cursor.execute("DELETE FROM individual_test_results WHERE run_id = ?", (run_id,))
            cursor.execute("DELETE FROM level_scores WHERE run_id = ?", (run_id,))
            cursor.execute("DELETE FROM test_results WHERE run_id = ?", (run_id,))
            cursor.execute("DELETE FROM evaluation_runs WHERE run_id = ?", (run_id,))
            conn.commit()
            return cursor.rowcount > 0

    def clear_all_runs(self) -> int:
        """Delete all evaluation runs and related data. Returns count of deleted runs."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM evaluation_runs")
            count = cursor.fetchone()[0]
            cursor.execute("DELETE FROM generated_training_data")
            cursor.execute("DELETE FROM improvement_cycles")
            cursor.execute("DELETE FROM individual_test_results")
            cursor.execute("DELETE FROM level_scores")
            cursor.execute("DELETE FROM test_results")
            cursor.execute("DELETE FROM evaluation_runs")
            conn.commit()
            return count

    def get_incomplete_count(self) -> int:
        """Get count of evaluation runs where completed_at IS NULL (incomplete)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM evaluation_runs WHERE completed_at IS NULL"
            )
            return cursor.fetchone()[0]

    def clear_incomplete_runs(self) -> int:
        """Delete all incomplete evaluation runs (completed_at IS NULL) and related data.
        Returns count of deleted runs."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT run_id FROM evaluation_runs WHERE completed_at IS NULL"
            )
            incomplete_ids = [row[0] for row in cursor.fetchall()]
            count = len(incomplete_ids)

            if count == 0:
                return 0

            placeholders = ",".join("?" for _ in incomplete_ids)

            # Delete related data in dependency order
            cursor.execute(
                f"DELETE FROM generated_training_data WHERE cycle_id IN "
                f"(SELECT cycle_id FROM improvement_cycles "
                f" WHERE base_run_id IN ({placeholders}) OR improved_run_id IN ({placeholders}))",
                incomplete_ids + incomplete_ids,
            )
            cursor.execute(
                f"DELETE FROM improvement_cycles "
                f"WHERE base_run_id IN ({placeholders}) OR improved_run_id IN ({placeholders})",
                incomplete_ids + incomplete_ids,
            )
            cursor.execute(
                f"DELETE FROM individual_test_results WHERE run_id IN ({placeholders})",
                incomplete_ids,
            )
            cursor.execute(
                f"DELETE FROM level_scores WHERE run_id IN ({placeholders})",
                incomplete_ids,
            )
            cursor.execute(
                f"DELETE FROM test_results WHERE run_id IN ({placeholders})",
                incomplete_ids,
            )
            cursor.execute(
                f"DELETE FROM evaluation_runs WHERE run_id IN ({placeholders})",
                incomplete_ids,
            )
            conn.commit()
            return count

    def update_run_notes(self, run_id: int, notes: str) -> bool:
        """Update notes for an evaluation run"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE evaluation_runs SET notes = ? WHERE run_id = ?", (notes, run_id))
            conn.commit()
            return cursor.rowcount > 0

    def update_run_summary(self, run_id: int, summary: str) -> bool:
        """Update summary for an evaluation run"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE evaluation_runs SET summary = ? WHERE run_id = ?", (summary, run_id))
            conn.commit()
            return cursor.rowcount > 0

    def update_run_model_name(self, run_id: int, model_name: str) -> bool:
        """Update the model name of an evaluation run.

        The run's stored results (domain-level ``test_results`` and
        ``individual_test_results``) keep their own ``model_name`` snapshot, so
        the new label is cascaded to them as well.  This keeps the history
        detail view (header + per-cell test modals) consistent after a rename.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE evaluation_runs SET model_name = ? WHERE run_id = ?",
                (model_name, run_id)
            )
            updated = cursor.rowcount > 0
            if updated:
                cursor.execute(
                    "UPDATE test_results SET model_name = ? WHERE run_id = ?",
                    (model_name, run_id)
                )
                cursor.execute(
                    "UPDATE individual_test_results SET model_name = ? WHERE run_id = ?",
                    (model_name, run_id)
                )
            conn.commit()
            return updated

    def get_runs_count(self) -> int:
        """Get total count of evaluation runs"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM evaluation_runs")
            return cursor.fetchone()[0]

    def get_run_stats(self, run_id: int) -> Dict[str, Any]:
        """Get statistics for a run"""
        with self._connect() as conn:
            cursor = conn.cursor()

            # Count domain-level tests by status
            cursor.execute(
                """
                SELECT status, COUNT(*) as count
                FROM test_results
                WHERE run_id = ?
                GROUP BY status
                """,
                (run_id,)
            )
            status_counts = {row[0]: row[1] for row in cursor.fetchall()}

            # Average score
            cursor.execute(
                "SELECT AVG(score) FROM test_results WHERE run_id = ? AND score IS NOT NULL",
                (run_id,)
            )
            avg_score = cursor.fetchone()[0] or 0.0

            return {
                'status_counts': status_counts,
                'avg_score': avg_score,
                'total_tests': sum(status_counts.values())
            }

    # ==================== Improvement Cycles ====================

    def create_improvement_cycle(
        self,
        cycle_id: str,
        base_run_id: int,
        analysis: str = None,
        training_data_path: str = None,
        examples_count: int = 0
    ) -> str:
        """Create a new improvement cycle."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO improvement_cycles
                   (cycle_id, base_run_id, status, analysis, training_data_path, examples_count)
                   VALUES (?, ?, 'training_data_ready', ?, ?, ?)""",
                (cycle_id, base_run_id, analysis, training_data_path, examples_count)
            )
            conn.commit()
        return cycle_id

    def complete_improvement_cycle(
        self,
        cycle_id: str,
        improved_run_id: int,
        comparison: str,
        recommendation: str
    ):
        """Mark improvement cycle as completed."""
        completed_at = datetime.now()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE improvement_cycles
                   SET improved_run_id = ?, completed_at = ?, status = 'completed',
                       comparison = ?, recommendation = ?
                   WHERE cycle_id = ?""",
                (improved_run_id, completed_at, comparison, recommendation, cycle_id)
            )
            conn.commit()

    def get_improvement_cycle(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        """Get improvement cycle details."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT cycle_id, base_run_id, improved_run_id, created_at, completed_at, status, analysis, training_data_path, examples_count, comparison, recommendation FROM improvement_cycles WHERE cycle_id = ?",
                (cycle_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_improvement_cycles(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent improvement cycles."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT cycle_id, base_run_id, improved_run_id, created_at, completed_at, status, analysis, training_data_path, examples_count, comparison, recommendation FROM improvement_cycles ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def save_generated_training_data(
        self,
        cycle_id: str,
        examples: List[Dict[str, Any]]
    ):
        """Save generated training examples to database."""
        with self._connect() as conn:
            cursor = conn.cursor()
            for ex in examples:
                cursor.execute(
                    """INSERT INTO generated_training_data
                       (cycle_id, source_test_id, domain, level, prompt, response, tool_calls, rationale)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cycle_id,
                        ex.get('source_test_id'),
                        ex.get('domain'),
                        ex.get('level'),
                        ex.get('prompt'),
                        ex.get('response'),
                        json.dumps(ex.get('tool_calls')) if ex.get('tool_calls') else None,
                        ex.get('rationale')
                    )
                )
            conn.commit()
