"""Regression tests for the runtime escalation ingress guard.

Agent-originated messages (marked with agent_message/agent_reply metadata)
must never be treated as a human escalation reply and must not consume a
pending escalation correlation. This guards against the auto-forwarded final
answer of a delegating agent echoing back as a fake "human reply".
"""

import sqlite3
from contextlib import contextmanager
from unittest import mock

from backend.agent_runtime import runtime as ar_runtime
from backend.agent_runtime.runtime import AgentRuntime
from backend import escalation_routing
from models.mixins.escalations import EscalationMixin


class _EscalationStore(EscalationMixin):
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE user_escalations (
                    id TEXT PRIMARY KEY, requesting_agent_id TEXT NOT NULL,
                    requesting_session_id TEXT NOT NULL,
                    originating_agent_id TEXT NOT NULL,
                    originating_session_id TEXT NOT NULL,
                    delivery_session_id TEXT NOT NULL,
                    external_user_id TEXT NOT NULL, channel_id TEXT,
                    status TEXT NOT NULL, metadata TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, expires_at REAL NOT NULL,
                    answered_at REAL
                )
            """)

    def _connect(self):
        return sqlite3.connect(self.path)


def _create_pending(store, escalation_id='esc-1', origin_session='human-session'):
    store.create_user_escalation(
        escalation_id=escalation_id,
        requesting_agent_id='agent-b',
        requesting_session_id='delegated-session',
        originating_agent_id='agent-a',
        originating_session_id=origin_session,
        delivery_session_id=origin_session,
        external_user_id='human-user',
        channel_id='channel-1',
        metadata={'agent_message_depth': 1, 'reply_to_id': 'reply-1'},
    )
    assert store.mark_user_escalation_delivered(escalation_id)


@contextmanager
def _patch_runtime(monkeypatch, route_result=None):
    """Patch runtime dependencies so handle_message reaches the escalation guard."""
    monkeypatch.setattr(
        ar_runtime.db, 'get_agent',
        lambda agent_id: {'id': agent_id, 'name': 'Agent A', 'enabled': True, 'message_buffer_seconds': 0},
    )
    monkeypatch.setattr(
        ar_runtime.db, 'is_user_blocked_by_external_id',
        lambda external_user_id: False,
    )
    monkeypatch.setattr(
        ar_runtime.db, 'get_or_create_session',
        lambda agent_id, external_user_id, channel_id=None, db_agent_id=None: 'human-session',
    )
    monkeypatch.setattr(ar_runtime.db, 'add_chat_message', lambda *a, **k: 1)

    fake_cl = mock.MagicMock()
    fake_cl.append = mock.Mock()
    monkeypatch.setattr(ar_runtime.chatlog_manager, 'get', lambda agent_id, session_id: fake_cl)

    monkeypatch.setattr(ar_runtime.event_stream, 'emit', mock.Mock())

    runtime = AgentRuntime()
    runtime._llm_serializer._concurrency_mgr.is_agent_at_capacity = mock.Mock(return_value=False)

    def fake_put_task(task, already_queued=False):
        task.result = {'response': 'mocked', 'tool_trace': [], 'timeline': [], 'escalation_routed': False}
        task.event.set()

    runtime._put_task = fake_put_task

    with mock.patch.object(escalation_routing, 'route_pending_escalation_reply') as mock_route:
        if route_result is not None:
            mock_route.return_value = route_result
        yield mock_route, runtime


def test_agent_reply_does_not_consume_pending_escalation(monkeypatch, tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create_pending(store)

    with _patch_runtime(monkeypatch) as (mock_route, runtime):
        result = runtime.handle_message(
            'agent-a', 'human-user', 'restart please',
            metadata={'agent_reply': True},
        )

    mock_route.assert_not_called()
    # Escalation should still be pending (not consumed by handle_message)
    assert store.consume_pending_user_escalation('human-session') is not None


def test_agent_message_does_not_consume_pending_escalation(monkeypatch, tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create_pending(store)

    with _patch_runtime(monkeypatch) as (mock_route, runtime):
        result = runtime.handle_message(
            'agent-a', 'human-user', 'restart please',
            metadata={'agent_message': True},
        )

    mock_route.assert_not_called()
    # Escalation should still be pending (not consumed by handle_message)
    assert store.consume_pending_user_escalation('human-session') is not None


def test_plain_human_message_routes_and_consumes_once(monkeypatch, tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create_pending(store)

    with _patch_runtime(monkeypatch, route_result={'success': True}) as (mock_route, runtime):
        result = runtime.handle_message(
            'agent-a', 'human-user', 'restart please',
        )

    assert mock_route.called
    assert result['escalation_routed'] is True
    consumed = store.consume_pending_user_escalation('human-session')
    assert consumed is not None
    assert store.consume_pending_user_escalation('human-session') is None


def test_escalation_reply_metadata_does_not_route(monkeypatch, tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create_pending(store)

    with _patch_runtime(monkeypatch) as (mock_route, runtime):
        result = runtime.handle_message(
            'agent-a', 'human-user', 'restart please',
            metadata={'escalation_reply': True},
        )

    mock_route.assert_not_called()
    # Escalation should still be pending (not consumed by handle_message)
    assert store.consume_pending_user_escalation('human-session') is not None


def test_skip_escalation_consume_marker_does_not_route(monkeypatch, tmp_path):
    """The auto-forward's explicit marker is honoured as agent traffic.

    `skip_escalation_consume` is set by the delegation auto-forward path so the
    guard stays self-describing even if the generic agent_message/agent_reply
    keys are ever renamed.
    """
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create_pending(store)

    with _patch_runtime(monkeypatch) as (mock_route, runtime):
        runtime.handle_message(
            'agent-a', 'human-user', 'auto-forwarded answer',
            metadata={'skip_escalation_consume': True},
        )

    mock_route.assert_not_called()
    assert store.consume_pending_user_escalation('human-session') is not None
