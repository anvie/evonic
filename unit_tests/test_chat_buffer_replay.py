"""Regression checks for durable chat replay."""

import os
import tempfile
import threading
import time
from types import SimpleNamespace

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
            "/api/realtime/stream?channels=status&cursor_version=2&snapshot=0",
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


def test_wait_for_events_wakes_immediately_after_publish():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        finished = threading.Event()
        elapsed = []

        def wait():
            started = time.monotonic()
            store.wait_for_events(0, timeout=2)
            elapsed.append(time.monotonic() - started)
            finished.set()

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.05)
        store.publish("status", "agent_busy_changed", {"busy": True})
        assert finished.wait(0.5)
        thread.join()
        assert elapsed[0] < 0.5
        store.close()


def test_cancelled_queue_cannot_be_started_again():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        turn_id, _ = store.queue_turn("agent-a", "session-a", "turn-a")

        cancelled = store.cancel_queued_turns("session-a", turn_id)

        assert [turn["turn_id"] for turn in cancelled] == [turn_id]
        assert store.active_turns(session_id="session-a") == []
        assert store.start_turn(turn_id) is None
        store.close()


def test_runtime_stop_cancels_queued_worker_and_next_request_is_clean(monkeypatch):
    from backend.agent_runtime.runtime import AgentRuntime
    from backend.tools.lib.process_tracker import process_tracker

    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        monkeypatch.setattr(realtime_module, "realtime_store", store)
        monkeypatch.setattr(process_tracker, "kill", lambda _session_id: None)
        monkeypatch.setattr(process_tracker, "clear_stop", lambda _session_id: None)

        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._buffer_lock = threading.Lock()
        runtime._buffer_timers = {}
        raw_stop_event = threading.Event()

        class GuardedStopEvent:
            def set(self):
                assert runtime._buffer_lock.locked()
                raw_stop_event.set()

            def clear(self):
                assert runtime._buffer_lock.locked()
                raw_stop_event.clear()

            def is_set(self):
                return raw_stop_event.is_set()

        stop_event = GuardedStopEvent()
        runtime._get_stop_event = lambda _session_id: stop_event
        cancelled = []
        runtime._emit_cancelled_turn = lambda turn, reason: cancelled.append((turn, reason))

        turn_id, _ = store.queue_turn("agent-a", "session-a", "turn-a")
        runtime.request_stop("session-a")
        result = runtime._do_process(
            {"id": "agent-a"},
            SimpleNamespace(turn_id=turn_id, session_id="session-a"),
        )

        assert stop_event.is_set()
        assert result["stopped"] is True
        assert cancelled[0][1] == "stopped"
        assert store.active_turns() == []

        task = SimpleNamespace(
            agent={"id": "agent-a"},
            ctx=SimpleNamespace(turn_id="turn-b", session_id="session-a"),
        )
        runtime._mark_task_queued(task)
        assert not stop_event.is_set()
        assert task.ctx.turn_id == "turn-b"
        store.close()


def test_restart_recovery_handles_same_pid_once_per_process():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        first, _ = store.queue_turn("agent-a", "session-a", "turn-a")
        store.start_turn(first)

        assert [turn["turn_id"] for turn in store.interrupt_stale_turns()] == [first]
        assert store.active_turns() == []

        second, _ = store.queue_turn("agent-a", "session-b", "turn-b")
        store.start_turn(second)
        assert store.interrupt_stale_turns() == []
        assert [turn["turn_id"] for turn in store.active_turns()] == [second]
        store.close()


def test_session_purge_preserves_active_turn_projection():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        turn_id, _ = store.queue_turn("agent-a", "session-a", "turn-a")
        store.start_turn(turn_id)
        store.publish("chat", "thinking", {"content": "active"},
                      agent_id="agent-a", session_id="session-a", turn_id=turn_id)
        store.publish("chat", "message_received", {"content": "old"},
                      agent_id="agent-a", session_id="session-a")

        store.purge_session("session-a")

        assert [turn["turn_id"] for turn in store.active_turns()] == [turn_id]
        events = store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        )
        assert any(event["data"].get("content") == "active" for event in events)
        assert not any(event["data"].get("content") == "old" for event in events)

        store.finish_turn(turn_id)
        store.purge_session("session-a")
        assert store.events_after(
            0, {"chat"}, session_id="session-a", agent_id="agent-a",
        ) == []
        store.close()


