"""Regression tests for durable bidirectional escalation routing."""

import sqlite3
from unittest import mock

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


def _create(store, escalation_id='esc-1', origin_session='human-session'):
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


def test_store_consumes_once_and_only_for_exact_session(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store)

    assert store.consume_pending_user_escalation('other-session') is None
    consumed = store.consume_pending_user_escalation('human-session')
    assert consumed['id'] == 'esc-1'
    assert consumed['status'] == 'answered'
    assert consumed['metadata']['reply_to_id'] == 'reply-1'
    assert store.consume_pending_user_escalation('human-session') is None


def test_new_escalation_supersedes_older_pending_request(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store, 'old')
    _create(store, 'new')

    consumed = store.consume_pending_user_escalation('human-session')
    assert consumed['id'] == 'new'
    with store._connect() as conn:
        old_status = conn.execute(
            "SELECT status FROM user_escalations WHERE id = 'old'"
        ).fetchone()[0]
    assert old_status == 'cancelled'


def test_route_reply_resumes_requesting_inter_agent_session(monkeypatch):
    escalation = {
        'id': 'esc-1', 'requesting_agent_id': 'agent-b',
        'requesting_session_id': 'delegated-session',
        'originating_agent_id': 'agent-a',
        'originating_session_id': 'human-session',
        'external_user_id': 'human-user', 'channel_id': 'channel-1',
        'metadata': {'agent_message_depth': 1, 'reply_to_id': 'reply-1'},
    }
    fake_db = mock.MagicMock()
    fake_db.consume_pending_user_escalation.return_value = escalation
    monkeypatch.setattr(escalation_routing, 'db', fake_db)

    with mock.patch(
        'backend.agent_runtime.notifier.notify_agent',
        return_value={'success': True, 'session_id': 'delegated-session'},
    ) as notify:
        result = escalation_routing.route_pending_escalation_reply(
            'agent-a', 'human-session', 'Use option two.',
        )

    assert result['success'] is True
    kwargs = notify.call_args.kwargs
    assert kwargs['agent_id'] == 'agent-b'
    assert kwargs['session_id'] == 'delegated-session'
    assert kwargs['trigger_llm'] is True
    assert kwargs['metadata']['escalation_reply'] is True
    assert kwargs['metadata']['session_id'] == 'human-session'


def test_route_reply_leaves_normal_messages_unchanged(monkeypatch):
    fake_db = mock.MagicMock()
    fake_db.consume_pending_user_escalation.return_value = None
    monkeypatch.setattr(escalation_routing, 'db', fake_db)

    with mock.patch('backend.agent_runtime.notifier.notify_agent') as notify:
        result = escalation_routing.route_pending_escalation_reply(
            'agent-a', 'human-session', 'Ordinary message',
        )

    assert result is None
    notify.assert_not_called()
