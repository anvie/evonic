"""Integration coverage for automatic AgentState task lifecycle enforcement."""

import threading
from unittest.mock import MagicMock, patch

from backend.agent_runtime import llm_loop
from backend.agent_state import AgentState


def _tool_response(name, call_id):
    return {
        "success": True,
        "response": {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}]},
        "duration_ms": 1,
    }


def _final_response(content):
    return {
        "success": True,
        "response": {"choices": [{"message": {"content": content, "tool_calls": None},
                                     "finish_reason": "stop"}]},
        "duration_ms": 1,
    }


def test_successful_mutation_without_explicit_task_update_is_auto_completed():
    """A completed implementation turn advances the active AgentState task."""
    state = AgentState(mode="execute")
    state.update_tasks("set", tasks=["Apply the implementation change"])
    agent = {
        "id": "task-lifecycle-test-agent",
        "name": "Test",
        "model": None,
        "send_intermediate_responses": False,
        "summarize_threshold": 0,
    }
    context = {"user_id": "user", "channel_id": "channel", "is_super": False,
               "agent_state": state}
    database = MagicMock()
    database.get_setting.side_effect = lambda key, default=None: default or "0"
    database.get_agent_default_model.return_value = None
    database.get_agent_model.return_value = None
    database.get_agent_state.return_value = None
    database.get_agent_fallback_model.return_value = None
    database.get_summary.return_value = None
    registry = MagicMock()
    registry.get_builtin_executor.return_value = lambda name, args: None
    registry.get_real_executor.return_value = lambda name, args: {"result": "updated"}
    client = MagicMock()
    client.chat_completion.side_effect = [
        _tool_response("write_file", "mutation-1"),
        _final_response("Implemented the requested change."),
    ]

    from backend.event_stream import event_stream
    with patch.object(llm_loop, "db", database), \
         patch.object(llm_loop, "tool_registry", registry), \
         patch.object(llm_loop, "LLMClient", return_value=client), \
         patch.object(llm_loop, "llm_client", client), \
         patch.object(event_stream, "emit"):
        result, _, _ = llm_loop.run_tool_loop(
            agent=agent,
            agent_context=context,
            messages=[{"role": "system", "content": "system"},
                      {"role": "user", "content": "implement"}],
            tools=[{"type": "function", "function": {"name": "write_file"}}],
            session_id="task-lifecycle-test-session",
            llm_lock=threading.Lock(),
            stop_event=threading.Event(),
            session_skill_mds={},
            session_skill_tools={},
            llm_log_path=None,
        )

    assert result == "Implemented the requested change."
    assert state.tasks[0]["status"] == "done"


def test_legacy_stale_active_task_is_demoted_on_turn_start():
    """A pre-lifecycle active task (no in_progress_since) is demoted to pending
    when the session wakes, without being auto-completed."""
    state = AgentState(mode="execute", tasks=[
        {"id": 1, "text": "Old active task", "status": "in_progress"},
    ])
    agent = {
        "id": "task-lifecycle-test-agent",
        "name": "Test",
        "model": None,
        "send_intermediate_responses": False,
        "summarize_threshold": 0,
    }
    context = {"user_id": "user", "channel_id": "channel", "is_super": False,
               "agent_state": state}
    database = MagicMock()
    database.get_setting.side_effect = lambda key, default=None: default or "0"
    database.get_agent_default_model.return_value = None
    database.get_agent_model.return_value = None
    database.get_agent_state.return_value = None
    database.get_agent_fallback_model.return_value = None
    database.get_summary.return_value = None
    registry = MagicMock()
    registry.get_builtin_executor.return_value = lambda name, args: None
    registry.get_real_executor.return_value = lambda name, args: {"result": "ok"}
    client = MagicMock()
    client.chat_completion.side_effect = [
        _final_response("No stale work remains."),
    ]

    from backend.event_stream import event_stream
    emitted = []
    with patch.object(llm_loop, "db", database), \
         patch.object(llm_loop, "tool_registry", registry), \
         patch.object(llm_loop, "LLMClient", return_value=client), \
         patch.object(llm_loop, "llm_client", client), \
         patch.object(event_stream, "emit",
                      side_effect=lambda name, data: emitted.append((name, data))):
        result, _, _ = llm_loop.run_tool_loop(
            agent=agent,
            agent_context=context,
            messages=[{"role": "system", "content": "system"},
                      {"role": "user", "content": "continue"}],
            tools=[],
            session_id="task-lifecycle-test-session",
            llm_lock=threading.Lock(),
            stop_event=threading.Event(),
            session_skill_mds={},
            session_skill_tools={},
            llm_log_path=None,
        )

    assert result == "No stale work remains."
    # Demoted to pending, never auto-completed.
    assert state.tasks[0]["status"] == "pending"
    assert "in_progress_since" not in state.tasks[0]
    # The reconciliation surfaced to the UI as a lifecycle transition.
    transitions = [data for name, data in emitted if name == "tasks:auto_transition"]
    assert transitions and transitions[-1]["task_ids"] == [1]


def test_explicit_task_update_then_successful_mutation_completes_active_task():
    """An explicit update_tasks call must not disable later automatic
    completion of the active task after a successful implementation turn."""
    state = AgentState(mode="execute")
    state.tasks = [{"id": 1, "text": "Apply the implementation change",
                    "status": "in_progress", "in_progress_since": 1.0}]
    agent = {
        "id": "task-lifecycle-test-agent",
        "name": "Test",
        "model": None,
        "send_intermediate_responses": False,
        "summarize_threshold": 0,
    }
    context = {"user_id": "user", "channel_id": "channel", "is_super": False,
               "agent_state": state}
    database = MagicMock()
    database.get_setting.side_effect = lambda key, default=None: default or "0"
    database.get_agent_default_model.return_value = None
    database.get_agent_model.return_value = None
    database.get_agent_state.return_value = None
    database.get_agent_fallback_model.return_value = None
    database.get_summary.return_value = None
    registry = MagicMock()
    registry.get_builtin_executor.return_value = lambda name, args: None
    registry.get_real_executor.return_value = lambda name, args: {"result": "ok"}
    client = MagicMock()
    client.chat_completion.side_effect = [
        _tool_response("update_tasks", "update-1"),
        _tool_response("write_file", "mutation-1"),
        _final_response("Implemented the requested change."),
    ]

    from backend.event_stream import event_stream
    with patch.object(llm_loop, "db", database), \
         patch.object(llm_loop, "tool_registry", registry), \
         patch.object(llm_loop, "LLMClient", return_value=client), \
         patch.object(llm_loop, "llm_client", client), \
         patch.object(event_stream, "emit"):
        result, _, _ = llm_loop.run_tool_loop(
            agent=agent,
            agent_context=context,
            messages=[{"role": "system", "content": "system"},
                      {"role": "user", "content": "implement"}],
            tools=[{"type": "function",
                    "function": {"name": "update_tasks"}},
                   {"type": "function",
                    "function": {"name": "write_file"}}],
            session_id="task-lifecycle-test-session",
            llm_lock=threading.Lock(),
            stop_event=threading.Event(),
            session_skill_mds={},
            session_skill_tools={},
            llm_log_path=None,
        )

    assert result == "Implemented the requested change."
    assert state.tasks[0]["status"] == "done"
