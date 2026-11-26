"""Deterministic, protocol-safe provider request budgeting.

This module never mutates the persisted transcript.  It plans a provider-bound
copy of messages, tools, and tool choice immediately before a request is sent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.agent_runtime.active_context import (
    _PRESERVE_VERBATIM_TOOLS, _collect_groups, _eligible, repair_tool_pairs, validate_tool_pairs,
)
from backend.llm_usage_events import estimate_context_tokens, estimate_tokens

_DEFAULT_CONTEXT_WINDOW = 128000
_PROTOCOL_OVERHEAD = 64
_MULTIMODAL_OVERHEAD = 256
# The local estimator can only under-count what a provider reports (resent
# reasoning, chat-template framing, a different tokenizer), so the correction
# never shrinks an estimate and is capped against a nonsense usage block.
_MAX_CALIBRATION = 2.0
_RESULT_STUB = "[Tool output dropped to fit the context window. Run the tool again if it is still needed.]"
_STUB_KEEP_RECENT_GROUPS = 2
# Stubbing cuts down to this share of the budget, so the same cut holds for
# several tool iterations and the provider keeps its prompt cache in between.
_STUB_LOW_WATER_PERCENT = 80

@dataclass(frozen=True)
class RequestBudgetPlan:
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]]
    tool_choice: Any
    capacity: int
    input_budget: int
    estimated_tokens: int
    output_reserve: int
    thinking_reserve: int
    protocol_reserve: int
    multimodal_reserve: int
    safety_margin: int
    stage: str
    fit: bool
    diagnostic: Optional[str]
    fingerprint: str
    # Uncalibrated local estimate of this payload; estimated_tokens is the
    # calibrated value compared against input_budget.
    local_tokens: int = 0
    # First call id of the newest tool group whose results were stubbed. Passed
    # back on the next request so the cut is repeated instead of recomputed.
    stub_through: Optional[str] = None

    def metrics(self) -> Dict[str, Any]:
        return {"capacity": self.capacity, "input_budget": self.input_budget,
                "estimated_tokens": self.estimated_tokens, "local_tokens": self.local_tokens,
                "output_reserve": self.output_reserve,
                "thinking_reserve": self.thinking_reserve, "protocol_reserve": self.protocol_reserve,
                "multimodal_reserve": self.multimodal_reserve, "safety_margin": self.safety_margin,
                "stage": self.stage, "fit": self.fit, "diagnostic": self.diagnostic,
                "fingerprint": self.fingerprint, "message_count": len(self.messages),
                "tool_count": len(self.tools or [])}

def _positive(value: Any, default: int = 0) -> int:
    # Mocks and provider objects can implement surprising __int__ methods; only
    # concrete numeric/string configuration values are capacity declarations.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try: return max(0, int(value))
    except (TypeError, ValueError): return default

def _calibration(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 1.0:
        return 1.0
    return min(float(value), _MAX_CALIBRATION)

def _has_multimodal(messages: Sequence[Dict[str, Any]]) -> bool:
    return any(isinstance(m.get("content"), list) for m in messages if isinstance(m, dict))

def _fingerprint(messages, tools, tool_choice, capacity, stage) -> str:
    # Lengths/types only: telemetry fingerprints must not encode user content.
    shape = {"capacity": capacity, "stage": stage, "messages": [
        {"role": m.get("role"), "content_type": type(m.get("content")).__name__,
         "content_len": len(m.get("content", "")) if isinstance(m.get("content"), str) else len(m.get("content") or []),
         "calls": len(m.get("tool_calls") or [])} for m in messages],
        "tools": [((t.get("function") or {}).get("name"), len(json.dumps(t, sort_keys=True, default=str))) for t in (tools or [])],
        "tool_choice": bool(tool_choice)}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:24]

def _compact_tools(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep callable names and parameter types while removing descriptive bulk."""
    result = copy.deepcopy(list(tools))
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in {"description", "examples", "default"}}
        if isinstance(value, list): return [strip(v) for v in value]
        return value
    return [strip(tool) for tool in result]

