"""Regression tests for age-scoped notification deduplication.

A stale-task reminder that was persisted but never processed (its turn was
reaped) must not suppress every later retry forever. ``dedup_max_age_seconds``
lets an identical message be re-delivered once it is old enough, while recent
in-flight duplicates keep being suppressed.
"""

import time
import types
from unittest import mock

import pytest

from backend.agent_runtime import notifier


def _message(content, age_seconds):
    created = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - age_seconds))
    return {'role': 'user', 'content': content, 'created_at': created}


def _patch_db(monkeypatch, messages):
    fake_db = mock.MagicMock()
    fake_db.get_session_messages.return_value = messages
    monkeypatch.setattr(notifier, 'db', fake_db)
    return fake_db


# ── _is_duplicate ──────────────────────────────────────────────────────────

def test_recent_identical_message_is_a_duplicate(monkeypatch):
    _patch_db(monkeypatch, [_message('Reminder', 10)])
    assert notifier._is_duplicate('s', 'Reminder', 5, 300) is True


def test_old_identical_message_is_no_longer_a_duplicate(monkeypatch):
    _patch_db(monkeypatch, [_message('Reminder', 600)])
    assert notifier._is_duplicate('s', 'Reminder', 5, 300) is False


def test_without_max_age_legacy_window_behavior_is_kept(monkeypatch):
    _patch_db(monkeypatch, [_message('Reminder', 86_400)])
    assert notifier._is_duplicate('s', 'Reminder', 5) is True


def test_unparseable_timestamp_is_treated_as_recent(monkeypatch):
    _patch_db(monkeypatch, [{'role': 'user', 'content': 'Reminder', 'created_at': ''}])
    assert notifier._is_duplicate('s', 'Reminder', 5, 300) is True


def test_newest_matching_message_decides_the_outcome(monkeypatch):
    _patch_db(monkeypatch, [_message('Reminder', 900), _message('Reminder', 5)])
    assert notifier._is_duplicate('s', 'Reminder', 5, 300) is True


def test_assistant_and_other_content_are_ignored(monkeypatch):
    _patch_db(monkeypatch, [
        {'role': 'assistant', 'content': 'Reminder', 'created_at': ''},
        _message('Different', 1),
    ])
    assert notifier._is_duplicate('s', 'Reminder', 5, 300) is False


def test_parse_failure_of_one_row_does_not_hide_a_recent_match(monkeypatch):
    _patch_db(monkeypatch, [
        {'role': 'user', 'content': 'Reminder', 'created_at': 'not-a-date'},
        _message('Reminder', 5),
    ])
    assert notifier._is_duplicate('s', 'Reminder', 5, 300) is True


# ── notify_agent end-to-end delivery ──────────────────────────────────────

@pytest.fixture
def routing_env(monkeypatch):
    fake_db = mock.MagicMock()
    fake_db.get_session_messages.return_value = []
    fake_db.get_web_fallback_session.return_value = None
    fake_db.get_channel.return_value = None
    fake_db.get_or_create_session.return_value = 'resolved-session'

    channel_manager = mock.MagicMock()
    channel_manager.is_running.return_value = False
    registry_stub = types.ModuleType('backend.channels.registry')
    registry_stub.channel_manager = channel_manager

    send_guard_stub = types.ModuleType('backend.tools.channel_send_guard')
    send_guard_stub.wait_for_send_slot = mock.MagicMock()

    event_stream = mock.MagicMock()
    event_stream_stub = types.ModuleType('backend.event_stream')
    event_stream_stub.event_stream = event_stream

    monkeypatch.setattr(notifier, 'db', fake_db)
    monkeypatch.setitem(__import__('sys').modules, 'backend.channels.registry', registry_stub)
    monkeypatch.setitem(__import__('sys').modules, 'backend.tools.channel_send_guard', send_guard_stub)
    monkeypatch.setitem(__import__('sys').modules, 'backend.event_stream', event_stream_stub)
    return fake_db


def _deliver(**kwargs):
    defaults = {
        'agent_id': 'target_agent',
        'tag': 'System/Task',
        'message': 'Reminder',
        'dedup': True,
        'trigger_llm': False,
    }
    defaults.update(kwargs)
    return notifier.notify_agent(**defaults)


def test_stale_reminder_still_dedups_while_recent(routing_env):
    routing_env.get_session_messages.return_value = [_message('[System/Task] Reminder', 30)]

    result = _deliver(dedup_max_age_seconds=300)

    assert result['success'] is False
    assert result['reason'] == 'deduplicated'
    routing_env.add_chat_message.assert_not_called()


def test_stale_reminder_redelivers_once_old_enough(routing_env):
    routing_env.get_session_messages.return_value = [_message('[System/Task] Reminder', 3600)]

    result = _deliver(dedup_max_age_seconds=300)

    assert result['success'] is True
    assert result['reason'] is None
    routing_env.add_chat_message.assert_called_once()


def test_runtime_short_circuit_reports_delivery_kind(monkeypatch):
    fake_db = mock.MagicMock()
    fake_db.get_session_messages.return_value = []
    fake_db.get_session_with_details.return_value = {
        'id': 'session-1', 'agent_id': 'target_agent',
        'external_user_id': 'web-user', 'channel_id': None,
    }
    monkeypatch.setattr(notifier, 'db', fake_db)

    fake_runtime = mock.MagicMock()
    fake_runtime.handle_message.return_value = {'injected': True, 'response': None}
    ar_pkg = types.ModuleType('backend.agent_runtime')
    ar_pkg.agent_runtime = fake_runtime
    monkeypatch.setitem(__import__('sys').modules, 'backend.agent_runtime', ar_pkg)

    result = notifier.notify_agent(
        agent_id='target_agent', tag='System/Task', message='Reminder',
        session_id='session-1', dedup=False, trigger_llm=True,
    )

    assert result['success'] is True
    assert result['delivery'] == 'injected'
