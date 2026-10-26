"""
safety_pipeline — Orchestrates deterministic HMADS checkers and optional Decim Safety.

The pipeline runs each registered :class:`SafetyCheckerBase` in order, aggregates
their scores, and applies threshold logic to produce the same output shape that
``check_safety()`` historically returned.

Default checkers:
  1. HeuristicSafetyChecker (built-in system patterns)
  2. CustomRuleChecker (user-defined DB rules)

Decim Safety (a provider-agnostic *decision model* layer) is layered on top:

* ``check_deterministic()`` runs only the deterministic checkers and preserves
  exact score aggregation and threshold mapping — unchanged HMADS behaviour.
* ``check()`` keeps its legacy signature, computes the deterministic baseline,
  and — when Decim Safety is enabled and in scope — resolves a model decision:

  - ``shadow``  → deterministic HMADS still governs execution; the model only
    provides comparison evidence (and mandatory telemetry).
  - ``enforce`` → a valid, confident model decision governs the public verdict
    (allow→safe, review→requires_approval, block→dangerous).  Any unusable answer
    falls back to the exact deterministic result with sanitized metadata.

No provider failure can escape this module: a broken adapter degrades to the
deterministic pipeline.

Usage:
    from backend.tools.lib.safety_pipeline import get_safety_pipeline

    result = get_safety_pipeline().check(code, tool_type='bash', agent_context=ctx)
"""
from __future__ import annotations

import logging
from typing import Any

from backend.tools.lib.safety_base import SafetyCheckerBase, CheckResult

logger = logging.getLogger(__name__)

# Initial model-first scope.  Kept in sync with decim_safety.SCOPED_TOOL_TYPES.
SCOPED_TOOL_TYPES = frozenset({"bash", "python"})

# Levels that require human intervention or refuse execution.
_UNSAFE_LEVELS = frozenset({"warning", "requires_approval", "dangerous"})

