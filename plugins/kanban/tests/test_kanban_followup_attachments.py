"""Regression coverage for Kanban follow-up comment attachment forwarding."""

import os
from unittest.mock import patch

from plugins.kanban import handler


def test_format_followup_comment_includes_image_reference_and_guidance(tmp_path):
    comment = {'id': 42, 'task_id': 737, 'content': 'Fix the highlighted overflow.'}
    attachment = {
        'id': 9,
        'task_id': 737,
        'filename': 'mobile-overflow.png',
        'stored_name': 'stored-mobile-overflow.png',
        'mime_type': 'image/png',
        'size': 1234,
    }

    with patch('plugins.kanban.db.ATTACHMENTS_DIR', str(tmp_path)):
        rendered = handler._format_followup_comment(comment, [attachment])

    expected_path = os.path.join(str(tmp_path), 'task_737', 'stored-mobile-overflow.png')
    assert 'Fix the highlighted overflow.' in rendered
    assert 'name=mobile-overflow.png' in rendered
    assert 'mime_type=image/png' in rendered
    assert f'path={expected_path}' in rendered
    assert 'url=/api/kanban/attachments/9/file' in rendered
    assert 'describe_image' in rendered
    assert expected_path in rendered


def test_format_followup_comment_includes_non_image_attachment_without_vision_guidance(tmp_path):
    comment = {'id': 43, 'task_id': 737, 'content': 'Use the attached specification.'}
    attachment = {
        'id': 10,
        'task_id': 737,
        'filename': 'requirements.pdf',
        'stored_name': 'stored-requirements.pdf',
        'mime_type': 'application/pdf',
        'size': 5678,
    }

    with patch('plugins.kanban.db.ATTACHMENTS_DIR', str(tmp_path)):
        rendered = handler._format_followup_comment(comment, [attachment])

    expected_path = os.path.join(str(tmp_path), 'task_737', 'stored-requirements.pdf')
    assert 'name=requirements.pdf' in rendered
    assert 'mime_type=application/pdf' in rendered
    assert f'path={expected_path}' in rendered
    assert 'url=/api/kanban/attachments/10/file' in rendered
    assert 'describe_image' not in rendered


def test_format_followup_comment_keeps_text_only_comment_unchanged():
    comment = {'id': 44, 'task_id': 737, 'content': 'Please fix the mobile layout.'}

    assert handler._format_followup_comment(comment, []) == 'Please fix the mobile layout.'
