import copy

from backend.agent_runtime.active_context import validate_tool_pairs
from backend.agent_runtime.request_budget import plan_request


def _call(call_id, name="read_file"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _messages():
    return [
        {"role": "system", "content": "safety policy must remain"},
        {"role": "user", "content": "old request " * 300},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "x" * 1000},
        {"role": "tool", "tool_call_id": "b", "content": "x" * 1000},
        {"role": "user", "content": "latest intent must remain"},
    ]


def test_budget_reduces_completed_groups_without_mutating_or_orphaning():
    messages = _messages()
    original = copy.deepcopy(messages)
    plan = plan_request(messages, [], context_window=1000, max_tokens=200, safety_margin=100)
    assert plan.fit
    assert plan.estimated_tokens <= plan.input_budget
    assert plan.stage in {"completed_tool_groups", "historical_messages"}
    assert messages == original
    validate_tool_pairs(plan.messages)
    assert any(m.get("content") == "latest intent must remain" for m in plan.messages)
    assert any(m.get("role") == "system" for m in plan.messages)


def test_budget_accounts_for_tool_schema_and_can_disable_tools():
    tools = [{"type": "function", "function": {"name": "huge", "description": "z" * 6000,
             "parameters": {"type": "object", "description": "q" * 6000}}}]
    plan = plan_request([{"role": "system", "content": "safe"}, {"role": "user", "content": "latest"}],
                        tools, context_window=1000, max_tokens=100, safety_margin=100)
    assert plan.fit
    assert plan.stage in {"compact_tool_schemas", "disable_optional_tools"}
    assert plan.estimated_tokens <= plan.input_budget


def test_irreducible_system_payload_returns_diagnostic():
    plan = plan_request([{"role": "system", "content": "x" * 10000}], [],
                        context_window=1000, max_tokens=100, safety_margin=100)
    assert not plan.fit
    assert plan.diagnostic == "irreducible_payload"
    assert plan.fingerprint


def test_fingerprint_is_deterministic_and_capacity_specific():
    messages = [{"role": "system", "content": "safe"}, {"role": "user", "content": "hello 世界 ```json {\"a\": 1}```"}]
    first = plan_request(messages, [], context_window=3000, max_tokens=100)
    second = plan_request(messages, [], context_window=3000, max_tokens=100)
    smaller = plan_request(messages, [], context_window=1000, max_tokens=100)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != smaller.fingerprint


# ---------------------------------------------------------------------------
# Output reserve clamping (Kanban #929)
#
# A model row with ``max_tokens >= context_window`` used to collapse
# ``input_budget`` to 0 and reject every request before any provider call while
# the diagnostic claimed ``irreducible_payload``.  The payload was not the
# problem -- the reserve was.
# ---------------------------------------------------------------------------


def test_capacity_equal_to_max_tokens_still_fits_with_room_for_input():
    messages = [{"role": "system", "content": "safe"}, {"role": "user", "content": "latest intent"}]
    plan = plan_request(messages, [], context_window=64000, max_tokens=64000)
    assert plan.fit
    assert plan.input_budget > 0
    assert plan.output_reserve < plan.capacity
    assert plan.output_reserve_clamped
    assert plan.output_reserve_requested == 64000
    assert plan.estimated_tokens <= plan.input_budget


def test_output_reserve_is_clamped_when_it_alone_exceeds_capacity():
    messages = [{"role": "system", "content": "safe"}, {"role": "user", "content": "latest"}]
    plan = plan_request(messages, [], context_window=2000, max_tokens=1800, safety_margin=512)
    # 1800 + 64 + 512 > 2000, so the requested reserve alone makes the request
    # impossible. The plan must keep a positive input budget regardless.
    assert plan.input_budget > 0
    assert plan.output_reserve < 1800
    assert plan.output_reserve_clamped
    assert plan.fit


def test_normal_case_budget_is_unchanged():
    messages = [{"role": "system", "content": "safe"}, {"role": "user", "content": "hi"}]
    plan = plan_request(messages, [], context_window=128000, max_tokens=4096, safety_margin=512)
    # capacity - output - protocol - safety, exactly as before the clamp existed.
    assert plan.input_budget == 128000 - 4096 - 64 - 512
    assert plan.output_reserve == 4096
    assert not plan.output_reserve_clamped


def test_output_reserve_defect_gets_its_own_diagnostic():
    # Payload (about 50k tokens) fits next to a minimal answer reserve in a 64k
    # window: the blocker is the 64000-token output reserve, not the payload.
    plan = plan_request([{"role": "system", "content": "x" * 400000}], [],
                        context_window=64000, max_tokens=64000)
    assert not plan.fit
    assert plan.diagnostic == "output_reserve_exceeds_capacity"
    assert plan.input_budget > 0
    assert plan.output_reserve_clamped


def test_genuinely_huge_payload_keeps_irreducible_diagnostic():
    # Well beyond the whole window even with a minimal answer reserve.
    plan = plan_request([{"role": "system", "content": "x" * 600000}], [],
                        context_window=64000, max_tokens=64000)
    assert not plan.fit
    assert plan.diagnostic == "irreducible_payload"


def test_metrics_stay_json_serializable_and_backward_compatible():
    import json

    plan = plan_request([{"role": "system", "content": "x" * 400000}], [],
                        context_window=64000, max_tokens=64000)
    metrics = plan.metrics()
    # Pre-existing keys must survive (dashboards/events depend on them).
    for key in ("capacity", "input_budget", "estimated_tokens", "output_reserve",
                "thinking_reserve", "protocol_reserve", "multimodal_reserve",
                "safety_margin", "stage", "fit", "diagnostic", "fingerprint",
                "message_count", "tool_count"):
        assert key in metrics
    assert metrics["output_reserve_requested"] == 64000
    assert metrics["output_reserve_clamped"] is True
    assert json.loads(json.dumps(metrics))["diagnostic"] == "output_reserve_exceeds_capacity"


def test_clamped_plan_is_deterministic():
    messages = [{"role": "system", "content": "safe"}, {"role": "user", "content": "hi"}]
    first = plan_request(messages, [], context_window=64000, max_tokens=64000)
    second = plan_request(messages, [], context_window=64000, max_tokens=64000)
    assert first.fingerprint == second.fingerprint
    assert first.metrics() == second.metrics()
