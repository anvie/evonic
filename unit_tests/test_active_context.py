"""Tests for protocol-safe same-turn active-context projection."""

import copy
import json

from backend.agent_runtime.active_context import (
    normalize_mode,
    project_active_context,
    validate_tool_pairs,
)


def _group(call_ids, names, payload, *, reasoning=None):
    calls = [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps({"secret": f"arg-{call_id}"})},
        }
        for call_id, name in zip(call_ids, names)
    ]
    assistant = {"role": "assistant", "content": "", "tool_calls": calls}
    if reasoning:
        assistant["reasoning_content"] = reasoning
    return [assistant] + [
        {"role": "tool", "tool_call_id": call_id, "content": payload}
        for call_id in call_ids
    ]


def _base_messages():
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect the project"},
    ]


def test_projection_is_deterministic_and_does_not_mutate_canonical_messages():
    messages = _base_messages()
    messages += _group(["old"], ["read_file"], "x" * 2000)
    messages += _group(["new"], ["read_file"], "y" * 2000, reasoning="provider reasoning")
    original = copy.deepcopy(messages)

    first = project_active_context(messages, mode="shadow", recent_completed_groups=1,
                                   soft_token_threshold=0)
    second = project_active_context(messages, mode="shadow", recent_completed_groups=1,
                                    soft_token_threshold=0)

    assert messages == original
    assert first.messages == second.messages
    assert first.applied and first.compacted_groups == 1
    assert first.projected_tokens < first.canonical_tokens
    validate_tool_pairs(first.messages)
    retained = next(message for message in first.messages if message.get("tool_calls"))
    assert retained["reasoning_content"] == "provider reasoning"


def test_parallel_tool_group_is_compacted_atomically_without_sensitive_data():
    messages = _base_messages()
    messages += _group(["a", "b"], ["read_file", "calculator"], "sensitive-output" * 100)
    messages += _group(["frontier"], ["read_file"], "recent")

    result = project_active_context(messages, recent_completed_groups=1,
                                    soft_token_threshold=0, receipt_max_chars=1000)

    ledger = next(message["content"] for message in result.messages
                  if "Active Turn Ledger" in (message.get("content") or ""))
    assert "read_file, calculator" in ledger
    assert "arg-a" not in ledger
    assert "sensitive-output" not in ledger
    assert not any(message.get("tool_call_id") in {"a", "b"} for message in result.messages)
    validate_tool_pairs(result.messages)


def test_unknown_tool_and_error_groups_are_retained_conservatively():
    messages = _base_messages()
    messages += _group(["unknown"], ["plugin_without_policy"], "large" * 500)
    messages += _group(["error"], ["read_file"], json.dumps({"error": "permission denied"}))
    messages += _group(["frontier"], ["read_file"], "recent")

    result = project_active_context(messages, recent_completed_groups=1,
                                    soft_token_threshold=0)

    assert not result.applied
    assert result.compacted_groups == 0
    assert result.messages == messages


def test_unresolved_calls_are_retained_while_older_groups_compact():
    messages = _base_messages()
    messages += _group(["completed"], ["read_file"], "large result" * 200)
    unresolved = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "provider state",
        "tool_calls": [{
            "id": "pending",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }
    messages.append(unresolved)

    result = project_active_context(messages, recent_completed_groups=0,
                                    soft_token_threshold=0)

    assert result.applied
    assert result.messages[-1] == unresolved
    assert result.messages[-1] is not unresolved
    validate_tool_pairs(result.messages, allow_unresolved=True)


def test_invalid_protocol_fails_open_to_an_unchanged_copy():
    messages = _base_messages() + _group(["declared"], ["read_file"], "ok")
    messages[-1]["tool_call_id"] = "different"

    result = project_active_context(messages, recent_completed_groups=0,
                                    soft_token_threshold=0)

    assert result.failed_open
    assert not result.applied
    assert result.messages == messages
    assert result.messages is not messages


def test_bounded_receipts_and_mode_validation():
    messages = _base_messages()
    for index in range(12):
        messages += _group([f"old-{index}"], ["read_file"], "payload" * 100)
    messages += _group(["frontier"], ["read_file"], "recent")

    result = project_active_context(messages, recent_completed_groups=1,
                                    soft_token_threshold=0, receipt_max_chars=160)
    ledger = next(message["content"] for message in result.messages
                  if "Active Turn Ledger" in (message.get("content") or ""))

    assert len(ledger) <= 160
    assert "omitted" in ledger
    assert normalize_mode("ENFORCED") == "enforced"
    assert normalize_mode("invalid") == "off"


def test_synthetic_long_loop_materially_reduces_projected_growth():
    messages = _base_messages()
    for index in range(50):
        messages += _group([f"call-{index}"], ["read_file"], "line of source code\n" * 300)

    result = project_active_context(messages, recent_completed_groups=2,
                                    soft_token_threshold=0, receipt_max_chars=2000)

    assert result.compacted_groups == 48
    assert result.projected_tokens < result.canonical_tokens * 0.25
    validate_tool_pairs(result.messages)