# Rank for agreement calculation: how "strict" a verdict is.
_LEVEL_RANK = {"safe": 0, "warning": 0, "requires_approval": 1, "dangerous": 2}
_DECISION_RANK = {"allow": 0, "review": 1, "block": 2}


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

    def __init__(self, register_defaults: bool = True):
        self._checkers: list[SafetyCheckerBase] = []
        self._decim_resolver = None
        self._telemetry = None
        if register_defaults:
            self._register_defaults()

    def _register_defaults(self) -> None:
        """Register the built-in deterministic checkers (heuristic + custom rules)."""
        from backend.tools.lib.heuristic_safety import heuristic_checker
        from backend.tools.lib.custom_rule_checker import custom_rule_checker

        self.register(heuristic_checker)
        self.register(custom_rule_checker)

    def register(self, checker: SafetyCheckerBase) -> None:
        self._checkers.append(checker)

    # ------------------------------------------------------------------
    # Deterministic HMADS (unchanged aggregation + thresholds)
    # ------------------------------------------------------------------
    def check_deterministic(self, code: str, tool_type: str = "python",
                            agent_context: dict[str, Any] | None = None) -> dict:
        """Run the legacy HMADS checkers without consulting a decision model."""
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
            # Locate the dangerous spans so every surface (web modal, chat card, and
            # messaging channels) can highlight / focus on the risky code instead of
            # showing it as undifferentiated (and head-truncated) text.
            from backend.tools.lib.snippet_focus import build_focus_snippet, compute_highlights
            highlights = compute_highlights(code, all_matched)
            approval_info["highlights"] = highlights
            approval_info["focus_snippet"] = build_focus_snippet(code, highlights)
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
            # Legacy callers keep their keys unchanged; the source label is
            # additive and lets tool boundaries and telemetry distinguish the
            # deterministic path from a model decision.
            "decision_source": "deterministic",
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

    # ------------------------------------------------------------------
    # Source-aware safety (deterministic + optional Decim Safety)
    # ------------------------------------------------------------------
    def check(self, code: str, tool_type: str = "python",
              agent_context: dict[str, Any] | None = None,
              decim_settings: Any = None) -> dict:
        """Apply Decim Safety when operational, otherwise preserve HMADS exactly.

        Deterministic HMADS is always calculated when Decim is attempted so it
        remains available as comparison evidence and as an unchanged fallback.
        """
        deterministic = self.check_deterministic(code, tool_type, agent_context)

        # `safety.command` is the preferred configuration source for DMSS. The
        # bridge only classifies text; this pipeline retains final policy control.
        profile = self._safety_command_profile()
        if profile is not None:
            outcome = self._run_safety_command_profile(code, tool_type, agent_context)
            metadata = {
                "attempted": outcome.attempted,
                "accepted": outcome.decision is not None,
                "profile_key": "safety.command",
                "profile_mode": outcome.mode,
                "migration_source": "decision_profile",
                "fallback_reason": outcome.fallback_reason,
                "correlation_id": outcome.correlation_id,
            }
            if outcome.decision is None:
                result = dict(deterministic)
                result["decision_source"] = "deterministic_fallback"
                result["decim_safety"] = {**metadata, "decision_source": "deterministic_fallback"}
                return result
            metadata.update({"model_decision": outcome.decision,
                             "model_confidence": outcome.confidence,
                             "model_id": outcome.model_id,
                             "provider_key": outcome.provider_key,
                             "model_latency_ms": outcome.latency_ms})
            if outcome.mode == "shadow":
                result = dict(deterministic)
                result["decision_source"] = "deterministic_shadow"
                result["decim_safety"] = {**metadata, "decision_source": "deterministic_shadow"}
                return result
            return self._apply_profile_decision(deterministic, outcome.decision, metadata)

        settings = decim_settings
        if settings is None:
            settings = self._load_settings()
        if settings is None or not settings.enabled or settings.mode == "off" \
                or tool_type not in SCOPED_TOOL_TYPES:
            return deterministic

        try:
            resolver = self._decim_resolver or self._get_resolver()
            resolution = resolver.resolve(code, tool_type, agent_context, settings)
        except Exception:
            logger.exception("Decim Safety resolution failed; using deterministic result")
            result = dict(deterministic)
            result["decision_source"] = "deterministic_fallback"
            result["decim_safety"] = {
                "attempted": True, "accepted": False,
                "decision_source": "deterministic_fallback",
                "fallback_reason": "provider_error",
            }
            self._record(settings, code, tool_type, agent_context, deterministic,
                         model_decision=None, confidence=None, latency_ms=None,
                         fallback_reason="provider_error", decision_source="deterministic_fallback",
                         final_level=deterministic["level"])
            return result

        metadata = {
            "attempted": resolution.attempted,
            "accepted": resolution.decision is not None,
            "fallback_reason": resolution.fallback_reason,
            "correlation_id": resolution.correlation_id,
        }

        if not resolution.attempted:
            return deterministic

        if resolution.decision is None:
            result = dict(deterministic)
            result["decision_source"] = "deterministic_fallback"
            result["decim_safety"] = {**metadata, "decision_source": "deterministic_fallback"}
            self._record(settings, code, tool_type, agent_context, deterministic,
                         model_decision=None, confidence=None, latency_ms=None,
                         fallback_reason=resolution.fallback_reason,
                         decision_source="deterministic_fallback",
                         correlation_id=resolution.correlation_id,
                         final_level=deterministic["level"])
            return result

        decision = resolution.decision
        metadata.update({
            "provider_key": decision.provider_key,
            "policy_version": decision.policy_version,
            "model_id": decision.model_id,
            "model_decision": decision.decision,
            "model_confidence": decision.confidence,
            "model_latency_ms": decision.latency_ms,
        })

        if settings.mode == "shadow":
            result = dict(deterministic)
            result["decision_source"] = "deterministic_shadow"
            result["decim_safety"] = {**metadata, "decision_source": "deterministic_shadow"}
            event_id = self._record(settings, code, tool_type, agent_context, deterministic,
                                    model_decision=decision.decision, confidence=decision.confidence,
                                    latency_ms=decision.latency_ms, fallback_reason=None,
                                    decision_source="deterministic_shadow",
                                    correlation_id=decision.correlation_id,
                                    provider_key=decision.provider_key,
                                    model_id=decision.model_id, policy_version=decision.policy_version,
                                    final_level=deterministic["level"])
            if event_id:
                result["decim_safety"]["event_id"] = event_id
            return result

        # In enforce mode an accepted Decim decision governs the public verdict.
        level_map = {"allow": "safe", "review": "requires_approval", "block": "dangerous"}
        level = level_map[decision.decision]
        result = dict(deterministic)
        result.update({
            "level": level,
            "requires_approval": level == "requires_approval",
            "decision_source": "decim",
            "decim_safety": {**metadata, "decision_source": "decim"},
        })
        if level == "safe":
            result.update({"score": 0, "reasons": [], "blocked_patterns": [], "approval_info": None})
        elif level == "requires_approval":
            approval_info = _generate_approval_info(["decim_review"], 0)
            result.update({
                "score": max(8, deterministic["score"]),
                "reasons": ["Decim Safety requires manual approval."],
                "blocked_patterns": ["decim_review"],
                "approval_info": approval_info,
            })
        else:
            result.update({
                "score": max(15, deterministic["score"]),
                "reasons": ["Decim Safety blocked this execution."],
                "blocked_patterns": ["decim_block"],
                "approval_info": None,
            })

        event_id = self._record(settings, code, tool_type, agent_context, deterministic,
                                model_decision=decision.decision, confidence=decision.confidence,
                                latency_ms=decision.latency_ms, fallback_reason=None,
                                decision_source="decim",
                                correlation_id=decision.correlation_id,
                                provider_key=decision.provider_key,
                                model_id=decision.model_id, policy_version=decision.policy_version,
                                final_level=level)
        if event_id:
            result["decim_safety"]["event_id"] = event_id
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _safety_command_profile(self):
        try:
            from models.db import db
            return db.get_decision_profile("safety.command")
        except Exception:
            return None

    def _run_safety_command_profile(self, code, tool_type, agent_context):
        from backend.dmss_decision_profile import get_safety_command_runner
        boundary = "remote_ssh" if (agent_context or {}).get("ssh_connected") else "sandboxed_docker"
        if not (agent_context or {}).get("sandbox_enabled", 1):
            boundary = "local_process"
        return get_safety_command_runner().decide(code, tool_type, boundary)

    def _apply_profile_decision(self, deterministic, decision, metadata):
        level_map = {"allow": "safe", "review": "requires_approval", "block": "dangerous"}
        requested = level_map[decision]
        level = requested if _LEVEL_RANK[requested] >= _LEVEL_RANK[deterministic["level"]] else deterministic["level"]
        result = dict(deterministic)
        result.update({"level": level, "requires_approval": level == "requires_approval",
                       "decision_source": "decision_profile",
                       "decim_safety": {**metadata, "decision_source": "decision_profile",
                                        "final_reason": "model" if level == requested else "hmads_floor"}})
        if level == deterministic["level"]:
            return result
        if level == "requires_approval":
            result.update({"score": max(8, deterministic["score"]), "reasons": ["Command Safety requires manual approval."],
                           "blocked_patterns": ["safety_command_review"], "approval_info": _generate_approval_info(["safety_command_review"], 0)})
        else:
            result.update({"score": max(15, deterministic["score"]), "reasons": ["Command Safety blocked this execution."],
                           "blocked_patterns": ["safety_command_block"], "approval_info": None})
        return result

    def record_refusal(self, code: str, *, tool_type: str,
                       agent_context: dict | None, verdict: dict,
                       decision_source: str = "hmads") -> str | None:
        """Record a refusal that never reached Decim Safety.

        The safety gate returns *before* the Decim comparison runs, so a blocked
        execution used to leave no trace at all: the positive class was
        structurally invisible (`docs/safety/label-audit.md`).  A refusal is an
        execution outcome, not a comparison sample, so ``record_enforce_events``
        does not gate it — otherwise turning sampling off would blind us exactly
        when something was stopped.  Disabling safety still records nothing.
        """
        try:
            settings = self._load_settings()
            if settings is None or not getattr(settings, "enabled", False):
                return None
            if getattr(settings, "mode", "off") == "off":
                return None
            service = self._telemetry_service()
            return service.record_comparison(
                settings=settings,
                code=code,
                tool_type=tool_type,
                agent_context=agent_context,
                deterministic={
                    "level": verdict.get("level"),
                    "score": verdict.get("score"),
                    "blocked_patterns": verdict.get("blocked_patterns") or [],
                },
                model_decision=None,
                confidence=None,
                latency_ms=None,
                fallback_reason=None,
                decision_source=decision_source,
                final_level=verdict.get("level"),
            )
        except Exception:
            logger.exception("Decim Safety refusal recording failed")
            return None

    def _load_settings(self):
        try:
            from backend.tools.lib.decim_safety import load_decim_settings
            return load_decim_settings()
        except Exception:
            logger.exception("Unable to load Decim Safety settings")
            return None

    def _get_resolver(self):
        from backend.tools.lib.decim_safety import get_decim_safety_resolver
        return get_decim_safety_resolver()

    def _telemetry_service(self):
        if self._telemetry is None:
            from backend.services import decim_safety_telemetry
            self._telemetry = decim_safety_telemetry
        return self._telemetry

    def _record(self, settings, code, tool_type, agent_context, deterministic, *,
                model_decision, confidence, latency_ms, fallback_reason,
                decision_source, correlation_id=None, provider_key=None,
                model_id=None, policy_version=None, final_level=None) -> str | None:
        """Best-effort telemetry recording; never affects execution safety.

        ``shadow`` always records; ``enforce`` records only when explicitly
        enabled via ``record_enforce_events``.
        """
        if settings.mode == "enforce" and not settings.record_enforce_events:
            return None
        try:
            service = self._telemetry_service()
            return service.record_comparison(
                settings=settings,
                code=code,
                tool_type=tool_type,
                agent_context=agent_context,
                deterministic=deterministic,
                model_decision=model_decision,
                confidence=confidence,
                latency_ms=latency_ms,
                fallback_reason=fallback_reason,
                decision_source=decision_source,
                provider_key=provider_key,
                model_id=model_id,
                policy_version=policy_version,
                correlation_id=correlation_id,
                final_level=final_level,
            )
        except Exception:
            logger.exception("Decim Safety telemetry recording failed")
            return None


