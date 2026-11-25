"""Bounded built-in consumers for versioned Decision Profiles.

The helpers in this module are deliberately policy-neutral: a profile result is
used only after the generic :class:`DecisionRuntime` validates it.  Any missing,
disabled, invalid, unavailable, low-confidence, malformed, or timed-out profile
returns ``None`` so the caller keeps its pre-existing behaviour.

Inputs are allowlisted and compacted here rather than passing messages, tool
output, or session transcripts to a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Mapping

from backend.tools.lib.decision_models import DecisionCancellation, FieldKind
from backend.tools.lib.decision_runtime import DecisionRuntime, DecisionRuntimeError, RuntimeRequest

_LOG = logging.getLogger(__name__)
PROFILE_VERSION = "1"
TIMEOUT_MS = 3_000
MAX_TEXT_CHARS = 2_000

TASK_COMPLEXITY = "agent.task_complexity"
PATH_BOUNDARY = "agent.path_boundary"
GOAL_COMPLETION = "agent.goal_completion"
SKILL_WORTHINESS = "agent.skill_worthiness"


@dataclass(frozen=True)
class BuiltinDecisionSpec:
    """Reviewed contract and Playground-safe input description for one consumer."""

    key: str
    fields: tuple[dict[str, Any], ...]
    input_fields: tuple[str, ...]
    playground_preset: Mapping[str, Any]

    @property
    def schema(self) -> dict[str, Any]:
        return {"version": "decision-schema/v1", "fields": list(self.fields)}


BUILTIN_DECISION_SPECS = {
    TASK_COMPLEXITY: BuiltinDecisionSpec(
        TASK_COMPLEXITY,
        ({"key": "complexity", "kind": FieldKind.CHOICE.value,
          "description": "Whether the bounded task request needs planning.",
          "choices": ["trivial", "complex"]},),
        ("task_text",),
        {"task_text": "Add a README section explaining installation."},
    ),
    PATH_BOUNDARY: BuiltinDecisionSpec(
        PATH_BOUNDARY,
        ({"key": "belongs_to_current", "kind": FieldKind.BOOLEAN.value,
          "description": "Whether the incoming request belongs to the current task path."},),
        ("user_text", "active_goal", "active_title"),
        {"user_text": "Please add one more test for the current feature.",
         "active_goal": "Add validation to the upload endpoint.", "active_title": "Upload validation"},
    ),
    GOAL_COMPLETION: BuiltinDecisionSpec(
        GOAL_COMPLETION,
        ({"key": "decision", "kind": FieldKind.CHOICE.value,
          "description": "Whether the explicitly supplied goal is complete, should continue, or is blocked.",
          "choices": ["complete", "continue", "blocked"]},),
        ("goal", "candidate_response", "task_status", "nudges_used", "max_nudges"),
        {"goal": "Add validation to the upload endpoint.",
         "candidate_response": "Validation and tests are complete.",
         "task_status": ["done: Add validation", "done: Add tests"],
         "nudges_used": 0, "max_nudges": 5},
    ),
    SKILL_WORTHINESS: BuiltinDecisionSpec(
        SKILL_WORTHINESS,
        ({"key": "action", "kind": FieldKind.CHOICE.value,
          "description": "Whether the finished turn is worth keeping as a reusable learned skill: "
                         "create a new one, update an existing one, or skip.",
          "choices": ["skip", "create", "update"]},),
        ("user_request", "tool_sequence", "final_response", "existing_skills"),
        {"user_request": "Turn the monthly report markdown into a PDF.",
         "tool_sequence": ["bash:error", "bash:error", "write_file:ok", "bash:ok"],
         "final_response": "The PDF is at reports/monthly.pdf.",
         "existing_skills": ["csv-cleanup: Normalise exported CSV files."]},
    ),
}


def _compact(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def task_complexity_input(task_text: Any) -> dict[str, str]:
    return {"task_text": _compact(task_text)}


def path_boundary_input(user_text: Any, active_goal: Any, active_title: Any) -> dict[str, str]:
    return {"user_text": _compact(user_text), "active_goal": _compact(active_goal, 600),
            "active_title": _compact(active_title, 200)}


def goal_completion_input(goal: Any, candidate_response: Any, tasks: Any,
                          nudges_used: Any, max_nudges: Any) -> dict[str, Any]:
    status: list[str] = []
    if isinstance(tasks, (list, tuple)):
        for task in tasks[:12]:
            if isinstance(task, Mapping):
                text = _compact(task.get("text"), 180)
                if text:
                    status.append(f"{_compact(task.get('status'), 30) or 'pending'}: {text}")
    return {"goal": _compact(goal, 800), "candidate_response": _compact(candidate_response),
            "task_status": status, "nudges_used": max(0, int(nudges_used or 0)),
            "max_nudges": max(0, int(max_nudges or 0))}


def skill_worthiness_input(user_request: Any, tool_sequence: Any, final_response: Any,
                           existing_skills: Any) -> dict[str, Any]:
    return {"user_request": _compact(user_request, 800),
            "tool_sequence": [_compact(step, 60) for step in list(tool_sequence or [])[:40]],
            "final_response": _compact(final_response, 800),
            "existing_skills": [_compact(skill, 260) for skill in list(existing_skills or [])[:30]]}


class BuiltinDecisionRunner:
    """Shared safe invocation pattern for built-in agent decision profiles."""

    def __init__(self, *, runtime_factory: Callable[[], DecisionRuntime] | None = None,
                 db_provider: Callable[[], Any] | None = None,
                 timeout_ms: int = TIMEOUT_MS) -> None:
        self._runtime_factory = runtime_factory
        self._db_provider = db_provider
        self.timeout_ms = timeout_ms

    def _db(self) -> Any:
        if self._db_provider:
            return self._db_provider()
        from models.db import db
        return db

    def _runtime(self) -> DecisionRuntime:
        return self._runtime_factory() if self._runtime_factory else DecisionRuntime(self._db())

    def decide(self, profile_key: str, input_context: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return normalized fields or ``None`` and leave the caller's fallback intact."""
        spec = BUILTIN_DECISION_SPECS[profile_key]
        started = time.monotonic()
        result_class, reason = "fallback", "unknown"
        try:
            profile = self._db().get_decision_profile(profile_key)
            # A disabled/missing/shadow profile is observational only.  In all
            # cases normal application behaviour remains the caller's fallback.
            if not profile:
                reason = "profile_missing"
                return None
            if not profile.get("enabled") or profile.get("mode") == "off":
                reason = "profile_disabled"
                return None
            if profile.get("mode") != "enforce":
                reason = "profile_shadow"
                return None
            result = self._runtime().invoke(RuntimeRequest(
                profile_key=profile_key, profile_version=PROFILE_VERSION,
                input_context={key: input_context[key] for key in spec.input_fields if key in input_context},
                cancellation=DecisionCancellation(deadline_unix_ms=int(time.time() * 1000) + self.timeout_ms),
                channel="agent_builtin", rate_limit_key=profile_key,
            ))
            if result.error:
                reason = result.error.code.value
                return None
            values = {field.key: field.value for field in result.fields}
            expected = {field["key"] for field in spec.fields}
            if set(values) != expected:
                reason = "malformed_result"
                return None
            result_class, reason = "profile", "accepted"
            return values
        except DecisionRuntimeError as exc:
            reason = exc.code.value
            return None
        except Exception:
            _LOG.warning("builtin_decision.failed profile=%s", profile_key, exc_info=True)
            reason = "exception"
            return None
        finally:
            elapsed = (time.monotonic() - started) * 1000
            _LOG.info("builtin_decision profile=%s result=%s fallback_reason=%s latency_ms=%.1f",
                      profile_key, result_class, reason, elapsed)


def get_builtin_decision_runner() -> BuiltinDecisionRunner:
    return BuiltinDecisionRunner()
