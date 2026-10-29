"""Regression tests for durable bidirectional escalation routing."""

import sqlite3
import time
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


# -------------------------------------------------------
# has_pending_user_escalation — read-only suppression probe
# -------------------------------------------------------

def test_has_pending_escalation_true_for_delivered_request(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store)

    assert store.has_pending_user_escalation('human-session') is True
    assert store.has_pending_user_escalation('human-session', 'agent-a') is True


def test_has_pending_escalation_ignores_other_session_and_agent(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store)

    assert store.has_pending_user_escalation('other-session') is False
    assert store.has_pending_user_escalation('human-session', 'agent-z') is False


def test_has_pending_escalation_false_before_delivery(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store)
    with store._connect() as conn:
        conn.execute(
            "UPDATE user_escalations SET status = 'delivering' WHERE id = 'esc-1'"
        )

    assert store.has_pending_user_escalation('human-session') is False


def test_has_pending_escalation_false_after_cancel_or_answer(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store)
    store.cancel_user_escalation('esc-1')
    assert store.has_pending_user_escalation('human-session') is False

    _create(store, 'esc-2')
    assert store.has_pending_user_escalation('human-session') is True
    assert store.consume_pending_user_escalation('human-session')['id'] == 'esc-2'
    assert store.has_pending_user_escalation('human-session') is False


def test_has_pending_escalation_honours_expiry(tmp_path):
    store = _EscalationStore(tmp_path / 'escalations.db')
    _create(store)
    with store._connect() as conn:
        conn.execute(
            "UPDATE user_escalations SET expires_at = ? WHERE id = 'esc-1'",
            (time.time() - 1,),
        )

    assert store.has_pending_user_escalation('human-session') is False


# -------------------------------------------------------
# End-to-end: escalation delivery vs. delegation auto-forward
# -------------------------------------------------------

def test_delegation_auto_forward_does_not_consume_pending_escalation(tmp_path):
    """The delegating agent's own final answer must not swallow the escalation.

    Agent B escalates to the human through agent A's session; A then finishes
    its turn. A's auto-forwarded answer must be suppressed (the escalation is
    still pending) and the human's real reply must later resume B exactly once.
    """
    from backend.tools import agent_messaging

    store = _EscalationStore(tmp_path / 'escalations.db')
    agent_ctx = {
        'id': 'agent-b', '_db_agent_id': 'agent-b', 'name': 'Agent B',
        'user_id': '__agent__agent-a', 'session_id': 'delegated-session',
        'agent_message_depth': 1,
        'origin_session_id': 'human-session',
        'origin_report_to_id': 'human-user',
        'origin_report_to_channel_id': 'channel-1',
        'origin_reply_to_id': 'reply-1',
    }
    request_meta = {
        'from_agent_id': 'agent-a',
        'report_to_id': 'human-user',
        'report_to_channel_id': 'channel-1',
        'session_id': 'human-session',
        'agent_message_depth': 1,
        'reply_to_id': 'reply-1',
    }
    human_session = {
        'id': 'human-session', 'agent_id': 'agent-a',
        'external_user_id': 'human-user', 'channel_id': 'channel-1',
    }
    delivery_result = {
        'success': True, 'session_id': 'human-session',
        'delivery': 'external_channel',
    }

    with mock.patch.multiple(
        agent_messaging.db,
        create_user_escalation=store.create_user_escalation,
        mark_user_escalation_delivered=store.mark_user_escalation_delivered,
        cancel_user_escalation=store.cancel_user_escalation,
        has_pending_user_escalation=store.has_pending_user_escalation,
        get_latest_agent_request_metadata=mock.DEFAULT,
        get_session_with_details=mock.DEFAULT,
        get_agent=mock.DEFAULT,
    ) as patched, mock.patch(
        'backend.agent_runtime.notifier.notify_agent',
        return_value=delivery_result,
    ) as notify:
        patched['get_latest_agent_request_metadata'].return_value = request_meta
        patched['get_session_with_details'].return_value = human_session
        patched['get_agent'].return_value = {'id': 'agent-b', 'name': 'Agent B'}

        delivered = agent_messaging._exec_escalate_to_user(
            {'message': 'May I restart the service?'}, agent_ctx,
        )
        assert delivered['success'] is True
        assert store.has_pending_user_escalation('human-session') is True

        # A's turn ends right after the escalation was delivered.
        agent_messaging._on_final_answer({
            'external_user_id': '__agent__agent-a',
            'agent_id': 'agent-b',
            'session_id': 'delegated-session',
            'answer': 'I need the user to approve a restart.',
        })

        # Only the escalation delivery reached the human session: no echo.
        assert notify.call_count == 1
        assert store.has_pending_user_escalation('human-session') is True

    # The genuine human answer now consumes the correlation exactly once.
    with mock.patch.multiple(
        escalation_routing.db,
        consume_pending_user_escalation=store.consume_pending_user_escalation,
    ), mock.patch(
        'backend.agent_runtime.notifier.notify_agent',
        return_value={'success': True, 'session_id': 'delegated-session'},
    ) as notify_reply:
        reply = escalation_routing.route_pending_escalation_reply(
            'agent-a', 'human-session', 'Yes, restart it.',
        )

    assert reply['success'] is True
    kwargs = notify_reply.call_args.kwargs
    assert kwargs['agent_id'] == 'agent-b'
    assert kwargs['session_id'] == 'delegated-session'
    assert kwargs['message'] == 'Yes, restart it.'
    assert kwargs['metadata']['escalation_reply'] is True
    assert store.has_pending_user_escalation('human-session') is False
    assert store.consume_pending_user_escalation('human-session') is None
