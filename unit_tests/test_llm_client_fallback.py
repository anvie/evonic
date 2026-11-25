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


def _chain_settings(chain_json, legacy=''):
    values = {'default_model_fallback_ids': chain_json,
              'default_model_fallback_id': legacy}
    return lambda key, default=None: values.get(key, default)


def _model(name, **extra):
    return dict(FALLBACK, id=name, model_name=name,
                base_url=f'https://{name}.example/v1', **extra)


def test_global_chain_is_tried_in_order_until_one_succeeds():
    rows = {'fb1': _model('fb1'), 'fb2': _model('fb2'), 'fb3': _model('fb3')}
    calls = []

    def scripted(self, **_kwargs):
        calls.append(self.model)
        return dict(SUCCESS) if self.model == 'fb2' else dict(FAILURE)

    client = LLMClient()
    client.model = 'primary-model'
    with patch('models.db.db.get_setting',
               side_effect=_chain_settings('["fb1", "fb2", "fb3"]')), \
            patch('models.db.db.get_model_by_id', side_effect=rows.get), \
            patch.object(LLMClient, '_chat_completion_once', scripted):
        result = client.chat_completion(messages=[])

    assert result['success'] is True
    assert result['fallback_model'] == 'fb2'
    assert calls == ['primary-model', 'fb1', 'fb2']


def test_global_chain_skips_missing_disabled_and_primary_models():
    rows = {'fb1': _model('fb1', enabled=0), 'fb3': _model('fb3'),
            'same': dict(_model('primary-model'), base_url='')}
    client = LLMClient()
    client.model, client.base_url = 'primary-model', ''
    with patch('models.db.db.get_setting',
               side_effect=_chain_settings('["fb1", "gone", "same", "fb3"]')), \
            patch('models.db.db.get_model_by_id', side_effect=rows.get):
        fallbacks = client._get_fallback_clients()

    assert [f.model for f in fallbacks] == ['fb3']


def test_whole_chain_failing_returns_the_primary_error():
    rows = {'fb1': _model('fb1'), 'fb2': _model('fb2')}
    client = LLMClient()
    client.model = 'primary-model'
    with patch('models.db.db.get_setting',
               side_effect=_chain_settings('["fb1", "fb2"]')), \
            patch('models.db.db.get_model_by_id', side_effect=rows.get), \
            patch.object(LLMClient, '_chat_completion_once',
                         lambda self, **kw: dict(FAILURE)):
        result = client.chat_completion(messages=[])

    assert result['success'] is False
    assert 'fallback_used' not in result


def test_legacy_single_fallback_setting_is_read_as_a_one_entry_chain():
    from backend.default_model_chain import get_fallback_ids

    class FakeDb:
        def get_setting(self, key, default=None):
            return {'default_model_fallback_id': 'fb1'}.get(key, default)

    assert get_fallback_ids(FakeDb()) == ['fb1']


def test_saving_the_chain_mirrors_the_first_entry_to_the_legacy_key():
    from backend.default_model_chain import save_fallback_ids

    class FakeDb:
        def __init__(self):
            self.values = {}

        def set_setting(self, key, value):
            self.values[key] = value

    fake = FakeDb()
    saved = save_fallback_ids(fake, ['a', '', 'b', 'a', 'c', 'd', 'e', 'f'])
    assert saved == ['a', 'b', 'c', 'd', 'e']
    assert fake.values['default_model_fallback_id'] == 'a'
    assert fake.values['default_model_fallback_ids'] == '["a", "b", "c", "d", "e"]'


# ── Explorer model chain ──────────────────────────────────────────────────

def test_explorer_on_the_global_model_drops_its_own_fallbacks():
    from backend.agent_runtime.explorer import _model_fields
    fields = _model_fields({}, {'model_id': '', 'fallback_model_ids': ['a', 'b'],
                                'fallback_model_id': 'a'})
    assert fields == {'model_id': None, 'fallback_model_ids': [], 'fallback_model_id': None}


def test_explorer_custom_chain_keeps_order_and_skips_the_main_model():
    from backend.agent_runtime.explorer import _model_fields
    fields = _model_fields({}, {'model_id': 'm', 'fallback_model_ids': ['a', 'm', 'b', 'a']})
    assert fields['fallback_model_ids'] == ['a', 'b']
    assert fields['fallback_model_id'] == 'a'


def test_explorer_legacy_single_fallback_still_applies():
    from backend.agent_runtime.explorer import _model_fields
    fields = _model_fields({}, {'model_id': 'm', 'fallback_model_id': 'a'})
    assert fields['fallback_model_ids'] == ['a']


def test_explorer_inheriting_the_parent_copies_its_model_and_fallback():
    from backend.agent_runtime.explorer import _model_fields
    fields = _model_fields({'model_id': 'p', 'fallback_model_id': 'pf'},
                           {'inherit_parent_model': True, 'model_id': 'x',
                            'fallback_model_ids': ['y']})
    assert fields == {'model_id': 'p', 'fallback_model_ids': ['pf'], 'fallback_model_id': 'pf'}


def test_explorer_fallback_models_resolve_in_order_and_skip_unusable():
    from backend.agent_runtime import explorer
    rows = {'a': {'id': 'a', 'enabled': 0}, 'b': {'id': 'b', 'enabled': 1},
            'c': {'id': 'c', 'enabled': 1}}
    agent = {'is_explorer': True, 'fallback_model_ids': ['a', 'gone', 'b', 'c']}
    with patch('models.db.db.get_model_by_id', side_effect=rows.get):
        assert [m['id'] for m in explorer.fallback_models(agent)] == ['b', 'c']
        assert explorer.fallback_model(agent)['id'] == 'b'
    assert explorer.fallback_models({'fallback_model_ids': ['b']}) == []  # not an explorer