def test_initial_replay_starts_after_session_clear(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        turn_id, _ = store.queue_turn("agent-a", "session-a", "turn-a")
        store.start_turn(turn_id)
        store.publish("chat", "thinking", {"content": "before clear"},
                      agent_id="agent-a", session_id="session-a", turn_id=turn_id)
        store.publish("chat", "session_clear", {},
                      agent_id="agent-a", session_id="session-a")
        cursor = store.publish("chat", "thinking", {"content": "after clear"},
                               agent_id="agent-a", session_id="session-a",
                               turn_id=turn_id)
        monkeypatch.setattr(realtime_route, "realtime_store", store)
        monkeypatch.setattr(realtime_route, "_build_snapshot", lambda *args, **kwargs: [])

        response = make_app().test_client().get(
            "/api/realtime/stream?chat=1&agent_id=agent-a&session_id=session-a"
            f"&cursor_version=2&snapshot=1&after={cursor}",
            buffered=False,
        )
        chunks = []
        while not any("event: ready" in chunk for chunk in chunks):
            chunks.append(next(response.response).decode())
        response.close()
        output = "".join(chunks)

        assert "after clear" in output
        assert "before clear" not in output
        store.close()


def test_gateway_resets_invalid_and_future_v2_cursors(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        high_water = store.publish("status", "agent_busy_changed", {"busy": True})
        monkeypatch.setattr(realtime_route, "realtime_store", store)
        monkeypatch.setattr(realtime_route, "_build_snapshot", lambda *args, **kwargs: [])
        client = make_app().test_client()

        for cursor in ("status:9", str(2**63), str(2**63 - 1)):
            response = client.get(
                "/api/realtime/stream?channels=status&cursor_version=2"
                f"&snapshot=0&after={cursor}",
                buffered=False,
            )
            chunks = [next(response.response).decode() for _ in range(2)]
            response.close()
            assert f"id: {high_water}" in chunks[1]
            assert "event: ready" in chunks[1]
        store.close()


def test_initial_chat_replay_orders_and_deduplicates_pending_approval(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        turn_id, _ = store.queue_turn("agent-a", "session-a", "turn-a")
        store.start_turn(turn_id)
        store.publish("chat", "turn_begin", {}, agent_id="agent-a",
                      session_id="session-a", turn_id=turn_id)
        cursor = store.publish(
            "approvals", "approval_required", {"approval_id": "approval-a"},
            agent_id="agent-a", session_id="session-a", turn_id=turn_id,
        )
        monkeypatch.setattr(realtime_route, "realtime_store", store)

        def snapshot(_channels, _session_id, _agent_id, _workplace_id, skipped):
            if "approval-a" in skipped:
                return []
            return [("approvals", "approval_required", {"approval_id": "approval-a"})]

        monkeypatch.setattr(realtime_route, "_build_snapshot", snapshot)
        response = make_app().test_client().get(
            "/api/realtime/stream?chat=1&agent_id=agent-a&session_id=session-a"
            f"&cursor_version=2&snapshot=1&after={cursor}",
            buffered=False,
        )
        chunks = []
        while not any("event: ready" in chunk for chunk in chunks):
            chunks.append(next(response.response).decode())
        response.close()
        output = "".join(chunks)

        assert output.count("event: approval_required") == 1
        assert output.index("event: turn_begin") < output.index("event: approval_required")
        store.close()


def test_legacy_update_stream_keeps_old_event_names(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        monkeypatch.setattr(realtime_route, "realtime_store", store)
        monkeypatch.setattr(
            realtime_route, "_build_snapshot",
            lambda *args, **kwargs: [("update", "update_status", {"status": "idle"})],
        )
        response = make_app().test_client().get(
            "/api/realtime/stream?channels=update&legacy=update&snapshot=1",
            buffered=False,
        )
        chunks = [next(response.response).decode() for _ in range(3)]
        assert "event: status" in chunks[1]
        assert "event: update_status" not in chunks[1]

        event_id = store.publish("update", "update_done", {"status": "success"})
        live = next(response.response).decode()
        response.close()
        assert f"id: {event_id}" in live
        assert "event: done" in live
        store.close()


def test_json_client_message_id_rejects_non_strings(monkeypatch):
    from routes import agents as agents_route
    from routes import sessions as sessions_route

    monkeypatch.setattr(agents_route.db, "get_agent", lambda _agent_id: {"id": "agent-a"})
    app = make_app()
    for invalid in (123, ["id"], {"id": "value"}):
        with app.test_request_context(
                "/api/agents/agent-a/chat", method="POST",
                json={"message": "hello", "client_message_id": invalid}):
            _response, status = agents_route.api_chat("agent-a")
            assert status == 400
        with app.test_request_context(
                "/api/sessions/session-a/reply", method="POST",
                json={"text": "hello", "client_message_id": invalid}):
            _response, status = sessions_route.api_session_reply("session-a")
            assert status == 400


def test_legacy_routes_keep_safe_compatibility_contract(monkeypatch):
    from routes import agents as agents_route
    from routes import update as update_route
    from routes import workplaces as workplaces_route

    app = make_app()
    with app.test_request_context("/api/system/update/stream"):
        response = update_route.api_update_stream()
        assert "legacy=update" in response.location

    monkeypatch.setattr(workplaces_route.db, "get_workplace", lambda _workplace_id: None)
    with app.test_request_context("/api/workplaces/missing/events"):
        _response, status = workplaces_route.api_workplace_events("missing")
        assert status == 404

    monkeypatch.setattr(workplaces_route.db, "get_workplace", lambda workplace_id: {"id": workplace_id})
    with app.test_request_context("/api/workplaces/wp/events"):
        response = workplaces_route.api_workplace_events("wp/a?b")
        assert "workplace=wp%2Fa%3Fb" in response.location

    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_store(tmpdir)
        monkeypatch.setattr(realtime_module, "realtime_store", store)
        with app.test_request_context(
                "/api/agents/agent-a/chat/events?session_id=session-a&after=99"):
            response = agents_route.api_chat_events("agent-a")
            assert response.get_json() == {
                "events": [], "reset": True, "cursor": 0,
            }
        store.close()
