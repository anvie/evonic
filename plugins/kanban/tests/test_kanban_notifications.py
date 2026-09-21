"""Regression coverage for attachment references in task notifications."""

import os
from unittest.mock import MagicMock, patch

from plugins.kanban import handler


TASK = {
    'id': 784,
    'title': 'Review annotated design',
    'priority': 'medium',
    'description': 'Use the attached screenshot to verify the requested changes.',
}
IMAGE_ATTACHMENT = {
    'id': 31,
    'task_id': 784,
    'filename': 'review.png',
    'stored_name': 'stored-review.png',
    'mime_type': 'image/png',
}


def _assert_image_attachment_reference(message, tmp_path):
    expected_path = os.path.join(str(tmp_path), 'task_784', 'stored-review.png')
    assert 'Attachments:' in message
    assert 'name=review.png' in message
    assert 'mime_type=image/png' in message
    assert f'path={expected_path}' in message
    assert 'url=/api/kanban/attachments/31/file' in message
    assert 'describe_image' in message
    assert expected_path in message


def test_assignment_notification_includes_task_attachment_references(tmp_path):
    notifier = MagicMock(return_value={'success': True})
    attachment_db = MagicMock()
    attachment_db.get_attachments.return_value = [IMAGE_ATTACHMENT]

    with (
        patch.object(handler, '_agent_has_kanban_skill', return_value=True),
        patch.object(handler, '_busy_task_for', return_value=None),
        patch.object(handler, '_is_autopilot', return_value=False),
        patch.object(handler, '_load_config', return_value={'CLEAR_CONTEXT_ON_NEW_TASK': False}),
        patch.object(handler, '_state_lock'),
        patch('plugins.kanban.db.kanban_db', attachment_db),
        patch('plugins.kanban.db.ATTACHMENTS_DIR', str(tmp_path)),
        patch('backend.agent_runtime.notifier.notify_agent', notifier),
    ):
        result = handler._notify_agent('agent-1', TASK, 'telegram', force=True, force_delay=True)

    assert result == {'success': True}
    attachment_db.get_attachments.assert_called_once_with(784)
    _assert_image_attachment_reference(notifier.call_args.kwargs['message'], tmp_path)


def test_stale_notification_includes_task_attachment_references(tmp_path):
    notifier = MagicMock(return_value={'success': True})
    attachment_db = MagicMock()
    attachment_db.get_attachments.return_value = [IMAGE_ATTACHMENT]

    with (
        patch.object(handler, '_is_autopilot', return_value=False),
        patch.object(handler, '_state_lock'),
        patch('plugins.kanban.db.kanban_db', attachment_db),
        patch('plugins.kanban.db.ATTACHMENTS_DIR', str(tmp_path)),
        patch('backend.agent_runtime.agent_runtime.is_agent_busy', return_value=False),
        patch('backend.agent_runtime.notifier.notify_agent', notifier),
    ):
        handler._notify_stale_task('agent-1', TASK, 'telegram')

    attachment_db.get_attachments.assert_called_once_with(784)
    _assert_image_attachment_reference(notifier.call_args.kwargs['message'], tmp_path)
