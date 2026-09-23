"""Coverage for the Kanban board task-title flash realtime publisher.

When an agent invokes a tool while working on a Kanban task, the plugin
publishes ``kanban_task_activity`` on the ``kanban`` realtime channel so the
board can flash the task title. ``turn_complete`` publishes
``kanban_task_idle`` so the flash ends shortly after the agent stops.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from plugins.kanban import handler


@pytest.fixture(autouse=True)
def reset_flash_state():
    """Reset module-level flash state around every test."""
    with handler._state_lock:
        handler._active_tasks.clear()
        handler._paused_tasks.clear()
        handler._pending_tasks.clear()
    handler._flash_last_emit.clear()
    handler._flash_agent_tasks.clear()
    handler._flash_config_cache.update({'at': 0.0, 'enabled': True, 'decay': 5})
    yield
    with handler._state_lock:
        handler._active_tasks.clear()
        handler._paused_tasks.clear()
        handler._pending_tasks.clear()
    handler._flash_last_emit.clear()
    handler._flash_agent_tasks.clear()
    handler._flash_config_cache.update({'at': 0.0, 'enabled': True, 'decay': 5})


def _set_active(task_id, agent_id='agent-1'):
    with handler._state_lock:
        handler._active_tasks[agent_id] = str(task_id)


def _publish_mock():
    store = MagicMock(return_value=1)
    # The publisher is exercised through store.publish, so expose that child.
    return patch('backend.realtime_store.realtime_store', store), store.publish


def _config(**overrides):
    cfg = {'TASK_FLASH_ENABLED': True, 'TASK_FLASH_DECAY_SECONDS': 5}
    cfg.update(overrides)
    return cfg


def test_tool_call_started_publishes_activity():
    _set_active(7)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_call_started(
            {'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())

    assert publish.call_count == 1
    channel, event_name, payload = publish.call_args.args
    assert channel == 'kanban'
    assert event_name == 'kanban_task_activity'
    assert payload['task_id'] == '7'
    assert payload['agent_id'] == 'agent-1'
    assert payload['tool_name'] == 'bash'
    assert payload['decay_seconds'] == 5
    assert isinstance(payload['timestamp'], int)


def test_no_activity_without_active_task():
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())
        handler.on_tool_executed(
            {'agent_id': 'agent-1', 'tool_name': 'bash', 'tool_result': {}}, MagicMock())

    assert publish.call_count == 0


def test_activity_is_throttled_per_task():
    _set_active(11)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        for _ in range(5):
            handler.on_tool_call_started(
                {'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())

    assert publish.call_count == 1


def test_throttle_resets_when_idle_signal_arrives():
    _set_active(12)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())
        handler.on_turn_complete({'agent_id': 'agent-1'}, MagicMock())
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())

    events = [call.args[1] for call in publish.call_args_list]
    assert events == ['kanban_task_activity', 'kanban_task_idle', 'kanban_task_activity']


def test_tool_executed_emits_activity_for_active_task():
    _set_active(21)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_executed(
            {'agent_id': 'agent-1', 'tool_name': 'read_file', 'tool_result': {}}, MagicMock())

    assert publish.call_count == 1
    assert publish.call_args.args[1] == 'kanban_task_activity'
    assert publish.call_args.args[2]['task_id'] == '21'


def test_status_update_activation_flashes_newly_picked_task():
    patcher, publish = _publish_mock()
    result = {'task': {'id': 33, 'status': 'in-progress'}}
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_executed(
            {'agent_id': 'agent-2', 'tool_name': 'kanban_update_status',
             'tool_result': json.dumps(result)}, MagicMock())

    assert with_active(handler, 'agent-2') == '33'
    assert publish.call_count == 1
    assert publish.call_args.args[0] == 'kanban'
    assert publish.call_args.args[1] == 'kanban_task_activity'
    assert publish.call_args.args[2]['task_id'] == '33'
    assert publish.call_args.args[2]['decay_seconds'] == 5


def with_active(h, agent_id):
    with h._state_lock:
        return h._active_tasks.get(agent_id)


def test_turn_complete_publishes_idle_with_grace():
    _set_active(41)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())
        handler.on_turn_complete({'agent_id': 'agent-1'}, MagicMock())

    channel, event_name, payload = publish.call_args.args
    assert channel == 'kanban'
    assert event_name == 'kanban_task_idle'
    assert payload['task_id'] == '41'
    assert payload['grace_seconds'] == 3


def test_turn_complete_without_activity_is_silent():
    _set_active(51)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config', return_value=_config()):
        handler.on_turn_complete({'agent_id': 'agent-1'}, MagicMock())

    assert publish.call_count == 0


def test_disabled_config_publishes_nothing():
    _set_active(61)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config',
                               return_value=_config(TASK_FLASH_ENABLED=False)):
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())
        handler.on_turn_complete({'agent_id': 'agent-1'}, MagicMock())

    assert publish.call_count == 0


def test_custom_decay_seconds_is_reported():
    _set_active(71)
    patcher, publish = _publish_mock()
    with patcher, patch.object(handler, '_load_config',
                               return_value=_config(TASK_FLASH_DECAY_SECONDS=12)):
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())

    assert publish.call_args.args[2]['decay_seconds'] == 12


def test_publish_failure_does_not_raise():
    _set_active(81)
    failing = MagicMock()
    failing.publish.side_effect = RuntimeError('journal down')
    with patch('backend.realtime_store.realtime_store', failing), \
            patch.object(handler, '_load_config', return_value=_config()):
        handler.on_tool_call_started({'agent_id': 'agent-1', 'tool_name': 'bash'}, MagicMock())


def _manifest():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'plugin.json')
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def test_manifest_declares_flash_events_with_handlers():
    """Every declared event must resolve to an on_<event> handler."""
    manifest = _manifest()
    events = manifest['events']
    assert 'tool_call_started' in events
    assert 'turn_complete' in events
    for event_name in events:
        assert callable(getattr(handler, f'on_{event_name}', None)), event_name


def test_manifest_flash_defaults():
    """The shipped defaults: enabled, and a 1 second fade."""
    variables = {v['name']: v for v in _manifest()['variables']}
    assert variables['TASK_FLASH_ENABLED']['default'] is True
    assert variables['TASK_FLASH_DECAY_SECONDS']['default'] == 1
