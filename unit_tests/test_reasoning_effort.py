"""Per-model effort discovery, persistence and provider request boundaries."""

from unittest.mock import MagicMock, patch
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from backend.llm_client import LLMClient
from backend.reasoning_capabilities import model_reasoning_capabilities, apply_reasoning_effort
from backend.reasoning_effort_error import ReasoningEffortError
from backend.provider.codex_client import CodexClient
from models.db import db


@pytest.fixture
def client():
    from app import app
    app.config['TESTING'] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


def _provider(api_format='openai', base_url='https://api.deepseek.com/v1'):
    pid = db.create_provider({'id': 'effort-test', 'api_format': api_format,
                              'base_url': base_url, 'api_key': 'test-key'})
    return db.get_provider(pid)


def _model(**extra):
    return {'provider': 'effort-test', 'model_name': 'deepseek-v4-pro',
            'name': 'Effort model', 'type': 'remote', **extra}


@pytest.mark.parametrize('url,api_format', [
    ('https://openrouter.ai/api/v1', 'openai'),
    ('https://generativelanguage.googleapis.com/v1beta/openai', 'openai'),
    ('https://api.deepseek.com.evil.example/v1', 'openai'),
    ('http://api.deepseek.com/v1', 'openai'),
    ('https://api.deepseek.com/v1', 'anthropic'),
])
def test_gateway_and_other_formats_do_not_inherit_deepseek_support(url, api_format):
    provider = {'base_url': url, 'api_format': api_format, 'model_name': 'deepseek-v4-pro'}
    assert model_reasoning_capabilities(provider)['efforts'] == []


def test_model_routes_preserve_effort_and_reject_invalid_updates(client):
    _provider()
    response = client.post('/api/models', json=_model(reasoning_effort='max', is_default=1))
    assert response.status_code == 200
    mid = response.json['model_id']
    saved = client.get('/api/models/' + mid).json
    assert saved['reasoning_effort'] == 'max'
    assert saved['reasoning_capabilities']['efforts'] == ['low', 'high', 'max']
    assert db.get_default_model()['reasoning_effort'] == 'max'
    assert db.get_model_by_shortcode(saved['shortcode'])['reasoning_effort'] == 'max'
    assert db.get_models_by_provider('effort-test')[0]['reasoning_effort'] == 'max'
    clone = client.post('/api/models/' + mid + '/clone').json
    assert db.get_model_by_id(clone['model_id'])['reasoning_effort'] == 'max'
    response = client.put('/api/models/' + clone['model_id'],
                          json={'reasoning_effort': 'medium', 'is_default': 1})
    assert response.status_code == 400
    assert db.get_default_model()['id'] == mid
    assert client.put('/api/models/' + mid, json={'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai'}).status_code == 400
    rows = db.get_llm_models()
    db.save_llm_models(rows)
    assert db.get_model_by_id(mid)['reasoning_effort'] == 'max'
    with pytest.raises(ReasoningEffortError):
        db.save_llm_models([{**rows[0], 'reasoning_effort': 'invalid'}])
    assert len(db.get_llm_models()) == len(rows)
    assert client.put('/api/models/' + mid, json={'reasoning_effort': None}).status_code == 200
    assert db.get_model_by_id(mid)['reasoning_effort'] is None


@pytest.mark.parametrize('effort,enable,expected', [('max', True, 'max'), ('max', False, 'max'), (None, True, None), (None, False, None)])
def test_runtime_effort_is_independent_of_legacy_thinking(effort, enable, expected):
    _provider()
    client = LLMClient(_model(reasoning_effort=effort, thinking=False, timeout=10))
    response = MagicMock(status_code=200)
    response.json.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                                              'finish_reason': 'stop'}], 'usage': {}}
    with patch('backend.llm_client.requests.post', return_value=response) as post:
        result = client.chat_completion([{'role': 'user', 'content': 'hi'}], enable_thinking=enable)
    assert result['success'], result
    payload = post.call_args.kwargs['json']
    assert payload.get('reasoning_effort') == expected
    if expected is None:
        assert 'reasoning_effort' not in payload


@pytest.mark.parametrize('enable', [True, False])
def test_invalid_runtime_effort_fails_before_network(enable):
    _provider()
    client = LLMClient(_model(reasoning_effort='ultra'))
    with patch('backend.llm_client.requests.post') as post:
        result = client.chat_completion([{'role': 'user', 'content': 'hi'}], enable_thinking=enable)
    assert result['error_type'] == 'configuration_error'
    post.assert_not_called()


def test_codex_effort_merges_summary_and_fast_mode():
    client = CodexClient('token', 'https://chatgpt.com/backend-api/codex')
    response = MagicMock(status_code=200)
    response.json.return_value = {'output': [], 'usage': {}}
    with patch('backend.provider.codex_client.httpx.post', return_value=response) as post:
        client.send_request('gpt-5.6-luna', [{'role': 'user', 'content': 'hi'}],
                            stream=False, reasoning=True, reasoning_effort='high', service_tier='priority')
    payload = post.call_args.kwargs['json']
    assert payload['reasoning'] == {'summary': 'auto', 'effort': 'high'}
    assert payload['service_tier'] == 'priority'


