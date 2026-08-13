"""Regression checks for durable chat replay."""

import os
import tempfile

from flask import Flask

import backend.realtime_store as realtime_module
from backend.realtime_store import RealtimeStore
from routes import realtime as realtime_route


def make_store(tmpdir):
    return RealtimeStore(os.path.join(tmpdir, "realtime.db"))


def make_app():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(realtime_route.realtime_bp)
    return app


def test_active_turn_replays_in_order_after_refresh():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        turn_id, created = store.queue_turn("agent-a", "session-a", "turn-a")
        assert created and turn_id == "turn-a"
        store.start_turn(turn_id)
        store.publish("chat", "turn_begin", {}, agent_id="agent-a",
                      session_id="session-a", turn_id=turn_id)
        store.publish("chat", "thinking", {"content": "working"},
                      agent_id="agent-a", session_id="session-a", turn_id=turn_id)

        events = store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
            active_only=True,
        )
        assert [event["event"] for event in events] == [
            "turn_queued", "agent_busy_changed", "turn_begin", "thinking",
        ]
        assert [event["id"] for event in events] == sorted(event["id"] for event in events)

        store.finish_turn(turn_id)
        assert store.active_turns(session_id="session-a") == []
        assert store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
            active_only=True,
        ) == []
        # Terminal telemetry remains replayable for the 24-hour retention window.
        assert len(store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        )) == 4
        store.close()


def test_debounced_queue_reuses_one_durable_turn():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        first, created = store.queue_turn("agent-a", "session-a", "turn-a")
        second, created_again = store.queue_turn("agent-a", "session-a", "turn-b")
        assert created is True
        assert created_again is False
        assert second == first
        assert len(store.active_turns(session_id="session-a")) == 1
        assert [event["event"] for event in store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        )] == ["turn_queued", "agent_busy_changed"]
        store.close()


def test_restart_marks_old_turn_interrupted():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        turn_id, _ = store.queue_turn("agent-a", "session-a", "turn-a")
        store.start_turn(turn_id)
        with store._connect() as conn:
            conn.execute(
                "UPDATE active_turns SET owner_pid = ? WHERE turn_id = ?",
                (os.getpid() + 100_000, turn_id),
            )

        assert [turn["turn_id"] for turn in store.interrupt_stale_turns()] == [turn_id]
        assert store.active_turns() == []
        events = store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        )
        assert events[-2]["event"] == "done"
        assert events[-2]["data"]["interrupted"] is True
        assert events[-1]["event"] == "agent_busy_changed"
        store.close()


def test_chat_replay_is_exactly_session_scoped():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        store.publish("chat", "thinking", {"content": "A"},
                      agent_id="agent-a", session_id="session-a")
        store.publish("chat", "thinking", {"content": "B"},
                      agent_id="agent-a", session_id="session-b")

        events = store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        )
        assert len(events) == 1
        assert events[0]["data"]["content"] == "A"
        store.close()


def test_expired_terminal_event_is_not_replayed():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        store.publish(
            "chat", "done", {"response": "old"},
            agent_id="agent-a", session_id="session-a",
            occurred_at_ms=1,
        )
        assert store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        ) == []
        store.close()


def test_message_event_keeps_correlation_without_local_paths(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        monkeypatch.setattr(realtime_module, "realtime_store", store)
        realtime_module.record_internal_event("message_received", {
            "agent_id": "agent-a", "session_id": "session-a",
            "message": "[Attached: report.pdf path=/private/report.pdf] inspect",
            "message_id": 42, "client_message_id": "browser-1", "role": "user",
            "metadata": {"attachment_info": {
                "attachment_id": 7, "filename": "report.pdf",
                "mime_type": "application/pdf", "file_path": "/private/report.pdf",
            }},
        })
        data = store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        )[0]["data"]
        assert data["message_id"] == 42
        assert data["client_message_id"] == "browser-1"
        assert "/private/report.pdf" not in str(data)
        assert data["metadata"]["attachment_info"]["filename"] == "report.pdf"
        store.close()


def test_gateway_replays_from_last_event_id(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        first = store.publish("status", "agent_busy_changed", {"busy": True})
        second = store.publish("status", "agent_busy_changed", {"busy": False})
        monkeypatch.setattr(realtime_route, "realtime_store", store)

        response = make_app().test_client().get(
            "/api/realtime/stream?channels=status",
            headers={"Last-Event-ID": str(first)}, buffered=False,
        )
        chunks = [next(response.response).decode() for _ in range(3)]
        response.close()

        assert f"id: {first}" in chunks[1] and "event: ready" in chunks[1]
        assert f"id: {second}" in chunks[2]
        assert '"busy":false' in chunks[2]
        store.close()


def test_fresh_global_gateway_starts_at_current_state(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        old = store.publish("update", "update_done", {"status": "old"})
        monkeypatch.setattr(realtime_route, "realtime_store", store)

        response = make_app().test_client().get(
            "/api/realtime/stream?channels=update", buffered=False,
        )
        chunks = []
        while not any("event: ready" in chunk for chunk in chunks):
            chunks.append(next(response.response).decode())
        assert all('"status":"old"' not in chunk for chunk in chunks)
        assert any(f"id: {old}" in chunk and "event: ready" in chunk for chunk in chunks)

        new = store.publish("update", "update_done", {"status": "new"})
        live = next(response.response).decode()
        response.close()
        assert f"id: {new}" in live and '"status":"new"' in live
        store.close()
