"""
safety_pipeline — Orchestrates multiple safety checkers and produces a final verdict.

The pipeline runs each registered :class:`SafetyCheckerBase` in order, aggregates
their scores, and applies threshold logic to produce the same output format that
``check_safety()`` historically returned.

Default checkers:
  1. HeuristicSafetyChecker (built-in system patterns)
  2. CustomRuleChecker (user-defined DB rules)

Usage:
    from backend.tools.lib.safety_pipeline import safety_pipeline

    result = safety_pipeline.check(code, tool_type='bash', agent_context=ctx)
"""
from __future__ import annotations

import logging
from typing import Any

from backend.tools.lib.safety_base import SafetyCheckerBase, CheckResult

logger = logging.getLogger(__name__)


def should_skip_safety(agent: dict | None) -> bool:
    """Return True only when the agent dict explicitly carries ``_skip_safety is True``.

    This helper prevents prompt-injection attacks where an LLM might try to set
    ``_skip_safety`` to a truthy string, integer, or dict.  The flag must be the
    exact boolean ``True``, which can only be set by trusted server-side code
    (e.g. after human approval).
    """
    if agent is None:
        return False
    return agent.get("_skip_safety") is True


def _generate_approval_info(blocked_patterns: list[str], matched_count: int) -> dict:
    """Generate approval information for requires_approval cases."""
    categories = set(blocked_patterns)

    if "sandbox_escape" in categories or "network_exploit" in categories:
        risk_level = "critical"
        description = "This action poses a critical security risk and may compromise the system."
    elif "sql_destructive" in categories:
        risk_level = "high"
        description = "This action performs destructive SQL operations (DROP, TRUNCATE, DELETE) that may permanently destroy data."
    elif "remote_code_execution" in categories or "secure_deletion" in categories:
        risk_level = "high"
        description = "This action may cause significant damage to the system."
    elif "file_destruction" in categories or "disk_overwrite" in categories:
        risk_level = "high"
        description = "This action may permanently delete or overwrite data."
    elif "git_history_rewrite" in categories or "git_branch_deletion" in categories:
        risk_level = "high"
        description = "This action may permanently alter or destroy version history."
    elif "git_staging" in categories:
        risk_level = "medium"
        description = "This action stages all files which may include unintended changes."
    elif "sqlite_access" in categories or "sqlite_db_file" in categories:
        risk_level = "medium"
        description = "This action accesses local SQLite database files which may contain sensitive data."
    elif "privilege_escalation" in categories or "permission_escalation" in categories:
        risk_level = "medium"
        description = "This action may escalate privileges or change permissions."
    else:
        risk_level = "medium"
        description = "This action requires careful consideration."

    return {
        "risk_level": risk_level,
        "description": description,
        "categories": list(categories),
        "pattern_count": matched_count,
    }


class SafetyPipeline:
    """Run all registered checkers and merge into a single verdict."""

    def __init__(self):
        self._checkers: list[SafetyCheckerBase] = []

    def register(self, checker: SafetyCheckerBase) -> None:
        self._checkers.append(checker)

    def check(self, code: str, tool_type: str = 'python', agent_context: dict[str, Any] | None = None) -> dict:
        """Run all checkers and return a final safety result.

        Returns the same dict shape as the legacy ``check_safety()`` function
        so existing callers (bash.py, runpy.py) can switch seamlessly.
        """
        total_score = 0
        all_reasons: list[str] = []
        all_blocked: list[str] = []
        all_matched: list[dict] = []

        for checker in self._checkers:
            try:
                result: CheckResult = checker.check(code, tool_type, agent_context)
            except Exception:
                logger.exception("Safety checker %s failed; skipping", type(checker).__name__)
                continue

            total_score += result.get("score", 0)
            all_reasons.extend(result.get("reasons", []))
            all_blocked.extend(result.get("blocked_patterns", []))
            all_matched.extend(result.get("matched_patterns", []))

        # Deduplicate blocked_patterns
        blocked_deduped = list(set(all_blocked))

        # Determine level using the same thresholds as the legacy system
        if total_score >= 15:
            level = "dangerous"
            requires_approval = False
            approval_info = None
        elif total_score >= 8:
            level = "requires_approval"
            requires_approval = True
            approval_info = _generate_approval_info(blocked_deduped, len(all_matched))
        elif total_score >= 4:
            level = "warning"
            requires_approval = False
            approval_info = None
        else:
            level = "safe"
            requires_approval = False
            approval_info = None

        result_dict = {
            "level": level,
            "score": total_score,
            "reasons": all_reasons,
            "blocked_patterns": blocked_deduped,
            "requires_approval": requires_approval,
            "approval_info": approval_info,
        }

        if level != "safe":
            categories = ", ".join(blocked_deduped) or "-"
            reasons_summary = "; ".join(all_reasons[:3])
            if len(all_reasons) > 3:
                reasons_summary += f" (+ {len(all_reasons) - 3} more)"
            logger.warning(
                "[safety_pipeline] level=%s score=%d tool=%s categories=[%s] reasons: %s",
                level, total_score, tool_type, categories, reasons_summary,
            )

        return result_dict


def _build_default_pipeline() -> SafetyPipeline:
    """Construct the default pipeline with system + custom rule checkers."""
    from backend.tools.lib.heuristic_safety import heuristic_checker
    from backend.tools.lib.custom_rule_checker import custom_rule_checker

    pipeline = SafetyPipeline()
    pipeline.register(heuristic_checker)
    pipeline.register(custom_rule_checker)
    return pipeline


# Module-level singleton (lazy-initialized to avoid circular imports)
_pipeline: SafetyPipeline | None = None


def get_safety_pipeline() -> SafetyPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = _build_default_pipeline()
    return _pipeline


# Convenience alias
safety_pipeline = None  # Will be replaced on first access


def __getattr__(name: str):
    """Lazy initialization of safety_pipeline singleton."""
    if name == "safety_pipeline":
        return get_safety_pipeline()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
