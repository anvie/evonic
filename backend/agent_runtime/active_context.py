"""Protocol-safe projection of active-turn messages for bounded LLM context.

The projector is intentionally pure: callers retain the complete canonical
transcript while this module builds a deep-copied, model-facing alternative.
Shadow mode measures the projection; enforced mode sends the validated projection.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.llm_usage_events import estimate_context_tokens, estimate_tokens

_VALID_MODES = frozenset({"off", "shadow", "enforced"})

# Retention policy is deliberately separate from execution parallelism. Unknown
# plugin tools are never compacted until they receive an explicit classification.
_INFORMATIONAL_TOOLS = frozenset({
    "read_file", "read", "calculator", "find", "stats", "tree", "outline",
    "list_functions", "ps", "ports", "disk_usage", "env", "uname", "which",
    "recall", "recall_sessions", "list_artifacts", "kanban_search_tasks",
    "kanban_get_task", "kanban_get_comments", "list_sessions",
})
_MUTATION_RECEIPT_TOOLS = frozenset({
    "write_file", "patch", "str_replace", "remember", "forget_memory",
    "save_artifact", "send_file", "send_notification", "send_agent_message",
    "send_channel_message", "escalate_to_user", "resolve_agent_approval",
    "kanban_add_comment", "kanban_create_task", "kanban_update_status",
    "kanban_update_task", "kanban_delete_task", "clear_log_file",
})
_CONTEXT_CONTROL_TOOLS = frozenset({
    "use_skill", "unload_skill", "save_plan", "set_mode", "update_tasks",
    "state", "compile_task_graph", "switch_path", "new_path", "reset_active_model",
})
# Tools whose results must ALWAYS be retained verbatim in the LLM context.
# use_skill/unload_skill results inform the LLM whether a skill is loaded/unloaded;
# compacting them into receipts causes confusion (regression edaa229 + 2e09fc9).
_PRESERVE_VERBATIM_TOOLS = frozenset({"use_skill", "unload_skill"})
_ELIGIBLE_TOOLS = _INFORMATIONAL_TOOLS | _MUTATION_RECEIPT_TOOLS | _CONTEXT_CONTROL_TOOLS


@dataclass(frozen=True)
class ActiveContextProjection:
    """Projected messages and deterministic attribution for one request."""

    messages: List[Dict[str, Any]]
    mode: str
    applied: bool
    failed_open: bool
    error: Optional[str]
    canonical_tokens: int
    projected_tokens: int
    receipt_tokens: int
    completed_groups: int
    compacted_groups: int
    retained_groups: int

    @property
    def saved_tokens(self) -> int:
        return max(0, self.canonical_tokens - self.projected_tokens)

    def metrics(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "applied": self.applied,
            "failed_open": self.failed_open,
            "canonical_tokens": self.canonical_tokens,
            "projected_tokens": self.projected_tokens,
            "saved_tokens": self.saved_tokens,
            "receipt_tokens": self.receipt_tokens,
            "completed_groups": self.completed_groups,
            "compacted_groups": self.compacted_groups,
            "retained_groups": self.retained_groups,
        }


@dataclass(frozen=True)
class _ToolGroup:
    start: int
    end: int
    assistant: Dict[str, Any]
    results: Tuple[Dict[str, Any], ...]
    names: Tuple[str, ...]


def normalize_mode(mode: Any) -> str:
    """Return a supported mode, defaulting invalid input to safe ``off``."""
    value = str(mode or "off").strip().lower()
    return value if value in _VALID_MODES else "off"


def validate_tool_pairs(
    messages: Sequence[Dict[str, Any]], *, allow_unresolved: bool = False,
) -> None:
    """Validate assistant tool-call/result protocol structure.

    Projection permits an unresolved batch because it must remain verbatim until
    execution completes. Completed batches remain strict: results must be
    contiguous, unique, and in declared order. Provider-bound validation can keep
    the default and require every call to have a result.
    """
    i = 0
    while i < len(messages):
        message = messages[i]
        if message.get("role") == "tool":
            raise ValueError("orphaned tool result")
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            i += 1
            continue
        expected = [call.get("id") for call in calls if call.get("id")]
        if len(expected) != len(calls) or len(set(expected)) != len(expected):
            raise ValueError("tool calls must have unique non-empty ids")
        actual: List[str] = []
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            actual.append(messages[j].get("tool_call_id"))
            j += 1
        if len(set(actual)) != len(actual) or actual != expected[:len(actual)]:
            raise ValueError("tool results must immediately match declared call order")
        if len(actual) != len(expected) and not allow_unresolved:
            raise ValueError("tool calls are unresolved")
        i = j


def _collect_groups(messages: Sequence[Dict[str, Any]]) -> List[_ToolGroup]:
    groups: List[_ToolGroup] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            i += 1
            continue
        expected = [call.get("id") for call in calls if call.get("id")]
        results: List[Dict[str, Any]] = []
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            results.append(messages[j])
            j += 1
        actual = [result.get("tool_call_id") for result in results]
        if len(expected) == len(calls) and expected == actual:
            names = tuple(str((call.get("function") or {}).get("name") or "unknown") for call in calls)
            groups.append(_ToolGroup(i, j, message, tuple(results), names))
        i = max(j, i + 1)
    return groups


def _result_has_error(result: Dict[str, Any]) -> bool:
    content = result.get("content")
    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        lowered = content.lower()
        return "error:" in lowered or '"error"' in lowered
    return isinstance(parsed, dict) and (
        bool(parsed.get("error")) or parsed.get("status") == "error"
        or parsed.get("level") in {"rejected", "requires_approval"}
    )


def _eligible(group: _ToolGroup) -> bool:
    return (
        bool(group.names)
        and all(name in _ELIGIBLE_TOOLS for name in group.names)
        and not any(
            _result_has_error(result) for result in group.results
        )
        and not any(name in _PRESERVE_VERBATIM_TOOLS for name in group.names)
    )


def _receipt_line(sequence: int, group: _ToolGroup) -> str:
    """Build a sanitized receipt containing no arguments or raw tool output."""
    labels = ", ".join(group.names)
    kind = "informational"
    if any(name in _CONTEXT_CONTROL_TOOLS for name in group.names):
        kind = "context-control"
    elif any(name in _MUTATION_RECEIPT_TOOLS for name in group.names):
        kind = "mutation"
    digest_input = "|".join(
        str(result.get("tool_call_id") or "") + ":" + str(result.get("content") or "")
        for result in group.results
    )
    digest = hashlib.sha256(digest_input.encode("utf-8", errors="replace")).hexdigest()[:12]
    # Keep the same model-visible facts in a denser receipt. These lines recur once
    # per compacted group, so even small reductions materially bound long loops.
    return f"- #{sequence} {labels}: success/{kind}; ref:{digest}"


def _bounded_ledger(entries: Sequence[str], max_chars: int) -> str:
    header = "## Active Turn Ledger\nOlder completed tool groups were compacted into sanitized receipts."
    if max_chars <= len(header):
        return header[:max_chars]
    kept: List[str] = []
    used = len(header)
    for entry in entries:
        needed = len(entry) + 1
        if used + needed > max_chars:
            break
        kept.append(entry)
        used += needed
    omitted = len(entries) - len(kept)
    if omitted:
        marker = f"- … {omitted} additional completed group(s) omitted"
        while kept and used + len(marker) + 1 > max_chars:
            removed = kept.pop()
            used -= len(removed) + 1
            omitted += 1
            marker = f"- … {omitted} additional completed group(s) omitted"
        if used + len(marker) + 1 <= max_chars:
            kept.append(marker)
    return header + (("\n" + "\n".join(kept)) if kept else "")


def project_active_context(
    canonical_messages: Sequence[Dict[str, Any]],
    tools: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    mode: str = "shadow",
    recent_completed_groups: int = 2,
    receipt_max_chars: int = 4000,
    soft_token_threshold: int = 12000,
) -> ActiveContextProjection:
    """Build a deterministic bounded projection, failing open on every error.

    Only complete, successful, explicitly classified groups older than the recent
    frontier are eligible. The canonical sequence is never mutated.
    """
    normalized_mode = normalize_mode(mode)
    try:
        canonical_copy = copy.deepcopy(list(canonical_messages))
    except Exception as exc:
        # Even malformed/custom message containers must not break the live request.
        canonical_copy = list(canonical_messages)
        return ActiveContextProjection(
            messages=canonical_copy,
            mode=normalized_mode,
            applied=False,
            failed_open=True,
            error=f"{type(exc).__name__}: {exc}",
            canonical_tokens=0,
            projected_tokens=0,
            receipt_tokens=0,
            completed_groups=0,
            compacted_groups=0,
            retained_groups=0,
        )
    try:
        canonical_tokens = estimate_context_tokens(canonical_copy, list(tools or []))
    except Exception:
        canonical_tokens = 0
    base = dict(
        mode=normalized_mode,
        canonical_tokens=canonical_tokens,
        receipt_tokens=0,
        completed_groups=0,
        compacted_groups=0,
        retained_groups=0,
    )
    if normalized_mode == "off" or canonical_tokens < max(0, int(soft_token_threshold)):
        return ActiveContextProjection(
            messages=canonical_copy, applied=False, failed_open=False, error=None,
            projected_tokens=canonical_tokens, **base,
        )

    try:
        # A live loop can contain a trailing unresolved/partially resolved batch.
        # It is intentionally excluded by _collect_groups and retained verbatim.
        validate_tool_pairs(canonical_copy, allow_unresolved=True)
        groups = _collect_groups(canonical_copy)
        recent_count = max(0, int(recent_completed_groups))
        frontier_groups = groups[-recent_count:] if recent_count else []
        frontier = {group.start for group in frontier_groups}
        compactable = [group for group in groups if group.start not in frontier and _eligible(group)]
        compact_starts = {group.start for group in compactable}
        if not compactable:
            return ActiveContextProjection(
                messages=canonical_copy, applied=False, failed_open=False, error=None,
                projected_tokens=canonical_tokens, completed_groups=len(groups),
                retained_groups=len(groups), **{k: v for k, v in base.items()
                                               if k not in {"completed_groups", "retained_groups"}},
            )

        entries = [_receipt_line(groups.index(group) + 1, group) for group in compactable]
        ledger = _bounded_ledger(entries, max(128, int(receipt_max_chars)))
        by_start = {group.start: group for group in groups}
        projected: List[Dict[str, Any]] = []
        ledger_inserted = False
        i = 0
        while i < len(canonical_copy):
            group = by_start.get(i)
            if group and i in compact_starts:
                if not ledger_inserted:
                    projected.append({"role": "system", "content": ledger})
                    ledger_inserted = True
                i = group.end
                continue
            projected.append(copy.deepcopy(canonical_copy[i]))
            i += 1

        validate_tool_pairs(projected, allow_unresolved=True)
        projected_tokens = estimate_context_tokens(projected, list(tools or []))
        return ActiveContextProjection(
            messages=projected,
            mode=normalized_mode,
            applied=True,
            failed_open=False,
            error=None,
            canonical_tokens=canonical_tokens,
            projected_tokens=projected_tokens,
            receipt_tokens=estimate_tokens(ledger),
            completed_groups=len(groups),
            compacted_groups=len(compactable),
            retained_groups=len(groups) - len(compactable),
        )
    except Exception as exc:
        return ActiveContextProjection(
            messages=canonical_copy,
            mode=normalized_mode,
            applied=False,
            failed_open=True,
            error=f"{type(exc).__name__}: {exc}",
            canonical_tokens=canonical_tokens,
            projected_tokens=canonical_tokens,
            receipt_tokens=0,
            completed_groups=0,
            compacted_groups=0,
            retained_groups=0,
        )
