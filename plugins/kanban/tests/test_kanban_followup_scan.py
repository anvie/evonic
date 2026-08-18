"""Regression coverage: a comment follow-up must survive a failed delivery.

The scan consumes comments (LLM classification) and reopens the task before it
notifies.  When the notification does not land, none of that may be silently
discarded — otherwise the comment is dropped until the process restarts.
"""

from unittest.mock import MagicMock, patch

import pytest

from plugins.kanban import handler


TASK = {
    'id': '7',
    'title': 'Ship the export button',
    'status': 'done',
    'assignee': 'agent-a',
    'completed_at': '2026-08-18T10:00:00',
    'priority': 'low',
}
COMMENT = {'id': 101, 'task_id': '7', 'content': 'Still broken on mobile.',
           'author': 'owner'}


@pytest.fixture
def kanban_db():
    db = MagicMock()
    db.get_comments_since.return_value = [dict(COMMENT)]
    db.get_attachments_for_comment.return_value = []
    db.get_last_comment_before.return_value = None
    db.get.return_value = dict(TASK, status='in-progress', completed_at=None)
    return db


@pytest.fixture(autouse=True)
def clean_state():
    """The handler keeps scanner state in module globals."""
    handler._classified_comments.clear()
    handler._pending_tasks.clear()
    handler._active_tasks.clear()
    handler._paused_tasks.clear()
    yield
    handler._classified_comments.clear()
    handler._pending_tasks.clear()
    handler._active_tasks.clear()
    handler._paused_tasks.clear()


@pytest.fixture
def scan(kanban_db):
    """Run the scan with everything but the classifier/notifier stubbed out."""
    def run(*, classify=True, notified=True):
        with patch('plugins.kanban.db.kanban_db', kanban_db), \
                patch.object(handler, '_load_config', return_value={}), \
                patch.object(handler, '_get_kanban_skill_agents',
                             return_value=['agent-a']), \
                patch.object(handler, '_load_tasks', return_value=[dict(TASK)]), \
                patch.object(handler, '_classify_followup',
                             return_value=classify) as classifier, \
                patch.object(handler, '_notify_agent_followup',
                             return_value=notified) as notifier:
            handler._scan_comments_for_followup()
            return classifier, notifier
    return run


def test_busy_agent_defers_the_comment_instead_of_consuming_it(scan, kanban_db):
    handler._active_tasks['agent-a'] = '99'

    classifier, notifier = scan()

    classifier.assert_not_called()
    notifier.assert_not_called()
    kanban_db.update.assert_not_called()
    assert COMMENT['id'] not in handler._classified_comments

    # Once the agent is free the same comment is picked up.
    handler._active_tasks.clear()
    classifier, notifier = scan()
    assert classifier.called and notifier.called


def test_failed_notification_leaves_the_comment_retryable(scan):
    _classifier, notifier = scan(notified=False)

    assert notifier.called
    assert COMMENT['id'] not in handler._classified_comments

    _classifier, notifier = scan(notified=True)
    assert notifier.called
    assert COMMENT['id'] in handler._classified_comments


def test_delivered_comment_is_not_classified_twice(scan, kanban_db):
    classifier, notifier = scan()

    assert classifier.call_count == 1
    assert notifier.call_count == 1
    assert COMMENT['id'] in handler._classified_comments
    kanban_db.update.assert_called_once_with(
        '7', {'status': 'in-progress', 'completed_at': None})

    classifier, notifier = scan()
    classifier.assert_not_called()
    notifier.assert_not_called()


def test_comment_that_needs_no_followup_is_not_reclassified(scan, kanban_db):
    classifier, notifier = scan(classify=False)

    assert classifier.call_count == 1
    notifier.assert_not_called()
    kanban_db.update.assert_not_called()
    assert COMMENT['id'] in handler._classified_comments

    classifier, _notifier = scan(classify=False)
    classifier.assert_not_called()
