"""Regression tests for mid-turn tool pruning in the agent tool loop.

Background
----------
``backend/agent_runtime/llm_loop.py`` prunes tools that have never been called
once an iteration threshold is crossed, to save tokens. The keep-decision was
originally a nested closure, and platform built-in tools (``remember`` /
``recall`` / ``recall_sessions`` / ``forget_memory``, CMP path navigation, the
state-machine gate, ``reset_active_model``, ...) were not exempted. They could
therefore be silently dropped from the schema sent to the LLM whenever they went
uncalled past the threshold -- even though the agent's SYSTEM.md mandates their
use. The fix exempts every platform built-in tool and lifts the decision into a
unit-testable module-level helper.
"""

from backend.agent_runtime.llm_loop import _tool_survives_pruning


# A representative slice of the platform built-ins (see
# backend/tools/registry.py, ``builtin:`` namespace).
BUILTIN_FNS = {
    "remember",
    "recall",
    "recall_sessions",
    "forget_memory",
    "reset_active_model",
    "switch_path",
    "new_path",
    "read_transcript",
    "clear_log_file",
}


def _survives(name, **overrides):
    """Call the helper with empty context unless overridden."""
    kwargs = {
        "essential": set(),
        "builtin_fns": set(),
        "assigned_fns": set(),
        "loaded_skill_fns": set(),
        "eager_skill_fns": set(),
        "call_counts": {},
    }
    kwargs.update(overrides)
    return _tool_survives_pruning(name, **kwargs)


def test_recall_and_remember_survive_with_zero_calls():
    """The regression: prompt-mandated built-ins must not be pruned.

    This is exactly the case that previously dropped ``recall``/``remember``
    from the schema after iteration 3 in an uncalled turn.
    """
    for name in ("recall", "remember"):
        assert _survives(name, builtin_fns=BUILTIN_FNS) is True


def test_all_representative_builtins_survive_with_zero_calls():
    for name in BUILTIN_FNS:
        assert _survives(name, builtin_fns=BUILTIN_FNS) is True


def test_uncalled_non_builtin_tool_is_pruned():
    """The optimization still applies to the (large) assigned/plugin corpus."""
    assert _survives("some_plugin_tool", builtin_fns=BUILTIN_FNS) is False


def test_called_tool_survives_even_if_not_exempt():
    assert _survives("some_plugin_tool", call_counts={"some_plugin_tool": 1}) is True


def test_essential_tool_survives_with_zero_calls():
    assert _survives("bash", essential={"bash"}) is True


def test_assigned_tool_survives():
    assert _survives("describe_image", assigned_fns={"describe_image"}) is True


def test_loaded_skill_tool_survives():
    assert _survives("kanban_task", loaded_skill_fns={"kanban_task"}) is True


def test_eager_skill_tool_survives():
    assert _survives("Explore", eager_skill_fns={"Explore"}) is True


def test_registry_exposes_memory_builtins():
    """Guard the assumption behind the fix: these names really are built-ins.

    If a built-in is renamed/removed, the pruning exemption must follow it --
    this test fails loudly so the change is not silent.
    """
    from backend.tools import tool_registry

    names = {
        d.get("name")
        for d in tool_registry.get_builtin_tool_defs()
    }
    assert {"recall", "remember", "recall_sessions", "forget_memory"} <= names
