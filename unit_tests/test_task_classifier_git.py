from unittest.mock import patch

from backend.task_classifier import classify_task


def test_atomic_git_operations_are_trivial_without_llm():
    cases = (
        'git status',
        'please push to origin dev',
        'now please push to origin dev',
        'git pull from origin dev',
        'fetch origin',
    )
    with patch('backend.task_classifier._get_classifier_client') as client:
        assert all(classify_task(text) == 'trivial' for text in cases)
    client.assert_not_called()


def test_git_request_with_complex_keyword_remains_complex():
    assert classify_task('please investigate and push to origin dev') == 'complex'