@pytest.mark.parametrize('enable', [True, False])
def test_anthropic_runtime_effort_without_native_thinking(enable):
    provider = _provider('anthropic', 'https://api.anthropic.com/v1')
    client = LLMClient(_model(model_name='claude-opus-4-6', reasoning_effort='high', thinking=False))
    response = MagicMock(status_code=200)
    response.json.return_value = {'content': [{'type': 'text', 'text': 'ok'}],
                                  'stop_reason': 'end_turn', 'usage': {}}
    with patch('backend.llm_client.requests.post', return_value=response) as post:
        assert client.chat_completion([{'role': 'user', 'content': 'hi'}], enable_thinking=enable)['success']
    payload = post.call_args.kwargs['json']
    assert payload['output_config'] == {'effort': 'high'}
    assert 'thinking' not in payload


def test_codex_stream_effort_merges_summary():
    client = CodexClient('token', 'https://chatgpt.com/backend-api/codex')
    with patch('backend.provider.codex_client.httpx.Client') as http:
        stream = http.return_value.__enter__.return_value.stream
        response = stream.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_lines.return_value = []
        list(client.stream_chunks('gpt-5.6-luna', [], reasoning=True, reasoning_effort='high'))
    assert stream.call_args.kwargs['json']['reasoning'] == {'summary': 'auto', 'effort': 'high'}


def test_reasoning_dropdown_behavior():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend behavior checks')
    subprocess.run([node, 'unit_tests/test_settings_models_reasoning.js'],
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=15)


@pytest.mark.parametrize('api_format,url,field', [
    ('codex', 'https://chatgpt.com/backend-api/codex', 'reasoning'),
    ('anthropic', 'https://api.anthropic.com/v1', 'output_config'),
    ('openai', 'https://api.deepseek.com/v1', 'reasoning_effort'),
])
def test_unidentified_model_accepts_manual_effort(client, api_format, url, field):
    provider = _provider(api_format, url)
    model = _model(model_name='new-model', reasoning_effort='custom_level')
    caps = db.get_model_reasoning_capabilities(model)
    assert caps['manual'] is True and caps['efforts'] == []
    created = client.post('/api/models', json=model)
    assert created.status_code == 200
    mid = created.json['model_id']
    saved = client.get('/api/models/' + mid).json
    assert saved['reasoning_effort'] == 'custom_level'
    assert saved['reasoning_capabilities']['manual'] is True
    resolved = db.resolve_model_config(saved)
    payload = {}
    apply_reasoning_effort(payload, resolved, db.validate_model_reasoning(saved))
    assert payload[field] == ('custom_level' if field == 'reasoning_effort' else {'effort': 'custom_level'})
    for invalid in ('high!', 'a' * 33, 123):
        assert client.put('/api/models/' + mid, json={'reasoning_effort': invalid}).status_code == 400
    assert client.put('/api/models/' + mid, json={'reasoning_effort': ''}).status_code == 200
    assert db.get_model_by_id(mid)['reasoning_effort'] is None
    assert db.get_model_reasoning_capabilities({**model, 'base_url': 'https://gateway.example'})['manual']


@pytest.mark.parametrize('model', ['gpt-6-astra', 'deepseek-v4-pro'])
def test_cavoti_uses_manual_effort_despite_model_name_and_cached_levels(client, model):
    provider = _provider('openai', 'https://cavoti.com/v1')
    snapshot = {'base_url': provider['base_url'], 'api_format': 'openai',
                'models': {model: {'efforts': ['low'], 'default_effort': 'low', 'manual': False}}}
    with db._connect() as conn:
        if 'model_capabilities' not in [row[1] for row in conn.execute('PRAGMA table_info(providers)')]:
            conn.execute("ALTER TABLE providers ADD COLUMN model_capabilities TEXT DEFAULT '{}'")
        conn.execute('UPDATE providers SET model_capabilities = ? WHERE id = ?',
                     (json.dumps(snapshot), provider['id']))
        conn.commit()
    assert db.get_model_reasoning_capabilities(_model(model_name=model)) == {
        'efforts': [], 'default_effort': None, 'manual': True}
    created = client.post('/api/models', json=_model(model_name=model, reasoning_effort='custom_level'))
    assert created.status_code == 200
    mid = created.json['model_id']
    saved = client.get('/api/models/' + mid).json
    assert saved['reasoning_effort'] == 'custom_level'
    clone = client.post('/api/models/' + mid + '/clone').json
    assert db.get_model_by_id(clone['model_id'])['reasoning_effort'] == 'custom_level'
    llm = LLMClient({**saved, 'timeout': 10})
    response = MagicMock(status_code=200)
    response.json.return_value = {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {}}
    with patch('backend.llm_client.requests.post', return_value=response) as post:
        assert llm.chat_completion([{'role': 'user', 'content': 'hi'}], enable_thinking=False)['success']
        assert post.call_args.kwargs['json']['reasoning_effort'] == 'custom_level'
        llm.reasoning_effort = None
        assert llm.chat_completion([{'role': 'user', 'content': 'hi'}], enable_thinking=False)['success']
        assert 'reasoning_effort' not in post.call_args.kwargs['json']


