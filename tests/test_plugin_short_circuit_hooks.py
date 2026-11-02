from backend import plugin_hooks


def test_turn_gate_short_circuits_on_first_handled_result():
    first = lambda context: None
    second = lambda context: {"handled": True, "response": "fixed"}
    third = lambda context: {"handled": True, "response": "wrong"}
    plugin_hooks.register_turn_gate(first)
    plugin_hooks.register_turn_gate(second)
    plugin_hooks.register_turn_gate(third)
    try:
        assert plugin_hooks.run_turn_gates({"agent_id": "a"}) == {"handled": True, "response": "fixed"}
    finally:
        for gate in (first, second, third):
            plugin_hooks.unregister_turn_gate(gate)


def test_tool_result_gate_ignores_non_terminating_decisions():
    first = lambda context, tool, args, result: {"terminate_turn": False}
    second = lambda context, tool, args, result: {"terminate_turn": True, "response": "fixed"}
    plugin_hooks.register_tool_result_gate(first)
    plugin_hooks.register_tool_result_gate(second)
    try:
        assert plugin_hooks.run_tool_result_gates({}, "tool", {}, {})["response"] == "fixed"
    finally:
        plugin_hooks.unregister_tool_result_gate(first)
        plugin_hooks.unregister_tool_result_gate(second)


def test_tool_guard_context_is_backward_compatible():
    old = lambda agent_id, tool, args: None
    captured = {}
    def new(agent_id, tool, args, context):
        captured.update(context)
        return {"block": True, "error": "locked"}
    plugin_hooks.register_tool_guard(old)
    plugin_hooks.register_tool_guard(new)
    try:
        decision = plugin_hooks.check_tool_guards("a", "tool", {}, {"session_id": "s"})
        assert decision["block"] is True and captured["session_id"] == "s"
    finally:
        plugin_hooks.unregister_tool_guard(old)
        plugin_hooks.unregister_tool_guard(new)


def test_required_tool_guard_fails_closed_and_an_ordinary_one_still_fails_open():
    def broken(agent_id, tool, args, context):
        raise RuntimeError("guard crashed")

    plugin_hooks.register_tool_guard(broken)
    try:
        assert plugin_hooks.check_tool_guards("a", "bash", {}, {}) is None      # unchanged: an ordinary guard fails open
    finally:
        plugin_hooks.unregister_tool_guard(broken)

    plugin_hooks.register_tool_guard(broken, required=True)
    try:
        decision = plugin_hooks.check_tool_guards("a", "bash", {}, {})
        assert decision["block"] is True and "required safety guard" in decision["error"]
    finally:
        plugin_hooks.unregister_tool_guard(broken)
    assert broken not in plugin_hooks._required_tool_guards
    assert plugin_hooks.check_tool_guards("a", "bash", {}, {}) is None


def test_a_required_guard_that_works_decides_as_before():
    allow = lambda agent_id, tool, args, context: None
    deny = lambda agent_id, tool, args, context: {"block": True, "error": "no writes now"}
    plugin_hooks.register_tool_guard(allow, required=True)
    try:
        assert plugin_hooks.check_tool_guards("a", "read_file", {}, {}) is None
        plugin_hooks.register_tool_guard(deny, required=True)
        assert plugin_hooks.check_tool_guards("a", "bash", {}, {})["error"] == "no writes now"
    finally:
        plugin_hooks.unregister_tool_guard(allow)
        plugin_hooks.unregister_tool_guard(deny)
