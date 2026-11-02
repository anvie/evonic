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
* ``check()`` keeps its legacy signature and — when Decim Safety is enabled and
  in scope — resolves a model decision:

  - ``shadow``  → deterministic HMADS still governs execution; the model only
    provides comparison evidence (and mandatory telemetry).
  - ``enforce`` → the model is consulted first and a valid, confident decision
    governs the public verdict (allow→safe, review→requires_approval,
    block→dangerous) without running HMADS.  Any unusable answer falls back to
    the exact deterministic result with sanitized metadata.

No provider failure can escape this module: a broken adapter degrades to the
deterministic pipeline.

Usage:
    from backend.tools.lib.safety_pipeline import get_safety_pipeline

    result = get_safety_pipeline().check(code, tool_type='bash', agent_context=ctx)
"""
from __future__ import annotations

import logging
import time
from typing import Any

from backend.tools.lib.safety_base import SafetyCheckerBase, CheckResult

logger = logging.getLogger(__name__)

# Initial model-first scope.  Kept in sync with decim_safety.SCOPED_TOOL_TYPES.
SCOPED_TOOL_TYPES = frozenset({"bash", "python"})

# Levels that require human intervention or refuse execution.
_UNSAFE_LEVELS = frozenset({"warning", "requires_approval", "dangerous"})

# Rank for agreement calculation: how "strict" a verdict is.
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


def _threshold_verdict(code: str, total_score: int, all_reasons: list[str],
                       all_blocked: list[str], all_matched: list[dict]) -> dict:
    """Map an aggregated HMADS score onto the legacy verdict thresholds."""
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

    return {
        "level": level,
        "score": total_score,
        "reasons": all_reasons,
        "blocked_patterns": blocked_deduped,
        "requires_approval": requires_approval,
        "approval_info": approval_info,
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
        # score, reasons, blocked_patterns, matched_patterns from user rules only
        user_parts: list[Any] = [0, [], [], []]

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
            if getattr(checker, "user_defined", False):
                user_parts[0] += result.get("score", 0)
                user_parts[1].extend(result.get("reasons", []))
                user_parts[2].extend(result.get("blocked_patterns", []))
                user_parts[3].extend(result.get("matched_patterns", []))

        result_dict = _threshold_verdict(code, total_score, all_reasons, all_blocked, all_matched)
        blocked_deduped = result_dict["blocked_patterns"]
        level = result_dict["level"]
        # Legacy callers keep their keys unchanged; the source label is
        # additive and lets tool boundaries and telemetry distinguish the
        # deterministic path from a model decision.
        result_dict["decision_source"] = "deterministic"
        # The same verdict from user-defined rules alone: only these may floor
        # an accepted DMSS decision (see _apply_hmads_floor).
        result_dict["user_rules"] = _threshold_verdict(code, *user_parts)

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

        In ``enforce`` mode DMSS is consulted first and a confident decision is
        final; HMADS runs only when DMSS is unusable (timeout, error, low
        confidence, ...).  In ``shadow`` mode HMADS governs and always runs.
        """
        # `safety.command` is the preferred configuration source for DMSS. The
        # bridge only classifies text; this pipeline retains final policy control.
        profile = self._safety_command_profile()
        if profile is not None:
            started = time.perf_counter()
            outcome = self._run_safety_command_profile(code, tool_type, agent_context)
            dmss_ms = _elapsed_ms(started)
            metadata = {
                "attempted": outcome.attempted,
                "accepted": outcome.decision is not None,
                "profile_key": "safety.command",
                "profile_mode": outcome.mode,
                "migration_source": "decision_profile",
                "fallback_reason": outcome.fallback_reason,
                "correlation_id": outcome.correlation_id,
            }
            if outcome.decision is not None:
                metadata.update({"model_decision": outcome.decision,
                                 "model_confidence": outcome.confidence,
                                 "model_id": outcome.model_id,
                                 "provider_key": outcome.provider_key,
                                 "model_latency_ms": outcome.latency_ms})
            if outcome.decision is not None and outcome.mode == "enforce":
                deterministic = {}
                result = _model_verdict(outcome.decision, "decision_profile", metadata,
                                        "Command Safety", "safety_command")
                # Same floor contract as the native DMSS path: HMADS may only
                # make an accepted model decision stricter.
                floor_settings = self._load_settings()
                if floor_settings is not None and getattr(floor_settings, "hmads_floor", False):
                    deterministic = self._timed_deterministic(code, tool_type, agent_context)
                    result = _apply_hmads_floor(result, deterministic)
            else:
                deterministic = self._timed_deterministic(code, tool_type, agent_context)
                source = "deterministic_fallback" if outcome.decision is None else "deterministic_shadow"
                result = dict(deterministic)
                result["decision_source"] = source
                result["decim_safety"] = {**metadata, "decision_source": source}
            self._record_profile_outcome(code, tool_type, agent_context, deterministic, outcome,
                                         result, dmss_ms)
            return result

        settings = decim_settings
        if settings is None:
            settings = self._load_settings()
        if settings is None or not settings.enabled or settings.mode == "off" \
                or tool_type not in SCOPED_TOOL_TYPES:
            return self._timed_deterministic(code, tool_type, agent_context)

        # Shadow compares against the HMADS verdict it leaves in charge; enforce
        # only needs HMADS when the model's answer is unusable -- unless the
        # HMADS floor is enabled, in which case HMADS always scores the command
        # so its verdict can raise (never lower) the DMSS severity.
        deterministic = None
        if settings.mode == "shadow" or (
                settings.mode == "enforce" and getattr(settings, "hmads_floor", False)):
            deterministic = self._timed_deterministic(code, tool_type, agent_context)

        started = time.perf_counter()
        try:
            resolver = self._decim_resolver or self._get_resolver()
            resolution = resolver.resolve(code, tool_type, agent_context, settings)
        except Exception:
            logger.exception("Decim Safety resolution failed; using deterministic result")
            deterministic = deterministic or self._timed_deterministic(code, tool_type, agent_context)
            result = dict(deterministic)
            result["decision_source"] = "deterministic_fallback"
            result["decim_safety"] = {
                "attempted": True, "accepted": False,
                "decision_source": "deterministic_fallback",
                "fallback_reason": "provider_error",
            }
            self._record(settings, code, tool_type, agent_context, deterministic,
                         model_decision=None, confidence=None, latency_ms=_elapsed_ms(started),
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
            return deterministic or self._timed_deterministic(code, tool_type, agent_context)

        if resolution.decision is None:
            deterministic = deterministic or self._timed_deterministic(code, tool_type, agent_context)
            result = dict(deterministic)
            result["decision_source"] = "deterministic_fallback"
            result["decim_safety"] = {**metadata, "decision_source": "deterministic_fallback"}
            self._record(settings, code, tool_type, agent_context, deterministic,
                         model_decision=None, confidence=None, latency_ms=_elapsed_ms(started),
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
        # With the HMADS floor enabled, the deterministic verdict already scored
        # above can only make it stricter, never more permissive.
        result = _model_verdict(decision.decision, "decim", metadata, "Decim Safety", "decim")
        if deterministic is not None:
            result = _apply_hmads_floor(result, deterministic)
        level = result["level"]

        event_id = self._record(settings, code, tool_type, agent_context, deterministic or {},
                                model_decision=decision.decision, confidence=decision.confidence,
                                latency_ms=decision.latency_ms, fallback_reason=None,
                                decision_source=result["decision_source"],
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

    def _timed_deterministic(self, code, tool_type, agent_context) -> dict:
        started = time.perf_counter()
        deterministic = self.check_deterministic(code, tool_type, agent_context)
        # Timing is telemetry only; it rides on the verdict so a refusal recorded
        # later by the tool boundary still carries it.
        deterministic["hmads_duration_ms"] = _elapsed_ms(started)
        return deterministic

    def _record_profile_outcome(self, code, tool_type, agent_context, deterministic,
                                outcome, result, dmss_ms) -> None:
        """Record a ``safety.command`` attempt with both the HMADS and DMSS output.

        Without this the profile path left no row for allowed commands, and a
        refusal surfaced only through ``record_refusal`` labelled as HMADS with
        no DMSS verdict.  Retention and enforce sampling come from the Decim
        settings; the mode is the profile's own.
        """
        if not outcome.attempted or outcome.mode not in {"shadow", "enforce"}:
            return
        from dataclasses import replace
        from backend.tools.lib.decim_safety import DecimSettings

        settings = self._load_settings() or DecimSettings()
        settings = replace(settings, enabled=True, mode=outcome.mode)
        event_id = self._record(settings, code, tool_type, agent_context, deterministic,
                                model_decision=outcome.decision, confidence=outcome.confidence,
                                latency_ms=outcome.latency_ms if outcome.latency_ms is not None else dmss_ms,
                                fallback_reason=outcome.fallback_reason,
                                decision_source=result["decision_source"],
                                correlation_id=outcome.correlation_id,
                                provider_key=outcome.provider_key, model_id=outcome.model_id,
                                final_level=result["level"])
        if event_id:
            result["decim_safety"]["event_id"] = event_id

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
                hmads_duration_ms=verdict.get("hmads_duration_ms"),
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

        ``shadow`` always records; ``enforce`` records allowed executions only
        when explicitly enabled via ``record_enforce_events``.  A refusal is an
        execution outcome, not a sample, so it is always recorded here — with
        the DMSS output — rather than later by ``record_refusal`` without it.
        """
        # One audit line per decision, so a missing telemetry row can always be
        # explained from the log alone (policy, not failure).
        audit = ("[dmss-audit] level=%s source=%s tool=%s model_decision=%s "
                 "confidence=%s correlation_id=%s") % (
            final_level, decision_source, tool_type, model_decision,
            confidence, correlation_id,
        )

        if settings.mode == "enforce" and not settings.record_enforce_events \
                and final_level not in {"dangerous", "requires_approval"}:
            logger.info("%s recorded=false reason=record_enforce_events_disabled", audit)
            return None
        try:
            service = self._telemetry_service()
            event_id = service.record_comparison(
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
                hmads_duration_ms=deterministic.get("hmads_duration_ms"),
            )
            if event_id:
                logger.info("%s recorded=true event_id=%s", audit, event_id)
            else:
                logger.info("%s recorded=false reason=store_rejected", audit)
            return event_id
        except Exception:
            logger.exception("Decim Safety telemetry recording failed")
            logger.info("%s recorded=error", audit)
            return None


def _model_verdict(decision: str, source: str, metadata: dict,
                   label: str, pattern_prefix: str) -> dict:
    """Build the public verdict for an accepted enforce-mode model decision."""
    level = {"allow": "safe", "review": "requires_approval", "block": "dangerous"}[decision]
    result = {
        "level": level,
        "score": 0,
        "reasons": [],
        "blocked_patterns": [],
        "requires_approval": level == "requires_approval",
        "approval_info": None,
        "decision_source": source,
        "decim_safety": {**metadata, "decision_source": source},
    }
    if level == "requires_approval":
        result.update({
            "score": 8,
            "reasons": [f"{label} requires manual approval."],
            "blocked_patterns": [f"{pattern_prefix}_review"],
            "approval_info": _generate_approval_info([f"{pattern_prefix}_review"], 0),
        })
    elif level == "dangerous":
        result.update({
            "score": 15,
            "reasons": [f"{label} blocked this execution."],
            "blocked_patterns": [f"{pattern_prefix}_block"],
        })
    return result


# Severity order used by the HMADS floor: a floor may only move a verdict up.
_LEVEL_RANK = {"safe": 0, "warning": 1, "requires_approval": 2, "dangerous": 3}


def _apply_hmads_floor(result: dict, deterministic: dict) -> dict:
    """Raise *result* to the user-rule verdict when it is stricter.

    The HMADS floor exists because a model cannot know platform-local policy
    (``git add -A`` is a local staging rule, not a general security rule), so
    only rules the user defined in HMADS may floor a confident DMSS decision;
    built-in rules never do.  A floored verdict keeps the DMSS metadata for
    auditing but is attributed to the rules (`decision_source="decim_floor"`),
    and it can never be *more* permissive than the DMSS decision.
    """
    model_level = result.get("level") or "safe"
    user_verdict = (deterministic or {}).get("user_rules") or {}
    floor_level = user_verdict.get("level") or "safe"
    meta = dict(result.get("decim_safety") or {})
    if _LEVEL_RANK.get(floor_level, 0) <= _LEVEL_RANK.get(model_level, 0):
        meta["hmads_floor"] = floor_level
        result["decim_safety"] = meta
        return result

    floored = dict(user_verdict)
    floored["decision_source"] = "decim_floor"
    floored["decim_safety"] = {
        **meta,
        "decision_source": "decim_floor",
        "model_level": model_level,
        "hmads_floor": floor_level,
    }
    floored["reasons"] = list(user_verdict.get("reasons") or []) + [
        f"User HMADS rule raised the DMSS verdict ({model_level} → {floor_level})."
    ]
    floored["requires_approval"] = floor_level in {"requires_approval", "dangerous"}
    logger.warning(
        "[dmss] User HMADS rule raised the model verdict: model=%s floor=%s categories=%s",
        model_level, floor_level, ",".join(user_verdict.get("blocked_patterns") or []),
    )
    return floored


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


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
    elif decision_source == "decim_floor":
        # DMSS answered, HMADS overruled it: attribute the decision to the rules
        # so the badge never claims the model cleared something it did not.
        source, mode = "rules", "enforce"
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
