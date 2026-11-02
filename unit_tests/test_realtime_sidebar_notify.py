"""A finished turn only raises a sidebar bubble for turns a person is waiting for.

One-shot questions (``backend.agent_oneshot``) run in throwaway sessions nobody is looking at; they
must not mark the agent unread or pop up a bubble. Their own ``chat/done`` event is kept.
"""

import os
import tempfile

import backend.realtime_store as realtime_module
from backend.realtime_store import RealtimeStore, should_notify_sidebar

ONESHOT_UID = "__agent__oneshot__pringverify_e2b54cd921"
WEB_UID = "web-8f2c"


def make_store(tmpdir):
    return RealtimeStore(os.path.join(tmpdir, "realtime.db"))


def _turn_complete(**overrides):
    data = {
        "agent_id": "agent-a",
        "agent_name": "Agent A",
        "session_id": "session-a",
        "external_user_id": WEB_UID,
        "response": "the answer",
        "tool_trace": [],
        "is_error": False,
    }
    data.update(overrides)
    return data


def _publish(monkeypatch, data):
    with tempfile.TemporaryDirectory() as tmpdir:
        local = make_store(tmpdir)
        monkeypatch.setattr(realtime_module, "realtime_store", local)
        realtime_module.record_internal_event("turn_complete", data)
        status = local.events_after(0, {"status"}, agent_id="agent-a")
        # A session-scoped query also returns status events, so filter to the chat channel.
        chat = [e for e in local.events_after(0, {"chat"}, session_id="session-a", agent_id="agent-a")
                if e["channel"] == "chat"]
        local.close()
    return status, chat


def test_oneshot_turn_does_not_notify_sidebar(monkeypatch):
    status, chat = _publish(monkeypatch, _turn_complete(external_user_id=ONESHOT_UID))
    assert [e["event"] for e in status] == []
    assert [e["event"] for e in chat] == ["done"]


def test_normal_web_turn_notifies_sidebar(monkeypatch):
    status, chat = _publish(monkeypatch, _turn_complete())
    assert [e["event"] for e in status] == ["agent_turn_complete"]
    assert status[0]["data"]["external_user_id"] == WEB_UID
    assert [e["event"] for e in chat] == ["done"]


def test_silent_flag_suppresses_sidebar(monkeypatch):
    status, chat = _publish(monkeypatch, _turn_complete(silent=True))
    assert [e["event"] for e in status] == []
    assert [e["event"] for e in chat] == ["done"]


def test_should_notify_sidebar_helper():
    assert should_notify_sidebar(_turn_complete()) is True
    assert should_notify_sidebar({"external_user_id": ""}) is True
    assert should_notify_sidebar(_turn_complete(external_user_id=ONESHOT_UID)) is False
    assert should_notify_sidebar(_turn_complete(notify_sidebar=False)) is False
