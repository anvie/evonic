"""Regression guards for SSE-pushed agent state snapshots."""

from pathlib import Path

from backend.event_stream import CHAT_FORWARDED_EVENTS, EventStream


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_state_changed_is_chat_forwarded_after_tool_result():
    stream = EventStream()
    session_id = "state-sse-order"

    stream.emit("tool_executed", {"session_id": session_id, "tool_name": "set_mode"})
    stream.emit("state:changed", {"session_id": session_id, "mode": "execute"})

    events = stream.get_session_events(session_id)
    assert "state:changed" in CHAT_FORWARDED_EVENTS
    assert [event["event"] for event in events] == ["tool_executed", "state:changed"]
    assert [event["chat_seq"] for event in events] == [1, 2]


def test_live_and_gap_fill_transforms_forward_only_state_snapshot_fields():
    routes = read_repo_file("routes/agents.py")

    assert routes.count("'state:changed':") == 2
    assert routes.count("('state:changed',") == 2
    assert routes.count("('mode', 'plan_file', 'tasks', 'loaded_skills')") == 2


def test_unified_realtime_stream_forwards_state_changed():
    """The unified /api/realtime/stream is what the chat UI actually connects to.

    Without a 'state:changed' transform there, an explicit update_tasks() call
    consumes a chat seq that never reaches the browser, so the Session State
    panel only catches up on the next gap-fill.
    """
    realtime = read_repo_file("routes/realtime.py")

    assert "'state:changed': ('state:changed'" in realtime
    assert "('mode', 'plan_file', 'tasks', 'loaded_skills')" in realtime


def test_runtime_emits_fresh_state_snapshot_after_persist_and_tool_event():
    loop = read_repo_file("backend/agent_runtime/llm_loop.py")

    tool_emit = loop.index("event_stream.emit('tool_executed'")
    persist = loop.index("_persist_agent_state_split(_ms", tool_emit)
    state_emit = loop.index("event_stream.emit('state:changed'", persist)
    assert tool_emit < persist < state_emit
    assert "'mode': _ms.mode" in loop[state_emit:state_emit + 400]
    assert "'plan_file': _ms.plan_file" in loop[state_emit:state_emit + 400]
    assert "'tasks': list(_ms.tasks)" in loop[state_emit:state_emit + 400]


def test_frontend_consumes_snapshot_without_state_polling():
    transport = read_repo_file("static/js/chat-ui/transport.js")
    turn = read_repo_file("static/js/chat-ui/turn.js")
    chat_ui = read_repo_file("static/js/chat-ui/index.js")
    sessions = read_repo_file("templates/sessions.html")
    bundle = read_repo_file("static/js/chat-ui.js")

    # #745 inserted lifecycle events between state:changed and response_chunk in
    # the SSE list; the guard verifies state snapshots flow through the same
    # transport as response chunks.
    assert "'state:changed', 'tasks:auto_transition', 'tasks:stale', 'response_chunk'" in transport
    assert "this._onTrigger('state:changed', data);" in turn
    assert "new CustomEvent('evonic:state-changed', { detail: data })" in chat_ui
    assert "new CustomEvent('evonic:agent-state-changed', { detail: data })" in chat_ui
    assert "'state:changed', 'tasks:auto_transition', 'tasks:stale', 'response_chunk'" in bundle

    listener_start = sessions.index("document.addEventListener('evonic:agent-state-changed'")
    listener_end = sessions.index("function toggleMobileSummary()", listener_start)
    listener = sessions[listener_start:listener_end]
    assert "_sessionStateData[key] = detail[key]" in listener
    assert "scheduleSessionSummaryRefresh();" in listener
    assert "/chat/state" not in listener
    assert "setTimeout(() =>" in sessions
    assert "}, 1000);" in sessions


def test_legacy_state_event_keeps_http_fallback_for_atg_and_non_stream_callers():
    sessions = read_repo_file("templates/sessions.html")
    listener_start = sessions.index("document.addEventListener('evonic:agent-state-changed'")
    listener_end = sessions.index("function toggleMobileSummary()", listener_start)
    listener = sessions[listener_start:listener_end]

    assert "if (hasStateSnapshot)" in listener
    assert "else {" in listener
    assert "loadAgentState();" in listener


def test_whatsapp_restriction_warning_is_replayable_and_frontend_registered():
    stream = EventStream()
    session_id = "restricted-session"
    stream.emit("whatsapp_restriction_warning", {
        "session_id": session_id,
        "content": "WhatsApp reach-out restriction",
        "metadata": {
            "reachout_enforcement_type": "RESTRICT_ALL_COMPANIONS",
            "reachout_enforcement_ends": "2026-07-30T06:59:55Z",
        },
    })

    events = stream.get_session_events(session_id)
    assert "whatsapp_restriction_warning" in CHAT_FORWARDED_EVENTS
    assert events[0]["event"] == "whatsapp_restriction_warning"
    assert events[0]["chat_seq"] == 1

    realtime_route = read_repo_file("routes/realtime.py")
    realtime_client = read_repo_file("static/js/realtime.js")
    transport = read_repo_file("static/js/chat-ui/transport.js")
    bundle = read_repo_file("static/js/chat-ui.js")
    sessions = read_repo_file("templates/sessions.html")

    assert "'whatsapp_restriction_warning': ('whatsapp_restriction_warning'" in realtime_route
    assert realtime_client.count("'whatsapp_restriction_warning'") >= 2
    assert "'whatsapp_restriction_warning'" in transport
    assert "'whatsapp_restriction_warning'" in bundle
    assert sessions.count("function formatWhatsAppRestriction(") == 1
    assert "whatsapp_restriction_warning" in sessions
    assert "meta.whatsapp_restriction_key" in sessions
