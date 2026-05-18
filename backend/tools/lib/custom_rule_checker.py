"""
custom_rule_checker — DB-backed regex safety checker.

Loads user-defined rules from the ``safety_rules`` table, compiles them once,
and evaluates them against code.  Results are returned as a :class:`CheckResult`
so the :class:`SafetyPipeline` can merge them with system (heuristic) results.

Rules are cached in memory with a short TTL to avoid hitting the DB on every
tool call while still picking up changes quickly.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from backend.tools.lib.safety_base import SafetyCheckerBase, CheckResult

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 5


class CustomRuleChecker(SafetyCheckerBase):
    """Evaluates user-defined regex rules stored in the database."""

    def __init__(self):
        self._cache: dict[str, Any] = {}  # key → {rules, ts}

    def _get_rules(self, agent_id: str | None) -> list[dict]:
        """Load and compile rules applicable to a given agent (global + assigned specific)."""
        cache_key = agent_id or "__global__"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and (now - cached["ts"]) < _CACHE_TTL_SECONDS:
            return cached["rules"]

        from models.db import db

        if agent_id:
            raw_rules = db.get_safety_rules_for_agent(agent_id, enabled_only=True)
        else:
            # No agent context — only global rules apply
            raw_rules = [r for r in db.get_safety_rules(enabled_only=True) if r.get("scope") == "global"]

        # Filter out system rules — those are handled by HeuristicSafetyChecker
        raw_rules = [r for r in raw_rules if not r.get("is_system")]

        compiled = []
        for r in raw_rules:
            try:
                compiled.append({
                    **r,
                    "_compiled": re.compile(r["pattern"], re.IGNORECASE),
                })
            except re.error as exc:
                logger.warning("Skipping invalid regex in safety rule %s: %s", r["id"], exc)

        self._cache[cache_key] = {"rules": compiled, "ts": now}
        return compiled

    def invalidate_cache(self):
        """Invalidate all cached rules. Called after rule CRUD or assignment changes."""
        self._cache.clear()

    def check(self, code: str, tool_type: str = 'python', agent_context: dict[str, Any] | None = None) -> CheckResult:
        agent_id = (agent_context or {}).get("agent_id")
        rules = self._get_rules(agent_id)

        score = 0
        reasons: list[str] = []
        matched_patterns: list[dict] = []
        blocked_patterns: list[str] = []

        # Deduplicate by category — keep highest weight per category
        best_by_category: dict[str, dict] = {}

        for rule in rules:
            # Filter by tool_scope
            scope = rule.get("tool_scope", "all")
            if scope != "all" and scope != tool_type:
                continue

            if rule["_compiled"].search(code):
                cat = rule["category"]
                if cat not in best_by_category or rule["weight"] > best_by_category[cat]["weight"]:
                    best_by_category[cat] = rule

        for rule in best_by_category.values():
            score += rule["weight"]
            reasons.append(rule.get("description") or rule["name"])
            matched_patterns.append({
                "pattern": rule["pattern"],
                "weight": rule["weight"],
                "category": rule["category"],
                "description": rule.get("description") or rule["name"],
            })
            blocked_patterns.append(rule["category"])

        return CheckResult(
            score=score,
            reasons=reasons,
            matched_patterns=matched_patterns,
            blocked_patterns=blocked_patterns,
        )


# Singleton
custom_rule_checker = CustomRuleChecker()
