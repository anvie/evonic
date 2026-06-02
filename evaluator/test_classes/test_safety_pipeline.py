"""
Test suite for the HMADS safety pipeline, custom rule checker, and safety base classes.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.tools.lib.safety_base import SafetyCheckerBase, CheckResult
from backend.tools.lib.heuristic_safety import HeuristicSafetyChecker, check_safety
from backend.tools.lib.safety_pipeline import SafetyPipeline


# ============================================================================
# SafetyCheckerBase / HeuristicSafetyChecker
# ============================================================================

class TestHeuristicCheckerClass:
    """Verify the class wrapper returns consistent results with the legacy function."""

    def test_safe_code_returns_zero_score(self):
        checker = HeuristicSafetyChecker()
        result = checker.check("print('hello')", tool_type='python')
        assert result['score'] == 0
        assert result['reasons'] == []

    def test_dangerous_code_scores_match_legacy(self):
        checker = HeuristicSafetyChecker()
        result = checker.check("import ctypes", tool_type='python')
        legacy = check_safety("import ctypes", tool_type='python')
        # Class returns raw score; legacy adds threshold logic
        # The class score should match the legacy score
        assert result['score'] == legacy['score']

    def test_bash_rm_rf(self):
        checker = HeuristicSafetyChecker()
        result = checker.check("rm -rf /var", tool_type='bash')
        assert result['score'] >= 8
        assert 'file_destruction' in result['blocked_patterns']

    def test_agent_context_passed_through(self):
        checker = HeuristicSafetyChecker()
        ctx = {'agent_id': 'test-agent', 'is_super': False}
        result = checker.check("print(1)", tool_type='python', agent_context=ctx)
        assert result['score'] == 0


# ============================================================================
# SafetyPipeline
# ============================================================================

class _DummyChecker(SafetyCheckerBase):
    """A test checker that always returns a fixed score."""
    def __init__(self, score, reason="dummy reason", category="dummy"):
        self._score = score
        self._reason = reason
        self._category = category

    def check(self, code, tool_type='python', agent_context=None):
        if self._score == 0:
            return CheckResult(score=0, reasons=[], matched_patterns=[], blocked_patterns=[])
        return CheckResult(
            score=self._score,
            reasons=[self._reason],
            matched_patterns=[{
                'pattern': 'dummy',
                'weight': self._score,
                'category': self._category,
                'description': self._reason,
            }],
            blocked_patterns=[self._category],
        )


class TestSafetyPipeline:

    def test_empty_pipeline_is_safe(self):
        pipeline = SafetyPipeline()
        result = pipeline.check("rm -rf /", tool_type='bash')
        assert result['level'] == 'safe'
        assert result['score'] == 0

    def test_single_checker(self):
        pipeline = SafetyPipeline()
        pipeline.register(_DummyChecker(score=10, category="test_cat"))
        result = pipeline.check("anything", tool_type='bash')
        assert result['level'] == 'requires_approval'
        assert result['score'] == 10
        assert 'test_cat' in result['blocked_patterns']

    def test_scores_aggregate_across_checkers(self):
        pipeline = SafetyPipeline()
        pipeline.register(_DummyChecker(score=3, category="cat_a"))
        pipeline.register(_DummyChecker(score=3, category="cat_b"))
        result = pipeline.check("anything")
        # 3 + 3 = 6 → warning level
        assert result['level'] == 'warning'
        assert result['score'] == 6

    def test_dangerous_threshold(self):
        pipeline = SafetyPipeline()
        pipeline.register(_DummyChecker(score=8, category="cat_a"))
        pipeline.register(_DummyChecker(score=8, category="cat_b"))
        result = pipeline.check("anything")
        # 8 + 8 = 16 → dangerous
        assert result['level'] == 'dangerous'
        assert result['score'] == 16

    def test_safe_threshold(self):
        pipeline = SafetyPipeline()
        pipeline.register(_DummyChecker(score=0))
        pipeline.register(_DummyChecker(score=0))
        result = pipeline.check("safe code")
        assert result['level'] == 'safe'
        assert result['score'] == 0

    def test_checker_exception_is_caught(self):
        """A failing checker should not crash the pipeline."""
        class _BrokenChecker(SafetyCheckerBase):
            def check(self, code, tool_type='python', agent_context=None):
                raise RuntimeError("boom")

        pipeline = SafetyPipeline()
        pipeline.register(_BrokenChecker())
        pipeline.register(_DummyChecker(score=5, category="fallback"))
        result = pipeline.check("test")
        assert result['level'] == 'warning'
        assert result['score'] == 5

    def test_approval_info_generated(self):
        pipeline = SafetyPipeline()
        pipeline.register(_DummyChecker(score=10, category="file_destruction"))
        result = pipeline.check("test")
        assert result['requires_approval'] is True
        assert result['approval_info'] is not None
        assert result['approval_info']['risk_level'] == 'high'

    def test_heuristic_checker_in_pipeline(self):
        """The real HeuristicSafetyChecker should work inside the pipeline."""
        pipeline = SafetyPipeline()
        pipeline.register(HeuristicSafetyChecker())
        result = pipeline.check("import ctypes", tool_type='python')
        assert result['level'] == 'dangerous' or result['score'] >= 12

    def test_output_format_matches_legacy(self):
        """Pipeline output must have all keys the legacy check_safety() returned."""
        pipeline = SafetyPipeline()
        pipeline.register(HeuristicSafetyChecker())
        result = pipeline.check("print(1)")
        required_keys = {'level', 'score', 'reasons', 'blocked_patterns',
                         'requires_approval', 'approval_info'}
        assert required_keys.issubset(result.keys())


# ============================================================================
# CustomRuleChecker (unit tests without DB)
# ============================================================================

class TestCustomRuleCheckerUnit:
    """Test CustomRuleChecker logic without a real database."""

    def test_regex_evaluation(self):
        """Directly test that the checker can evaluate cached rules."""
        from backend.tools.lib.custom_rule_checker import CustomRuleChecker
        import re

        checker = CustomRuleChecker()
        # Manually inject compiled rules into cache
        checker._cache["__global__"] = {
            "rules": [
                {
                    "id": "test-1",
                    "name": "Block AWS keys",
                    "pattern": r"AKIA[0-9A-Z]{16}",
                    "weight": 12,
                    "category": "credential_leak",
                    "description": "AWS access key detected",
                    "tool_scope": "all",
                    "agent_id": None,
                    "is_system": False,
                    "_compiled": re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),
                }
            ],
            "ts": __import__("time").monotonic(),
        }

        # Should match
        result = checker.check("export AWS_KEY=AKIAIOSFODNN7EXAMPLE", tool_type='bash')
        assert result['score'] == 12
        assert 'credential_leak' in result['blocked_patterns']

        # Should not match
        result2 = checker.check("echo hello", tool_type='bash')
        assert result2['score'] == 0

    def test_tool_scope_filtering(self):
        """Rules with tool_scope='python' should not match bash code."""
        from backend.tools.lib.custom_rule_checker import CustomRuleChecker
        import re

        checker = CustomRuleChecker()
        checker._cache["__global__"] = {
            "rules": [
                {
                    "id": "py-only",
                    "name": "Python only rule",
                    "pattern": r"eval\(",
                    "weight": 8,
                    "category": "code_exec",
                    "description": "eval() call",
                    "tool_scope": "python",
                    "agent_id": None,
                    "is_system": False,
                    "_compiled": re.compile(r"eval\(", re.IGNORECASE),
                }
            ],
            "ts": __import__("time").monotonic(),
        }

        # Should match for python
        result = checker.check("eval('1+1')", tool_type='python')
        assert result['score'] == 8

        # Should NOT match for bash
        result2 = checker.check("eval('1+1')", tool_type='bash')
        assert result2['score'] == 0

    def test_category_deduplication(self):
        """Only highest-weight rule per category should count."""
        from backend.tools.lib.custom_rule_checker import CustomRuleChecker
        import re

        checker = CustomRuleChecker()
        checker._cache["__global__"] = {
            "rules": [
                {
                    "id": "r1", "name": "low", "pattern": r"secret", "weight": 3,
                    "category": "secrets", "description": "low secret",
                    "tool_scope": "all", "agent_id": None, "is_system": False,
                    "_compiled": re.compile(r"secret", re.IGNORECASE),
                },
                {
                    "id": "r2", "name": "high", "pattern": r"SECRET_KEY", "weight": 10,
                    "category": "secrets", "description": "high secret",
                    "tool_scope": "all", "agent_id": None, "is_system": False,
                    "_compiled": re.compile(r"SECRET_KEY", re.IGNORECASE),
                },
            ],
            "ts": __import__("time").monotonic(),
        }

        result = checker.check("export SECRET_KEY=abc", tool_type='bash')
        # Both match, but only highest weight (10) should count
        assert result['score'] == 10
        assert len(result['matched_patterns']) == 1
