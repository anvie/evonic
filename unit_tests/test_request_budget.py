import copy

from backend.agent_runtime.active_context import validate_tool_pairs
from backend.agent_runtime.request_budget import _RESULT_STUB, plan_request


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


def _bash_history(groups, name="bash"):
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "explore"}]
    for i in range(groups):
        messages.append({"role": "assistant", "content": "", "tool_calls": [_call(f"g{i}", name)]})
        messages.append({"role": "tool", "tool_call_id": f"g{i}", "content": f"output {i} " * 400})
    return messages


def _stubbed(plan):
    return [m["tool_call_id"] for m in plan.messages if m.get("content") == _RESULT_STUB]


def test_old_results_of_any_tool_are_stubbed_before_anything_is_removed():
    messages = _bash_history(8)
    original = copy.deepcopy(messages)
    plan = plan_request(messages, [], context_window=8000, max_tokens=500, safety_margin=100)
    assert plan.fit and plan.stage == "tool_results"
    assert plan.estimated_tokens <= plan.input_budget
    assert messages == original
    validate_tool_pairs(plan.messages)
    # every call survives, the newest two groups keep their output
    assert len(plan.messages) == len(messages)
    stubbed = _stubbed(plan)
    assert stubbed and stubbed == [f"g{i}" for i in range(len(stubbed))]
    assert plan.stub_through == stubbed[-1]
    assert plan.messages[-1] == messages[-1] and plan.messages[-3] == messages[-3]


def test_skill_results_are_never_stubbed():
    plan = plan_request(_bash_history(8, "use_skill"), [], context_window=8000, max_tokens=500,
                        safety_margin=100)
    assert not _stubbed(plan)


def test_provider_calibration_reduces_a_payload_the_local_estimate_accepts():
    messages = _bash_history(8)
    kwargs = dict(context_window=12000, max_tokens=500, safety_margin=100)
    local = plan_request(messages, [], **kwargs)
    assert local.stage == "initial" and local.estimated_tokens == local.local_tokens
    plan = plan_request(messages, [], calibration=1.5, **kwargs)
    assert plan.fit and plan.stage == "tool_results"
    assert plan.local_tokens < local.local_tokens
    assert plan.local_tokens * 1.5 <= plan.input_budget
    # a ratio below 1 (cached tokens left out of the usage block) never loosens the budget
    assert plan_request(messages, [], calibration=0.2, **kwargs).estimated_tokens == local.local_tokens


def test_stub_cut_is_repeated_so_the_prefix_stays_cacheable():
    kwargs = dict(context_window=8000, max_tokens=500, safety_margin=100)
    first = plan_request(_bash_history(8), [], **kwargs)
    # the next tool iteration appends one more group
    second = plan_request(_bash_history(9), [], stub_through=first.stub_through, **kwargs)
    assert second.fit and second.stub_through == first.stub_through
    assert _stubbed(second) == _stubbed(first)
    # only the group that left the verbatim tail changed, everything before the cut is identical
    cut = 2 + 2 * len(_stubbed(first))
    assert second.messages[:cut] == first.messages[:cut]
