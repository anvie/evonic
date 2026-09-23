"""LLMClient fallback: a failed primary call is retried once on the fallback.

Generic callers (classifiers, plugins, dashboards) build an LLMClient and never
touch the agent runtime, so the fallback has to live in the client itself.
"""

from unittest.mock import patch

import pytest

from backend.llm_client import LLMClient


PRIMARY = {
    'provider': 'primary-provider',
    'base_url': 'https://primary.example/v1',
    'api_key': 'key-primary',
    'model_name': 'primary-model',
    'timeout': 5,
    'thinking': False,
    'api_format': 'openai',
}
FALLBACK = {
    'provider': 'fallback-provider',
    'base_url': 'https://fallback.example/v1',
    'api_key': 'key-fallback',
    'model_name': 'fallback-model',
    'timeout': 5,
    'thinking': False,
    'api_format': 'openai',
}

FAILURE = {'success': False, 'error_type': 'rate_limit_error',
           'response': {'error': 'quota exhausted'}}
SUCCESS = {'success': True,
           'response': {'choices': [{'message': {'content': 'ok'}}]}}


@pytest.fixture(autouse=True)
def passthrough_resolve():
    """Keep model configs verbatim so tests never touch the real DB."""
    with patch('models.db.db.resolve_model_config', side_effect=lambda cfg: cfg):
        yield


def _scripted(self, **_kwargs):
    """Answer like the provider would: primary fails, fallback works."""
    if self.model == 'primary-model':
        return dict(FAILURE)
    return dict(SUCCESS)


def test_primary_failure_falls_back_and_tags_the_result():
    client = LLMClient(model_config=dict(PRIMARY),
                       fallback_model_config=dict(FALLBACK))
    with patch.object(LLMClient, '_chat_completion_once', _scripted):
        result = client.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])

    assert result['success'] is True
    assert result['fallback_used'] is True
    assert result['primary_model'] == 'primary-model'
    assert result['primary_error_type'] == 'rate_limit_error'


def test_healthy_primary_never_calls_the_fallback():
    client = LLMClient(model_config=dict(PRIMARY),
                       fallback_model_config=dict(FALLBACK))
    calls = []

    def only_primary(self, **kwargs):
        calls.append(self.model)
        return dict(SUCCESS)

    with patch.object(LLMClient, '_chat_completion_once', only_primary):
        result = client.chat_completion(messages=[])

    assert result['success'] is True
    assert 'fallback_used' not in result
    assert calls == ['primary-model']


def test_failed_fallback_returns_the_primary_error():
    client = LLMClient(model_config=dict(PRIMARY),
                       fallback_model_config=dict(FALLBACK))

    def both_fail(self, **kwargs):
        return dict(FAILURE)

    with patch.object(LLMClient, '_chat_completion_once', both_fail):
        result = client.chat_completion(messages=[])

    assert result['success'] is False
    assert result['error_type'] == 'rate_limit_error'
    assert 'fallback_used' not in result


def test_explicit_model_without_fallback_does_not_resolve_the_global_one():
    """A caller-chosen model must not be silently swapped for another one."""
    client = LLMClient(model_config=dict(PRIMARY))
    with patch('models.db.db.get_setting', return_value='fallback-model-id') as get_setting:
        assert client._get_fallback_client() is None
    get_setting.assert_not_called()


def test_fallback_client_never_retries_itself():
    client = LLMClient(model_config=dict(PRIMARY),
                       fallback_model_config=dict(FALLBACK),
                       _allow_fallback=False)
    with patch.object(LLMClient, '_chat_completion_once', _scripted):
        result = client.chat_completion(messages=[])

    assert result['success'] is False
    assert 'fallback_used' not in result


def test_same_model_as_primary_is_not_used_as_fallback():
    client = LLMClient(model_config=dict(PRIMARY),
                       fallback_model_config=dict(PRIMARY))
    assert client._get_fallback_client() is None


def test_global_default_client_resolves_the_fallback_setting():
    """The default model with no explicit fallback uses the global setting."""
    client = LLMClient()  # model_config=None -> default model
    assert client.model is None or client.model  # smoke: construction survived

    with patch('models.db.db.get_setting', return_value='deepseek/deepseek-v4-flash'), \
            patch('models.db.db.get_model_by_id', return_value=dict(FALLBACK)):
        fallback = client._get_fallback_client()

    assert fallback is not None
    assert fallback.model == 'fallback-model'


def test_disabled_fallback_model_is_ignored():
    disabled = dict(FALLBACK, enabled=0)
    client = LLMClient(model_config=None, fallback_model_config=disabled)
    assert client._get_fallback_client() is None
