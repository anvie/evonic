"""Regression guards for durable SSE state events."""

import os
import tempfile
from pathlib import Path

import backend.realtime_store as realtime_module
from backend.event_stream import EventStream
from backend.realtime_store import RealtimeStore


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def journal_events(*events):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = RealtimeStore(os.path.join(tmpdir, "realtime.db"))
        previous = realtime_module.realtime_store
        stream = EventStream()
        stream._write_log = lambda _line: None
        realtime_module.realtime_store = store
        try:
            for event_name, payload in events:
                stream.emit(event_name, payload)
            return store.events_after(
                0, {"chat"}, session_id="state-session", agent_id="agent-a",
            )
        finally:
            realtime_module.realtime_store = previous
            stream._executor.shutdown(wait=True)
            store.close()


def test_state_changed_is_durably_ordered_after_tool_result():
    events = journal_events(
        ("tool_executed", {
            "agent_id": "agent-a", "session_id": "state-session",
            "tool_name": "set_mode", "tool_result": {"ok": True},
        }),
        ("state:changed", {
            "agent_id": "agent-a", "session_id": "state-session",
            "mode": "execute", "plan_file": "plan.md", "ignored": "private",
        }),
    )
    assert [event["event"] for event in events] == ["tool_executed", "state:changed"]
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert events[1]["data"]["mode"] == "execute"
    assert events[1]["data"]["plan_file"] == "plan.md"
    assert "ignored" not in events[1]["data"]


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

    for source in (transport, bundle):
        assert "'state:changed'" in source
        assert "'whatsapp_restriction_warning'" in source
    assert "this._onTrigger('state:changed', data);" in turn
    assert "new CustomEvent('evonic:state-changed', { detail: data })" in chat_ui
    assert "new CustomEvent('evonic:agent-state-changed', { detail: data })" in chat_ui

    listener_start = sessions.index("document.addEventListener('evonic:agent-state-changed'")
    listener_end = sessions.index("function toggleMobileSummary()", listener_start)
    listener = sessions[listener_start:listener_end]
    assert "_sessionStateData[key] = detail[key]" in listener
    assert "scheduleSessionSummaryRefresh();" in listener
    assert "/chat/state" not in listener


def test_whatsapp_warning_is_durable_and_registered():
    events = journal_events(("whatsapp_restriction_warning", {
        "agent_id": "agent-a", "session_id": "state-session",
        "content": "WhatsApp reach-out restriction",
        "metadata": {
            "reachout_enforcement_type": "RESTRICT_ALL_COMPANIONS",
            "reachout_enforcement_ends": "2026-07-30T06:59:55Z",
        },
    }))
    assert len(events) == 1
    assert events[0]["event"] == "whatsapp_restriction_warning"
    assert events[0]["data"]["metadata"]["reachout_enforcement_type"] == \
        "RESTRICT_ALL_COMPANIONS"

    realtime_client = read_repo_file("static/js/realtime.js")
    sessions = read_repo_file("templates/sessions.html")
    assert realtime_client.count("'whatsapp_restriction_warning'") >= 2
    assert sessions.count("function formatWhatsAppRestriction(") == 1
    assert "meta.whatsapp_restriction_key" in sessions
