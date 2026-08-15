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


def _run_loop(state, responses, tool_names, real_exec=None):
    """Drive run_tool_loop with a fake LLM; returns (result, client)."""
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
    registry.get_real_executor.return_value = (
        real_exec or (lambda name, args: {"result": "ok"}))
    client = MagicMock()
    client.chat_completion.side_effect = responses

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
            tools=[{"type": "function", "function": {"name": name}}
                   for name in tool_names],
            session_id="task-lifecycle-test-session",
            llm_lock=threading.Lock(),
            stop_event=threading.Event(),
            session_skill_mds={},
            session_skill_tools={},
            llm_log_path=None,
        )
    return result, client


def _reminder_count(client):
    """Count bookkeeping-reminder messages across every LLM request made."""
    seen = set()
    for call in client.chat_completion.call_args_list:
        for i, msg in enumerate(call.kwargs.get("messages", [])):
            if llm_loop._TASK_BOOKKEEPING_REMINDER in str(msg.get("content", "")):
                seen.add(i)
    return len(seen)


def test_successful_mutation_without_explicit_task_update_is_auto_completed():
    """Forgotten bookkeeping triggers one reminder round-trip; when the agent
    still does not reconcile, the conservative auto-complete closes the task."""
    state = AgentState(mode="execute")
    state.update_tasks("set", tasks=["Apply the implementation change"])
    result, client = _run_loop(
        state,
        [
            _tool_response("write_file", "mutation-1"),
            _final_response("Implemented the requested change."),
            _final_response("Implemented the requested change."),
        ],
        ["update_tasks", "write_file"],
    )

    assert result == "Implemented the requested change."
    # Draft answer intercepted once, reminder injected exactly once, no loop.
    assert client.chat_completion.call_count == 3
    assert _reminder_count(client) == 1
    # The agent ignored the reminder — fallback auto-complete still closes it.
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


def test_reminder_then_explicit_reconcile_suppresses_auto_complete():
    """When the agent reconciles its task list in response to the reminder,
    its explicit statuses are authoritative — no silent auto-complete."""
    import time
    state = AgentState(mode="execute", tasks=[
        {"id": 1, "text": "First change", "status": "in_progress",
         "in_progress_since": time.time()},
        {"id": 2, "text": "Second change", "status": "pending"},
    ])

    def reconcile_exec(name, args):
        if name == "update_tasks":
            state.update_tasks("done", task_id=1)
            state.update_tasks("in_progress", task_id=2)
        return {"result": "ok"}

    result, client = _run_loop(
        state,
        [
            _tool_response("write_file", "mutation-1"),
            _final_response("Finished the first change."),
            _tool_response("update_tasks", "reconcile-1"),
            _final_response("Finished the first change."),
        ],
        ["update_tasks", "write_file"],
        real_exec=reconcile_exec,
    )

    assert result == "Finished the first change."
    assert client.chat_completion.call_count == 4
    assert _reminder_count(client) == 1
    assert state.tasks[0]["status"] == "done"
    # Task #2 was explicitly activated by the agent; the auto-complete
    # fallback must not silently close it with zero work done.
    assert state.tasks[1]["status"] == "in_progress"


def test_no_reminder_when_update_tasks_called_in_earlier_batch():
    """An update_tasks call anywhere in the turn counts as bookkeeping —
    the reminder must not fire on a later batch's final answer."""
    state = AgentState(mode="execute", tasks=[
        {"id": 1, "text": "First change", "status": "pending"},
        {"id": 2, "text": "Second change", "status": "pending"},
    ])
    result, client = _run_loop(
        state,
        [
            _tool_response("update_tasks", "update-1"),
            _tool_response("write_file", "mutation-1"),
            _final_response("Implemented the requested change."),
        ],
        ["update_tasks", "write_file"],
    )

    assert result == "Implemented the requested change."
    assert client.chat_completion.call_count == 3
    assert _reminder_count(client) == 0
    # Automatic transitions stay per-batch (b456105): the mutating batch
    # auto-activated #1 and turn-end auto-complete closed it.
    assert state.tasks[0]["status"] == "done"
    assert state.tasks[1]["status"] == "pending"


def test_no_reminder_for_pure_chat_turn():
    """A turn with no implementation work never triggers the reminder."""
    state = AgentState(mode="execute", tasks=[
        {"id": 1, "text": "Pending work", "status": "pending"},
    ])
    result, client = _run_loop(
        state,
        [_final_response("Here is the answer to your question.")],
        ["update_tasks", "write_file"],
    )

    assert result == "Here is the answer to your question."
    assert client.chat_completion.call_count == 1
    assert _reminder_count(client) == 0
    assert state.tasks[0]["status"] == "pending"


def test_no_reminder_for_read_only_turn():
    """Read-only tool usage is not implementation work — no reminder."""
    import time
    state = AgentState(mode="execute", tasks=[
        {"id": 1, "text": "Ongoing work", "status": "in_progress",
         "in_progress_since": time.time()},
    ])
    result, client = _run_loop(
        state,
        [
            _tool_response("read_file", "read-1"),
            _final_response("The file looks fine."),
        ],
        ["update_tasks", "read_file"],
    )

    assert result == "The file looks fine."
    assert client.chat_completion.call_count == 2
    assert _reminder_count(client) == 0
    assert state.tasks[0]["status"] == "in_progress"


def test_agent_without_update_tasks_tool_is_exempt():
    """Agents whose tool list lacks update_tasks (e.g. kanban suppression)
    must never be reminded to call a tool they do not have."""
    state = AgentState(mode="execute", tasks=[
        {"id": 1, "text": "Board-driven work", "status": "pending"},
    ])
    result, client = _run_loop(
        state,
        [
            _tool_response("write_file", "mutation-1"),
            _final_response("Implemented the requested change."),
        ],
        ["write_file"],
    )

    assert result == "Implemented the requested change."
    assert client.chat_completion.call_count == 2
    assert _reminder_count(client) == 0
    # The conservative auto-complete fallback still applies unchanged.
    assert state.tasks[0]["status"] == "done"
