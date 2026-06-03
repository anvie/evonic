"""
SQL Executor Evaluator

Two-pass extraction + SQL execution and result validation.
Used for SQL domain.
"""

import re
from typing import Any, Dict
from .base import BaseEvaluator, EvaluationResult
from evaluator.answer_extractor import answer_extractor
from evaluator.sql_executor import sql_executor

# Compiled regex patterns for _normalize_sql — compiled once at import time
_RE_DATE_TRUNC_MONTH = re.compile(
    r"DATE_TRUNC\s*\(\s*'month'\s*,\s*([^)]+)\)", re.IGNORECASE
)
_RE_DATE_TRUNC_YEAR = re.compile(
    r"DATE_TRUNC\s*\(\s*'year'\s*,\s*([^)]+)\)", re.IGNORECASE
)
_RE_DATE_TRUNC_DAY = re.compile(
    r"DATE_TRUNC\s*\(\s*'day'\s*,\s*([^)]+)\)", re.IGNORECASE
)
_RE_DATE_FORMAT = re.compile(
    r"DATE_FORMAT\s*\(\s*([^,]+),\s*'([^']+)'\s*\)", re.IGNORECASE
)
_RE_NOW_FUNC = re.compile(
    r"\bNOW\s*\(\s*\)", re.IGNORECASE
)
_RE_ILIKE = re.compile(
    r"\bILIKE\b", re.IGNORECASE
)


