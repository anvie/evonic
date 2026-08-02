"""Regression tests for behavior-preserving provider-payload compaction."""

import copy
import importlib.util
from pathlib import Path

from backend.agent_runtime import context
from backend.agent_runtime.active_context import project_active_context, validate_tool_pairs
from backend.agent_runtime.llm_loop import EffectiveRequest
from backend.llm_usage_events import estimate_context_tokens, estimate_tokens


def test_tool_schema_compaction_removes_only_semantically_empty_fields():
    tools = [{
        "type": "function",
        "function": {
            "name": "sample",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": ""},
                    "mode": {"type": "string", "enum": ["safe", "fast"]},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }]
    original = copy.deepcopy(tools)

    compacted = context.compact_tool_definitions(tools)

    assert compacted is tools
    assert compacted[0]["function"]["name"] == original[0]["function"]["name"]
    parameters = compacted[0]["function"]["parameters"]
    assert "description" not in compacted[0]["function"]
    assert "description" not in parameters["properties"]["query"]
    assert "required" not in parameters
    assert parameters["properties"]["mode"] == original[0]["function"]["parameters"]["properties"]["mode"]
    assert parameters["additionalProperties"] is False


def test_compact_receipts_preserve_attribution_and_protocol_without_raw_data():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "inspect"}]
    messages.extend([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "old-call",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"secret":"argument"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "private output" * 200},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "recent-call",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "recent-call", "content": "recent"},
    ])
    canonical = copy.deepcopy(messages)

    result = project_active_context(messages, mode="enforced", recent_completed_groups=1,
                                    receipt_max_chars=1000, soft_token_threshold=0)
    ledger = next(message["content"] for message in result.messages
                  if "Active Turn Ledger" in (message.get("content") or ""))

    assert messages == canonical
    assert "#1 read_file: success/informational; ref:" in ledger
    assert "argument" not in ledger
    assert "private output" not in ledger
    validate_tool_pairs(result.messages)


def test_retry_derivation_preserves_payload_protocol_and_recounts_messages():
    messages = [{"role": "user", "content": "inspect"}, {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"file_path":"sample.py"}'},
        }],
    }, {"role": "tool", "tool_call_id": "call-1", "content": "result"}]
    tools = [{"type": "function", "function": {
        "name": "read_file",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"},
        }, "required": ["file_path"]},
    }}]
    request = EffectiveRequest(
        messages=messages, tools=tools,
        canonical_message_tokens=estimate_context_tokens(messages, None),
        effective_message_tokens=estimate_context_tokens(messages, None),
        initial_tool_tokens=estimate_context_tokens([], tools),
        effective_tool_tokens=estimate_context_tokens([], tools),
        projection_mode="enforced", projection_applied=True, fail_open_reason=None,
        provider="primary", model="model-a",
    )

    retry = request.derive(provider="fallback", model="model-b", path="fallback")

    assert retry.messages is messages
    assert retry.tools is tools
    assert retry.effective_message_tokens == estimate_context_tokens(messages, None)
    assert (retry.provider, retry.model, retry.path) == ("fallback", "model-b", "fallback")
    validate_tool_pairs(retry.messages)


def test_artifact_instructions_keep_capabilities_with_lower_token_cost(monkeypatch):
    monkeypatch.setattr(context, "_build_portal_info", lambda _agent_id: [])
    context._system_prompt_cache.clear()
    agent = {
        "id": "token-test-agent",
        "system_prompt": "Base prompt",
        "inject_agent_id": False,
        "inject_datetime": False,
        "sandbox_enabled": True,
        "artifacts_enabled": True,
        "builtin_tools_enabled": False,
    }

    prompt = context.build_system_prompt(agent)
    artifact_section = prompt.split("## Artifacts Directory", 1)[1]

    assert "/workspace/shared/agents/token-test-agent/artifacts" in artifact_section
    assert "/_self/artifacts/" in artifact_section
    assert "save_artifact(content=" in artifact_section
    assert "save_artifact(source_path=" in artifact_section
    assert "/api/agents/token-test-agent/artifacts/<filename>" in artifact_section
    assert "<img src=" in artifact_section
    assert "`bash`/`runpy`" in artifact_section
    assert estimate_tokens(artifact_section) < 150


def test_offline_benchmark_is_deterministic_and_covers_required_shapes():
    benchmark_path = Path(__file__).parents[1] / "scripts" / "token_cost_benchmark.py"
    spec = importlib.util.spec_from_file_location("token_cost_benchmark_test", benchmark_path)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)

    first = benchmark.run("current")
    second = benchmark.run("current")
    names = {scenario["name"] for scenario in first["scenarios"]}

    assert first == second
    assert first["metadata"]["network_calls"] == 0
    assert first["metadata"]["payload_variant"] == "current"
    assert {
        "simple_turn", "multi_tool_loop", "long_tool_outputs", "loaded_skill",
        "retry_same_payload", "fallback_same_payload", "compacted_long_loop",
    } <= names
