import sqlite3
import json
from typing import Dict, Any, List, Optional


class TestingMixin:
    """Domains, levels, tests, evaluators, level scores, and individual results CRUD.
    Requires self._connect() from the host class."""

    # ==================== Domains ====================

    def get_domains(self) -> List[Dict[str, Any]]:
        """Get all domains"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, icon, color, evaluator_id, system_prompt, system_prompt_mode, enabled, path, created_at, updated_at, tool_ids FROM domains ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]

    def get_domain(self, domain_id: str) -> Optional[Dict[str, Any]]:
        """Get a single domain by ID"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, icon, color, evaluator_id, system_prompt, system_prompt_mode, enabled, path, created_at, updated_at, tool_ids FROM domains WHERE id = ?", (domain_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_domain_enabled_states(self) -> Dict[str, bool]:
        """Get enabled state for all domains from DB"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, enabled FROM domains")
            return {row['id']: bool(row['enabled']) for row in cursor.fetchall()}

    def upsert_domain(self, domain: Dict[str, Any]) -> str:
        """Insert or update a domain"""
        with self._connect() as conn:
            cursor = conn.cursor()
            system_prompt = domain.get('system_prompt')
            system_prompt_mode = domain.get('system_prompt_mode', 'overwrite')

            # If 'enabled' not explicitly provided, preserve existing DB value
            if 'enabled' not in domain:
                cursor.execute("SELECT enabled FROM domains WHERE id = ?", (domain['id'],))
                row = cursor.fetchone()
                enabled = bool(row[0]) if row else True
            else:
                enabled = domain['enabled']

            cursor.execute("""
                INSERT INTO domains (id, name, description, icon, color, evaluator_id, system_prompt, system_prompt_mode, enabled, path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    icon = excluded.icon,
                    color = excluded.color,
                    evaluator_id = excluded.evaluator_id,
                    system_prompt = excluded.system_prompt,
                    system_prompt_mode = excluded.system_prompt_mode,
                    enabled = excluded.enabled,
                    path = excluded.path,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                domain['id'], domain.get('name'), domain.get('description'),
                domain.get('icon'), domain.get('color'), domain.get('evaluator_id'),
                system_prompt, system_prompt_mode, enabled, domain.get('path')
            ))
            conn.commit()
        return domain['id']

    def delete_domain(self, domain_id: str) -> bool:
        """Delete a domain"""
        with self._connect() as conn:
            cursor = conn.cursor()
            # First delete all tests in this domain
            cursor.execute("DELETE FROM tests WHERE domain_id = ?", (domain_id,))
            # Delete level definitions
            cursor.execute("DELETE FROM levels WHERE domain_id = ?", (domain_id,))
            # Then delete the domain
            cursor.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ==================== Levels ====================

    def upsert_level(self, level_data: Dict[str, Any]) -> None:
        """Insert or update a level definition"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO levels (domain_id, level, system_prompt, system_prompt_mode, path, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(domain_id, level) DO UPDATE SET
                    system_prompt = excluded.system_prompt,
                    system_prompt_mode = excluded.system_prompt_mode,
                    path = excluded.path,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                level_data['domain_id'], level_data['level'],
                level_data.get('system_prompt'), level_data.get('system_prompt_mode', 'overwrite'),
                level_data.get('path')
            ))
            conn.commit()

    def get_level(self, domain_id: str, level: int) -> Optional[Dict[str, Any]]:
        """Get a single level definition"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT domain_id, level, system_prompt, system_prompt_mode, path, updated_at, tool_ids FROM levels WHERE domain_id = ? AND level = ?",
                (domain_id, level)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_levels_for_domain(self, domain_id: str) -> List[Dict[str, Any]]:
        """Get all level definitions for a domain"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT domain_id, level, system_prompt, system_prompt_mode, path, updated_at, tool_ids FROM levels WHERE domain_id = ? ORDER BY level",
                (domain_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_levels_for_domain(self, domain_id: str) -> None:
        """Delete all level definitions for a domain"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM levels WHERE domain_id = ?", (domain_id,))
            conn.commit()

    # ==================== Tests ====================

    def get_tests(self, domain_id: str = None, level: int = None) -> List[Dict[str, Any]]:
        """Get tests, optionally filtered by domain and level"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if domain_id and level:
                cursor.execute(
                    "SELECT id, domain_id, level, name, description, system_prompt, system_prompt_mode, prompt, expected, evaluator_id, timeout_ms, weight, enabled, path, created_at, updated_at, tool_ids FROM tests WHERE domain_id = ? AND level = ? ORDER BY name",
                    (domain_id, level)
                )
            elif domain_id:
                cursor.execute(
                    "SELECT id, domain_id, level, name, description, system_prompt, system_prompt_mode, prompt, expected, evaluator_id, timeout_ms, weight, enabled, path, created_at, updated_at, tool_ids FROM tests WHERE domain_id = ? ORDER BY level, name",
                    (domain_id,)
                )
            elif level:
                cursor.execute(
                    "SELECT id, domain_id, level, name, description, system_prompt, system_prompt_mode, prompt, expected, evaluator_id, timeout_ms, weight, enabled, path, created_at, updated_at, tool_ids FROM tests WHERE level = ? ORDER BY domain_id, name",
                    (level,)
                )
            else:
                cursor.execute("SELECT id, domain_id, level, name, description, system_prompt, system_prompt_mode, prompt, expected, evaluator_id, timeout_ms, weight, enabled, path, created_at, updated_at, tool_ids FROM tests ORDER BY domain_id, level, name")

            return [dict(row) for row in cursor.fetchall()]

    def get_test(self, test_id: str) -> Optional[Dict[str, Any]]:
        """Get a single test by ID"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, domain_id, level, name, description, system_prompt, system_prompt_mode, prompt, expected, evaluator_id, timeout_ms, weight, enabled, path, created_at, updated_at, tool_ids FROM tests WHERE id = ?", (test_id,))
            row = cursor.fetchone()
            result = dict(row) if row else None
            if result and result.get('expected'):
                result['expected'] = json.loads(result['expected'])
            return result

    def upsert_test(self, test: Dict[str, Any]) -> str:
        """Insert or update a test"""
        with self._connect() as conn:
            cursor = conn.cursor()
            expected_json = json.dumps(test.get('expected')) if test.get('expected') else None
            system_prompt = test.get('system_prompt')
            system_prompt_mode = test.get('system_prompt_mode', 'overwrite')
            cursor.execute("""
                INSERT INTO tests (id, domain_id, level, name, description, system_prompt, system_prompt_mode, prompt, expected, evaluator_id, timeout_ms, weight, enabled, path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    domain_id = excluded.domain_id,
                    level = excluded.level,
                    name = excluded.name,
                    description = excluded.description,
                    system_prompt = excluded.system_prompt,
                    system_prompt_mode = excluded.system_prompt_mode,
                    prompt = excluded.prompt,
                    expected = excluded.expected,
                    evaluator_id = excluded.evaluator_id,
                    timeout_ms = excluded.timeout_ms,
                    weight = excluded.weight,
                    enabled = excluded.enabled,
                    path = excluded.path,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                test['id'], test['domain_id'], test['level'], test.get('name'),
                test.get('description'), system_prompt, system_prompt_mode, test['prompt'], expected_json,
                test.get('evaluator_id'), test.get('timeout_ms', 30000),
                test.get('weight', 1.0), test.get('enabled', True), test.get('path')
            ))
            conn.commit()
        return test['id']

    def delete_test(self, test_id: str) -> bool:
        """Delete a test"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tests WHERE id = ?", (test_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_tests_by_domain_level(self, domain_id: str, level: int) -> List[Dict[str, Any]]:
        """Get all tests for a specific domain and level"""
        return self.get_tests(domain_id=domain_id, level=level)

    # ==================== Evaluators ====================

    def get_evaluators(self) -> List[Dict[str, Any]]:
        """Get all evaluators"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, description, eval_prompt, extraction_regex, uses_pass2, config, path, created_at, updated_at FROM evaluators ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]

    def get_evaluator(self, evaluator_id: str) -> Optional[Dict[str, Any]]:
        """Get a single evaluator by ID"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, description, eval_prompt, extraction_regex, uses_pass2, config, path, created_at, updated_at FROM evaluators WHERE id = ?", (evaluator_id,))
            row = cursor.fetchone()
            result = dict(row) if row else None
            if result and result.get('config'):
                result['config'] = json.loads(result['config'])
            return result

    def upsert_evaluator(self, evaluator: Dict[str, Any]) -> str:
        """Insert or update an evaluator"""
        with self._connect() as conn:
            cursor = conn.cursor()
            config_json = json.dumps(evaluator.get('config')) if evaluator.get('config') else None
            cursor.execute("""
                INSERT INTO evaluators (id, name, type, description, eval_prompt, extraction_regex, uses_pass2, config, path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    description = excluded.description,
                    eval_prompt = excluded.eval_prompt,
                    extraction_regex = excluded.extraction_regex,
                    uses_pass2 = excluded.uses_pass2,
                    config = excluded.config,
                    path = excluded.path,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                evaluator['id'], evaluator.get('name'), evaluator.get('type'),
                evaluator.get('description'), evaluator.get('eval_prompt'),
                evaluator.get('extraction_regex'), evaluator.get('uses_pass2', False),
                config_json, evaluator.get('path')
            ))
            conn.commit()
        return evaluator['id']

    def delete_evaluator(self, evaluator_id: str) -> bool:
        """Delete an evaluator"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM evaluators WHERE id = ?", (evaluator_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ==================== Level Scores & Individual Test Results ====================

    def save_level_score(self, run_id: int, domain: str, level: int,
                         average_score: float, total_tests: int, passed_tests: int):
        """Save aggregated level score"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO level_scores (run_id, domain, level, average_score, total_tests, passed_tests)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, domain, level) DO UPDATE SET
                    average_score = excluded.average_score,
                    total_tests = excluded.total_tests,
                    passed_tests = excluded.passed_tests
            """, (run_id, domain, level, average_score, total_tests, passed_tests))
            conn.commit()

    def get_level_scores(self, run_id: int) -> List[Dict[str, Any]]:
        """Get all level scores for a run"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, run_id, domain, level, average_score, total_tests, passed_tests, created_at FROM level_scores WHERE run_id = ? ORDER BY domain, level",
                (run_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def save_individual_test_result(self, run_id: int, test_id: str, domain: str, level: int,
                                    prompt: str, response: str, expected: str, score: float,
                                    status: str, details: str, duration_ms: int, model_name: str,
                                    system_prompt: str = None, system_prompt_mode: str = None,
                                    weight: float = 1.0, evaluator_id: str = None,
                                    definition_hash: str = None):
        """Save individual test result with optional system_prompt and mode"""
        from evaluator.points import max_points, earned_points
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO individual_test_results
                (run_id, test_id, domain, level, prompt, response, expected, score, status, details, duration_ms, model_name, system_prompt, system_prompt_mode,
                 weight, max_points, points, evaluator_id, definition_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, test_id, domain, level, prompt, response, expected, score, status, details, duration_ms, model_name, system_prompt, system_prompt_mode,
                  weight, max_points(level, weight), earned_points(score, level, weight), evaluator_id, definition_hash))
            conn.commit()

    def get_individual_test_results(self, run_id: int, domain: str = None, level: int = None) -> List[Dict[str, Any]]:
        """Get individual test results for a run - prioritize saved resolved system_prompt"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Select from individual_test_results table directly (has the saved resolved prompt)
            # JOIN is only needed to get domain name for reference, not for prompt
            if domain and level:
                cursor.execute("""
                    SELECT itr.*, d.name as domain_name
                    FROM individual_test_results itr
                    LEFT JOIN domains d ON itr.domain = d.id
                    WHERE itr.run_id = ? AND itr.domain = ? AND itr.level = ?
                """, (run_id, domain, level))
            elif domain:
                cursor.execute("""
                    SELECT itr.*, d.name as domain_name
                    FROM individual_test_results itr
                    LEFT JOIN domains d ON itr.domain = d.id
                    WHERE itr.run_id = ? AND itr.domain = ?
                """, (run_id, domain))
            else:
                cursor.execute("""
                    SELECT itr.*, d.name as domain_name
                    FROM individual_test_results itr
                    LEFT JOIN domains d ON itr.domain = d.id
                    WHERE itr.run_id = ? ORDER BY itr.domain, itr.level
                """, (run_id,))

            return [dict(row) for row in cursor.fetchall()]

    def delete_individual_test_result(self, run_id: int, test_id: str) -> None:
        """Delete individual test result(s) for a run+test_id so replay can replace them"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM individual_test_results WHERE run_id = ? AND test_id = ?",
                (run_id, test_id)
            )
            conn.commit()

    def get_individual_test_result_by_id(self, result_id: int) -> Optional[Dict[str, Any]]:
        """Get a single individual test result by its primary key"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT itr.*, d.name as domain_name
                FROM individual_test_results itr
                LEFT JOIN domains d ON itr.domain = d.id
                WHERE itr.id = ?
            """, (result_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_last_run(self) -> Optional[Dict[str, Any]]:
        """Get the most recent evaluation run"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT run_id, started_at, completed_at, model_name, summary, overall_score, total_tokens, total_duration_ms, notes, status, selected_domains FROM evaluation_runs
                ORDER BY started_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_last_run_id(self) -> Optional[int]:
        """Get the most recent evaluation run ID"""
        run = self.get_last_run()
        return run["run_id"] if run else None