def _remove_old_completed_group(messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    groups = _collect_groups(messages)
    for group in groups:
        if _eligible(group):
            return messages[:group.start] + messages[group.end:]
    return None

def _group_id(group) -> Optional[str]:
    return (group.assistant.get("tool_calls") or [{}])[0].get("id")

def _stub_old_results(messages: List[Dict[str, Any]], excess: int,
                      through: Optional[str] = None) -> Optional[Tuple[List[Dict[str, Any]], str]]:
    """Replace the oldest completed tool results with a stub, whatever the tool.

    The calls stay, so the model still sees what it ran. Stubs oldest-first until
    ``excess`` local tokens are saved, and always through the group ``through``
    so a later request repeats an earlier cut exactly. Returns the new messages
    and the newest stubbed group's id, or None when nothing changed.
    """
    candidates = _collect_groups(messages)[:-_STUB_KEEP_RECENT_GROUPS]
    forced = next((n + 1 for n, group in enumerate(candidates) if _group_id(group) == through), 0) if through else 0
    stub_tokens = estimate_tokens(_RESULT_STUB)
    out, saved, mark = list(messages), 0, None
    for n, group in enumerate(candidates):
        if n >= forced and saved >= excess:
            break
        if any(name in _PRESERVE_VERBATIM_TOOLS for name in group.names):
            continue
        for i in range(group.start + 1, group.end):
            content = out[i].get("content")
            if not content or content == _RESULT_STUB or (isinstance(content, str) and len(content) <= len(_RESULT_STUB)):
                continue
            if n >= forced:
                saved += estimate_context_tokens([out[i]]) - stub_tokens
            out[i] = {**out[i], "content": _RESULT_STUB}
            mark = _group_id(group)
    return (out, mark) if mark else None

def _remove_old_history(messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Drop one oldest ordinary historical message, retaining all system and latest user."""
    latest_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for i, message in enumerate(messages):
        if i != latest_user and message.get("role") not in {"system", "tool"} and not message.get("tool_calls"):
            return messages[:i] + messages[i + 1:]
    return None

def plan_request(messages: Sequence[Dict[str, Any]], tools: Optional[Sequence[Dict[str, Any]]], *,
                 tool_choice: Any = None, context_window: Any = None, max_tokens: Any = None,
                 thinking_budget: Any = None, safety_margin: Any = 512,
                 enforce: bool = True, force_reduction: bool = False,
                 calibration: Any = None, stub_through: Optional[str] = None) -> RequestBudgetPlan:
    """Build a fit payload by monotonic reductions or return an irreducible plan.

    ``calibration`` is the provider-reported / locally-estimated prompt size of
    the previous request; ``stub_through`` is the previous plan's value.
    """
    copied_messages, copied_tools = copy.deepcopy(list(messages)), copy.deepcopy(list(tools or []))
    capacity = _positive(context_window, _DEFAULT_CONTEXT_WINDOW) or _DEFAULT_CONTEXT_WINDOW
    output = _positive(max_tokens, 4096) or 4096
    thinking = _positive(thinking_budget)
    protocol = _PROTOCOL_OVERHEAD + (32 if tool_choice else 0)
    multimodal = _MULTIMODAL_OVERHEAD if _has_multimodal(copied_messages) else 0
    safety = _positive(safety_margin, 512)
    budget = max(0, capacity - output - thinking - protocol - multimodal - safety)
    ratio = _calibration(calibration)
    stage = "initial"
    diagnostic = None

    def _protocol_reject(reject_stage: str) -> RequestBudgetPlan:
        """Report a structural protocol defect -- never a size verdict."""
        _estimated = estimate_context_tokens(copied_messages, copied_tools) + estimate_tokens(json.dumps(tool_choice, default=str))
        return RequestBudgetPlan(copied_messages, copied_tools or None, tool_choice, capacity, budget, _estimated, output, thinking, protocol, multimodal, safety, reject_stage, False, "invalid_tool_protocol", _fingerprint(copied_messages, copied_tools, tool_choice, capacity, reject_stage))

    try:
        validate_tool_pairs(copied_messages, allow_unresolved=False)
    except ValueError:
        # Protocol invalidity is a structural defect, not a token-budget
        # condition. Conflating the two made every caller surface a misleading
        # "exceeds the safe context budget" message while the request was using
        # a few percent of the window. Attempt one deterministic repair pass on
        # this provider-bound copy and re-validate; only report the distinct
        # diagnostic when the history cannot be repaired.
        if not repair_tool_pairs(copied_messages):
            return _protocol_reject("invalid_tool_protocol")
        try:
            validate_tool_pairs(copied_messages, allow_unresolved=False)
        except ValueError:
            return _protocol_reject("invalid_tool_protocol_after_repair")
        stage = "tool_protocol_repair"
    if not enforce:
        stub_through = None
    if stub_through:
        stubbed = _stub_old_results(copied_messages, 0, stub_through)
        if stubbed is not None:
            copied_messages, stage = stubbed[0], "tool_results"
    while True:
        local = estimate_context_tokens(copied_messages, copied_tools) + estimate_tokens(json.dumps(tool_choice, default=str))
        estimated = math.ceil(local * ratio)
        if not enforce or (estimated <= budget and not force_reduction):
            return RequestBudgetPlan(copied_messages, copied_tools or None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, True, None, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage), local_tokens=local, stub_through=stub_through)
        # Old tool output goes first: it is the bulk of a long tool loop and the
        # only reduction that reaches results of tools the stages below never touch.
        stubbed = _stub_old_results(
            copied_messages, local - int(budget * _STUB_LOW_WATER_PERCENT / 100 / ratio), stub_through)
        if stubbed is not None:
            (copied_messages, stub_through), stage = stubbed, "tool_results"
            continue
        reduced = _remove_old_completed_group(copied_messages)
        if reduced is not None:
            copied_messages, stage = reduced, "completed_tool_groups"
            continue
        reduced = _remove_old_history(copied_messages)
        if reduced is not None:
            copied_messages, stage = reduced, "historical_messages"
            continue
        if copied_tools and stage != "compact_tool_schemas":
            copied_tools, stage = _compact_tools(copied_tools), "compact_tool_schemas"
            continue
        if copied_tools:
            copied_tools, tool_choice, stage = [], None, "disable_optional_tools"
            continue
        if force_reduction and estimated <= budget:
            return RequestBudgetPlan(copied_messages, copied_tools or None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, True, None, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage), local_tokens=local, stub_through=stub_through)
        diagnostic, stage = "irreducible_payload", "irreducible"
        return RequestBudgetPlan(copied_messages, None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, False, diagnostic, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage))