class SQLExecutorEvaluator(BaseEvaluator):
    """
    SQL evaluation with execution.
    
    1. PASS2: Extract clean SQL query
    2. Execute SQL on test database
    3. Compare results with expected
    """
    
    def __init__(self, domain: str = "sql"):
        self.domain = domain
        self.extractor = answer_extractor
    
    @property
    def name(self) -> str:
        return "sql_executor"
    
    @property
    def uses_pass2(self) -> bool:
        return True
    
    def evaluate(self, response: str, expected: Any, level: int, prompt: str = "") -> EvaluationResult:
        """Evaluate SQL with execution"""
        
        # PASS 2: Extract clean SQL (include original question for context)
        extraction = self.extractor.extract(self.domain, level, response, prompt)
        
        if not extraction["success"]:
            return EvaluationResult(
                score=0.0,
                status="failed",
                details={
                    "error": extraction.get("parse_error", "SQL extraction failed"),
                    "pass2": {
                        "success": False,
                        "error": extraction.get("parse_error")
                    }
                },
                extracted_answer=extraction.get("extracted"),
                pass2_used=True
            )
        
        sql_query = extraction["extracted"]

        # Strip markdown code fences if present (LLM may wrap SQL in ```sql blocks)
        sql_query = re.sub(r'```(?:sql)?\s*', '', sql_query)
        sql_query = sql_query.replace('```', '').strip()

        # Normalize SQL dialect (PostgreSQL/MySQL → SQLite)
        sql_query = self._normalize_sql(sql_query)

        # Extract only the first SQL statement if multiple were returned
        sql_query = self._extract_first_statement(sql_query)

        # Execute SQL
        execution_result = sql_executor.execute_safe_query(sql_query)
        
        if not execution_result.get("success"):
            return EvaluationResult(
                score=0.0,
                status="failed",
                details={
                    "error": execution_result.get("error", "SQL execution failed"),
                    "sql_query": sql_query,
                    "pass2": {
                        "success": True,
                        "format": extraction["expected_format"]
                    }
                },
                extracted_answer=sql_query,
                pass2_used=True
            )
        
        # Score the results using SQL scoring logic
        actual_result = execution_result.get("result", [])
        columns = execution_result.get("columns", [])
        
        score_result = self._score_results(
            sql_query,
            actual_result,
            columns,
            expected,
            level
        )
        
        # Add metadata
        score_result["details"]["sql_query"] = sql_query
        score_result["details"]["row_count"] = len(actual_result)
        score_result["details"]["columns"] = columns
        score_result["details"]["pass2"] = {
            "success": True,
            "format": extraction["expected_format"]
        }
        
        if actual_result:
            score_result["details"]["actual_result_preview"] = actual_result[:3]
        
        return EvaluationResult(
            score=score_result["score"],
            status=score_result.get("status", "passed" if score_result["score"] >= 0.8 else "failed"),
            details=score_result["details"],
            extracted_answer=sql_query,
            pass2_used=True
        )
    
    def _extract_first_statement(self, sql: str) -> str:
        """Extract only the first SQL statement when multiple are present."""
        # Split on semicolons, take the first non-empty statement
        statements = [s.strip() for s in sql.split(';') if s.strip()]
        if statements:
            return statements[0] + ';'
        return sql

    def _normalize_sql(self, sql: str) -> str:
        """Translate common PostgreSQL/MySQL functions to SQLite equivalents."""
        # DATE_TRUNC('month', col) → strftime('%Y-%m', col)
        sql = _RE_DATE_TRUNC_MONTH.sub(r"strftime('%Y-%m', \1)", sql)
        # DATE_TRUNC('year', col) → strftime('%Y', col)
        sql = _RE_DATE_TRUNC_YEAR.sub(r"strftime('%Y', \1)", sql)
        # DATE_TRUNC('day', col) → date(col)
        sql = _RE_DATE_TRUNC_DAY.sub(r"date(\1)", sql)
        # DATE_FORMAT(col, '%Y-%m') → strftime('%Y-%m', col)
        sql = _RE_DATE_FORMAT.sub(r"strftime('\2', \1)", sql)
        # NOW() → datetime('now')
        sql = _RE_NOW_FUNC.sub("datetime('now')", sql)
        # ILIKE → LIKE (SQLite LIKE is case-insensitive for ASCII)
        sql = _RE_ILIKE.sub("LIKE", sql)
        return sql

    def _score_results(self, sql_query: str, actual_result: list, columns: list,
                       expected: Dict, level: int) -> Dict:
        """Score SQL execution results"""
        
        # Multi-factor scoring
        breakdown = {}
        total_score = 0.0
        details_parts = []
        
        # 1. Query Execution (baseline)
        breakdown["execution"] = 1.0
        details_parts.append("✓ Query executed successfully")
        
        # 2. Result Quality Check
        if not actual_result:
            breakdown["results"] = 0.0
            details_parts.append("✗ Query returned no results")
        else:
            breakdown["results"] = 1.0
            details_parts.append(f"✓ Returned {len(actual_result)} rows")
        
        # 3. Column Validation
        if expected and actual_result:
            required_cols = expected.get("required_columns", [])
            forbidden_cols = expected.get("forbidden_columns", [])
            actual_cols = set(columns)

            if not required_cols and not forbidden_cols:
                # No column constraints = automatic pass
                col_score = 1.0
            else:
                col_score = 0.0
                if required_cols:
                    required_found = sum(1 for c in required_cols
                                        if c.lower() in [col.lower() for col in actual_cols])
                    col_score = required_found / len(required_cols)

                    if col_score >= 1.0:
                        details_parts.append(f"✓ All required columns present: {required_cols}")
                    elif col_score > 0:
                        details_parts.append(f"⚠ Partial columns: {required_found}/{len(required_cols)} found")
                    else:
                        details_parts.append(f"✗ Missing required columns: {required_cols}")
                else:
                    col_score = 1.0

                # Check forbidden columns
                if forbidden_cols:
                    forbidden_found = [c for c in forbidden_cols
                                      if c.lower() in [col.lower() for col in actual_cols]]
                    if forbidden_found:
                        col_score *= 0.7  # Penalty
                        details_parts.append(f"⚠ Unwanted columns selected: {forbidden_found}")

            breakdown["columns"] = col_score
        
        # 4. Row Count Validation
        if expected:
            min_rows = expected.get("min_rows")
            max_rows = expected.get("max_rows")
            
            row_score = 1.0
            if min_rows and len(actual_result) < min_rows:
                row_score = len(actual_result) / min_rows
                details_parts.append(f"⚠ Too few rows: {len(actual_result)} (expected ≥{min_rows})")
            elif max_rows and len(actual_result) > max_rows:
                row_score = max_rows / len(actual_result)
                details_parts.append(f"⚠ Too many rows: {len(actual_result)} (expected ≤{max_rows})")
            elif actual_result:
                details_parts.append("✓ Row count acceptable")
            
            breakdown["row_count"] = row_score
        
        # 5. Data Validation
        if expected and actual_result:
            data_score = 1.0
            
            # Basic email format check for level 2
            if level == 2 and 'email' in columns:
                valid_emails = sum(1 for r in actual_result 
                                  if '@' in str(r.get('email', '')))
                data_score *= (valid_emails / len(actual_result)) if actual_result else 0
                if data_score >= 0.9:
                    details_parts.append("✓ Email format valid")
                else:
                    details_parts.append(f"⚠ Some emails invalid: {valid_emails}/{len(actual_result)}")
            
            breakdown["data_quality"] = data_score
        
        # Calculate weighted final score
        weights = {
            "execution": 0.2,
            "results": 0.2,
            "columns": 0.3,
            "row_count": 0.15,
            "data_quality": 0.15
        }
        
        for key, weight in weights.items():
            if key in breakdown:
                total_score += breakdown[key] * weight
            else:
                breakdown[key] = 0.0
        
        # Determine status
        if total_score >= 0.8:
            status = "passed"
        elif total_score >= 0.5:
            status = "partial"
        else:
            status = "failed"
        
        return {
            "score": round(total_score, 3),
            "status": status,
            "details": {
                "details": " | ".join(details_parts),
                "breakdown": breakdown
            }
        }
