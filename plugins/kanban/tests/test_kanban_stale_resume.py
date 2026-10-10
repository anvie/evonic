"""Regression coverage for the stale-task resume notification.

A stale in-progress task is resumed by reserving agent state and sending a
reminder.  The reservation must be transactional: when the reminder is
deduplicated, fails, or is only handed off to a live loop (injected/buffered),
the reservation has to be released so the next scan can retry instead of the
agent being pinned active forever.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from plugins.kanban import handler


TASK = {
    'id': 842,
    'title': 'Proactive compaction',
    'description': 'Resume the interrupted compaction task.',
}


@pytest.fixture(autouse=True)
def clean_state():
    for name in ('_active_tasks', '_pending_tasks', '_paused_tasks',
                 '_task_state_since'):
        getattr(handler, name).clear()
    yield
    for name in ('_active_tasks', '_pending_tasks', '_paused_tasks',
                 '_task_state_since'):
        getattr(handler, name).clear()


def _run(notify_result, *, config=None, prev_active=None, prev_since=None,
         prev_state='PREV-STATE', mode_preset=False):
    notifier = MagicMock(return_value=notify_result)
    kanban_db = MagicMock()
    kanban_db.get_attachments.return_value = []
    model_db = MagicMock()
    model_db.get_agent_state.return_value = prev_state

    if prev_active is not None:
        handler._active_tasks['agent-a'] = str(prev_active)
    if prev_since is not None:
        handler._task_state_since['agent-a'] = prev_since

    with patch.object(handler, '_is_autopilot', return_value=False), \
            patch.object(handler, '_load_config', return_value=(config or {})), \
            patch.object(handler, '_state_lock', threading.Lock()), \
            patch.object(handler, '_pre_set_execute_mode',
                         return_value=mode_preset) as preset, \
            patch('plugins.kanban.db.kanban_db', kanban_db), \
            patch('models.db.db', model_db), \
            patch('backend.agent_runtime.agent_runtime.is_agent_busy',
                  return_value=False), \
            patch('backend.agent_runtime.notifier.notify_agent', notifier):
        handler._notify_stale_task('agent-a', TASK, 'telegram')
    return notifier, model_db, preset


def test_confirmed_runtime_delivery_keeps_the_reservation():
    _run({'success': True, 'delivery': 'runtime'})
    assert handler._active_tasks.get('agent-a') == '842'
    assert 'agent-a' in handler._task_state_since


def test_deduplicated_reminder_releases_the_reservation():
    _run({'success': False, 'reason': 'deduplicated'})
    assert 'agent-a' not in handler._active_tasks
    assert 'agent-a' not in handler._task_state_since


def test_failed_reminder_releases_the_reservation():
    _run({'success': False, 'reason': 'error'})
    assert 'agent-a' not in handler._active_tasks
    assert 'agent-a' not in handler._task_state_since


def test_injected_reminder_releases_the_reservation_for_retry():
    _run({'success': True, 'delivery': 'injected'})
    assert 'agent-a' not in handler._active_tasks


def test_buffered_reminder_releases_the_reservation_for_retry():
    _run({'success': True, 'delivery': 'buffered'})
    assert 'agent-a' not in handler._active_tasks


def test_rollback_restores_a_previous_reservation():
    _run({'success': False, 'reason': 'deduplicated'},
         prev_active=111, prev_since=123.5)
    assert handler._active_tasks.get('agent-a') == '111'
    assert handler._task_state_since.get('agent-a') == 123.5


def test_dedup_max_age_is_forwarded_from_config():
    notifier, _db, _preset = _run(
        {'success': True, 'delivery': 'runtime'},
        config={'STALE_REMINDER_DEDUP_MAX_AGE_SECONDS': 120},
    )
    kwargs = notifier.call_args.kwargs
    assert kwargs['dedup'] is True
    assert kwargs['dedup_max_age_seconds'] == 120


def test_dedup_max_age_defaults_when_config_is_missing():
    notifier, _db, _preset = _run({'success': True, 'delivery': 'runtime'})
    assert notifier.call_args.kwargs['dedup_max_age_seconds'] == 300


def test_preset_mode_is_reverted_when_delivery_is_not_confirmed():
    _notifier, model_db, preset = _run(
        {'success': False, 'reason': 'deduplicated'},
        prev_state='PREV-STATE', mode_preset=True,
    )
    preset.assert_called_once()
    model_db.upsert_agent_state.assert_called_once_with(
        'PREV-STATE', agent_id='agent-a')


def test_preset_mode_is_kept_on_confirmed_delivery():
    _notifier, model_db, preset = _run(
        {'success': True, 'delivery': 'runtime'},
        prev_state='PREV-STATE', mode_preset=True,
    )
    preset.assert_called_once()
    model_db.upsert_agent_state.assert_not_called()