_LEVEL_TO_DECISION = {
    "safe": "allow",
    "warning": "allow",
    "requires_approval": "review",
    "dangerous": "block",
}


def verdict_provenance(safety: dict | None) -> dict:
    """Return a compact, UI-safe description of who produced a safety verdict.

    Used by the chat timeline to show whether an execution was cleared or
    stopped by Decim Safety (DMSS), by the deterministic rule engine, or by a
    human approval.  Deliberately excludes the provider endpoint, provider key,
    model product name and any credential — the same disclosure policy as
    ``routes/decim_safety.py``.

    Shape::

        {source, mode, level, decision, model_decision, confidence,
         event_id, blocked_patterns[:6], reasons[:4]}
    """
    if not isinstance(safety, dict):
        return {}
    meta = safety.get("decim_safety") or {}
    decision_source = meta.get("decision_source") or safety.get("decision_source")
    if decision_source == "decim":
        source, mode = "dmss", "enforce"
    elif decision_source == "deterministic_shadow":
        source, mode = "rules", "shadow"
    elif decision_source == "deterministic_fallback":
        source, mode = "rules", "fallback"
    else:
        source, mode = "rules", None
    level = safety.get("level") or "safe"
    confidence = meta.get("model_confidence")
    try:
        confidence = round(float(confidence), 3) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return {
        "source": source,
        "mode": mode,
        "level": level,
        "decision": _LEVEL_TO_DECISION.get(level, "allow"),
        "model_decision": meta.get("model_decision"),
        "confidence": confidence,
        "event_id": meta.get("event_id"),
        "blocked_patterns": [str(p) for p in (safety.get("blocked_patterns") or [])[:6]],
        "reasons": [str(r) for r in (safety.get("reasons") or [])[:4]],
    }


def _build_default_pipeline() -> SafetyPipeline:
    """Construct the default pipeline with system + custom rule checkers."""
    return SafetyPipeline()


# Module-level singleton (lazy-initialized to avoid circular imports)
_pipeline: SafetyPipeline | None = None


def get_safety_pipeline() -> SafetyPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = _build_default_pipeline()
    return _pipeline


def reset_safety_pipeline() -> None:
    """Drop the cached singleton (used by tests and settings changes)."""
    global _pipeline
    _pipeline = None


# Convenience alias
safety_pipeline = None  # Will be replaced on first access


def __getattr__(name: str):
    """Lazy initialization of safety_pipeline singleton."""
    if name == "safety_pipeline":
        return get_safety_pipeline()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