@pytest.mark.parametrize('api_format,url,manual', [
    ('openai', 'http://localhost:11434/v1', True),
    ('openai', 'https://openrouter.ai/api/v1', True),
    ('codex', 'https://gateway.example/codex', True),
    ('anthropic', 'https://gateway.example/anthropic', True),
    ('openai', 'https://generativelanguage.googleapis.com/v1beta/openai', False),
    ('ollama', 'http://localhost:11434/api', False),
    ('openai', 'https://ollama.com/api', False),
])
def test_manual_support_follows_effective_request_format(api_format, url, manual):
    config = {'api_format': api_format, 'base_url': url, 'model_name': 'gpt-6-astra'}
    assert model_reasoning_capabilities(config)['manual'] is manual
    assert model_reasoning_capabilities(config)['efforts'] == []
    payload = {}
    if manual:
        apply_reasoning_effort(payload, config, 'high')
        expected = {'codex': {'reasoning': {'effort': 'high'}},
                    'anthropic': {'output_config': {'effort': 'high'}},
                    'openai': {'reasoning_effort': 'high'}}
        assert payload == expected[api_format]
    else:
        with pytest.raises(ReasoningEffortError):
            apply_reasoning_effort(payload, config, 'high')


def test_codex_effort_is_forwarded_when_thinking_disabled():
    _provider('codex', 'https://chatgpt.com/backend-api/codex')
    llm = LLMClient(_model(model_name='gpt-6-astra', reasoning_effort='high', thinking=False))
    response = {'success': True, 'response': {'choices': [{'message': {'content': 'ok'}}], 'usage': {}}}
    with patch('backend.provider.oauth_codex.get_valid_token', return_value='token'), \
         patch('backend.provider.codex_client.CodexClient.send_request', return_value=response) as send:
        assert llm.chat_completion([{'role': 'user', 'content': 'hi'}], enable_thinking=False)['success']
    assert send.call_args.kwargs['reasoning_effort'] == 'high'
    assert send.call_args.kwargs['reasoning'] is False


@pytest.mark.parametrize('model,efforts', [
    ('gpt-6-astra', ['low', 'medium', 'high', 'xhigh', 'max']),
    ('gpt-5.6-sol', ['low', 'medium', 'high', 'xhigh', 'max']),
    ('gpt-5.6-terra', ['low', 'medium', 'high', 'xhigh', 'max']),
    ('gpt-5.6-luna', ['low', 'medium', 'high', 'xhigh', 'max']),
    ('gpt-5.5', ['low', 'medium', 'high', 'xhigh']),
])
def test_codex_api_levels_ignore_old_harness_metadata(client, model, efforts):
    provider = _provider('codex', 'https://chatgpt.com/backend-api/codex')
    with db._connect() as conn:
        if 'model_capabilities' not in [row[1] for row in conn.execute('PRAGMA table_info(providers)')]:
            conn.execute("ALTER TABLE providers ADD COLUMN model_capabilities TEXT DEFAULT '{}'")
        conn.execute('UPDATE providers SET model_capabilities = ? WHERE id = ?',
                     (json.dumps({'models': {model: {'efforts': ['ultra']}}}), provider['id']))
        conn.commit()
    with patch('requests.get') as requests_get, patch('httpx.get') as httpx_get:
        catalog = client.get('/api/providers').json['reasoning_catalog']
        assert catalog['codex']['models'][model]['efforts'] == efforts
        assert db.get_model_reasoning_capabilities(_model(model_name=model))['efforts'] == efforts
        requests_get.assert_not_called()
        httpx_get.assert_not_called()
    assert client.post('/api/models', json=_model(model_name=model, reasoning_effort='ultra')).status_code == 400
    assert client.post('/api/models', json=_model(model_name=model, reasoning_effort='high')).status_code == 200


@pytest.mark.parametrize('model,efforts', [
    ('claude-opus-4-5', ['low', 'medium', 'high']),
    ('claude-opus-4-6', ['low', 'medium', 'high', 'max']),
    ('claude-sonnet-4-6', ['low', 'medium', 'high', 'max']),
    ('claude-opus-4-7', ['low', 'medium', 'high', 'xhigh', 'max']),
])
def test_anthropic_levels_are_model_specific(model, efforts):
    config = {'base_url': 'https://api.anthropic.com/v1', 'api_format': 'anthropic', 'model_name': model}
    assert model_reasoning_capabilities(config)['efforts'] == efforts
    assert model_reasoning_capabilities({**config, 'base_url': 'https://cavoti.com/v1'})['manual']


def test_spark_without_api_documentation_is_manual():
    config = {'base_url': 'https://chatgpt.com/backend-api/codex', 'api_format': 'codex',
              'model_name': 'gpt-5.3-codex-spark'}
    assert model_reasoning_capabilities(config)['manual']
