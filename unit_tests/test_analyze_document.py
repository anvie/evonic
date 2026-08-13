"""Native document validation, routing, and provider payload contracts."""

import json
import os

import pytest

from backend.llm_client import _convert_multimodal_to_claude
from backend.provider.codex_client import CodexClient
from backend.tools import analyze_document as ap
from models.db import db


def _agent(agent_id='pdf_agent', **extra):
    db.create_agent({
        'id': agent_id,
        'name': agent_id,
        'system_prompt': '',
        **extra,
    })
    return agent_id


def _attachment(tmp_path, agent_id, session_id='s1', name='document.pdf',
                mime='application/pdf', body=b'%PDF-1.7\nexample'):
    path = tmp_path / 'attachments' / agent_id / session_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    db.save_attachment(
        agent_id=agent_id,
        session_id=session_id,
        filename=os.path.basename(path),
        file_path=str(path),
        original_filename=name,
        mime_type=mime,
        file_type='document',
        size_bytes=len(body),
    )
    return str(path)


@pytest.fixture(autouse=True)
def _attachment_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, '_ATTACHMENTS_ROOT', str(tmp_path / 'attachments'))


def _model(model_id, *, provider='openrouter', api_format='openai',
           base_url='https://api.openai.com/v1', api_key='test-key',
           category='pdf'):
    db.create_model({
        'id': model_id,
        'name': model_id,
        'type': 'remote',
        'provider': provider,
        'base_url': base_url,
        'api_key': api_key,
        'model_name': model_id,
        'api_format': api_format,
        'enabled': 1,
        f'document_{category}_supported': 1,
    })
    return model_id


def _success(text='PDF answer'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': text}}]},
    }


def test_openai_receives_native_file_block(tmp_path, monkeypatch):
    agent_id = _agent()
    path = _attachment(tmp_path, agent_id)
    db.set_setting('document_model_id', _model('openai-pdf'))
    captured = {}

    class FakeClient:
        timeout = 60

        def __init__(self, model_config):
            captured['model'] = model_config

        def chat_completion(self, **kwargs):
            captured.update(kwargs)
            return _success()

    monkeypatch.setattr(ap, 'LLMClient', FakeClient)
    result = ap.execute(
        {'id': agent_id, 'session_id': 's1', 'document_enabled': 1},
        {'path': path, 'query': 'What is this?'},
    )

    assert result == 'PDF answer'
    parts = captured['messages'][1]['content']
    assert parts[0]['type'] == 'text'
    assert parts[1]['type'] == 'file'
    assert parts[1]['file']['filename'] == 'document.pdf'
    assert parts[1]['file']['file_data'].startswith('data:application/pdf;base64,')
    assert captured['max_tokens'] == 4096
    assert captured['enable_thinking'] is False


def test_anthropic_converter_maps_file_to_document_block():
    messages = [{
        'role': 'user',
        'content': [{
            'type': 'file',
            'file': {
                'filename': 'document.pdf',
                'file_data': 'data:application/pdf;base64,UEZERGF0YQ==',
            },
        }],
    }]

    converted = _convert_multimodal_to_claude(messages)

    document = converted[0]['content'][0]
    assert document == {
        'type': 'document',
        'source': {
            'type': 'base64',
            'media_type': 'application/pdf',
            'data': 'UEZERGF0YQ==',
        },
    }


def test_codex_converter_maps_file_to_responses_input_file():
    converted = CodexClient._convert_messages([{
        'role': 'user',
        'content': [{
            'type': 'file',
            'file': {
                'filename': 'document.pdf',
                'file_data': 'data:application/pdf;base64,UEZERGF0YQ==',
            },
        }],
    }])

    assert converted[0]['content'][0] == {
        'type': 'input_file',
        'filename': 'document.pdf',
        'file_data': 'data:application/pdf;base64,UEZERGF0YQ==',
    }


def test_gemini_uses_direct_generate_content_with_inline_pdf(tmp_path, monkeypatch):
    agent_id = _agent('gemini_pdf_agent')
    path = _attachment(tmp_path, agent_id)
    model_id = _model(
        'gemini-model',
        provider='google-gemini',
        base_url='',
        api_key='gemini-key',
    )
    with db._connect() as conn:
        conn.execute(
            "UPDATE llm_models SET model_name = 'models/gemini-2.5-pro' WHERE id = ?",
            (model_id,),
        )
        conn.commit()
    db.set_setting('document_model_id', model_id)
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {'candidates': [{'content': {'parts': [{'text': 'Gemini answer'}]}}]}

    def fake_post(url, **kwargs):
        captured['url'] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(ap.requests, 'post', fake_post)
    result = ap.execute(
        {'id': agent_id, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    )

    assert result == 'Gemini answer'
    assert captured['url'].endswith('/v1beta/models/gemini-2.5-pro:generateContent')
    assert captured['headers']['x-goog-api-key'] == 'gemini-key'
    inline = captured['json']['contents'][0]['parts'][1]['inline_data']
    assert inline['mime_type'] == 'application/pdf'
    assert inline['data']
    assert 'untrusted' in captured['json']['system_instruction']['parts'][0]['text']


def test_falls_back_to_second_native_model(tmp_path, monkeypatch):
    agent_id = _agent('fallback_pdf_agent')
    path = _attachment(tmp_path, agent_id)
    db.set_setting('document_model_id', _model('pdf-primary'))
    db.set_setting('document_fallback_model_id', _model('pdf-fallback'))
    calls = []

    class FakeClient:
        timeout = 60

        def __init__(self, model_config):
            self.model_id = model_config['id']

        def chat_completion(self, **kwargs):
            calls.append(self.model_id)
            if self.model_id == 'pdf-primary':
                return {'success': False, 'error_type': 'api_error', 'error_detail': 'down'}
            return _success('fallback answer')

    monkeypatch.setattr(ap, 'LLMClient', FakeClient)
    result = ap.execute(
        {'id': agent_id, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    )

    assert result == 'fallback answer'
    assert calls == ['pdf-primary', 'pdf-fallback']


def test_rejects_disabled_cross_session_unsupported_and_bad_signature(tmp_path):
    owner = _agent('pdf_validation_agent')
    valid_path = _attachment(tmp_path, owner, session_id='owner-session')

    assert 'document_enabled=0' in ap.execute(
        {'id': owner, 'document_enabled': 0}, {'path': valid_path}
    )
    assert 'does not belong to this agent and session' in ap.execute(
        {'id': owner, 'session_id': 'other-session', 'document_enabled': 1},
        {'path': valid_path},
    )

    unsupported_path = _attachment(
        tmp_path, owner, name='archive.zip', mime='application/zip', body=b'PK'
    )
    assert 'unsupported' in ap.execute(
        {'id': owner, 'session_id': 's1', 'document_enabled': 1},
        {'path': unsupported_path},
    )

    fake_path = _attachment(
        tmp_path, owner, name='fake.pdf', body=b'not actually a PDF'
    )
    assert 'valid PDF signature' in ap.execute(
        {'id': owner, 'session_id': 's1', 'document_enabled': 1},
        {'path': fake_path},
    )

    assert 'non-empty filesystem path' in ap.execute(
        {'id': owner, 'document_enabled': 1}, {'path': 1.5}
    )
    assert 'filesystem path, not a URL' in ap.execute(
        {'id': owner, 'document_enabled': 1},
        {'path': 'https://example.com/document.pdf'},
    )
    assert 'non-empty filesystem path' in ap.execute(
        {'id': owner, 'document_enabled': 1}, {'attachment_id': 1}
    )
    assert 'Invalid document analysis context' in ap.execute({}, [])


def test_rejects_cross_agent_attachment(tmp_path):
    owner = _agent('pdf_owner')
    other = _agent('pdf_other')
    path = _attachment(tmp_path, owner)

    result = ap.execute(
        {'id': other, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    )

    assert 'does not belong to this agent and session' in result

    alias = '/workspace/' + os.path.relpath(path, tmp_path)
    alias_result = ap.execute(
        {
            'id': other,
            'session_id': 's1',
            'document_enabled': 1,
            'workspace': str(tmp_path),
        },
        {'path': alias},
    )
    assert 'does not belong to this agent and session' in alias_result

    data, name, mime, error = ap._read_path(
        {'id': owner, 'session_id': 's1', 'workspace': str(tmp_path)},
        alias,
    )
    assert (data, name, mime, error) == (
        b'%PDF-1.7\nexample', 'document.pdf', 'application/pdf', ''
    )


def test_reads_workspace_and_scratchpad_paths_from_active_backend(monkeypatch):
    from backend.tools._workspace import scratch_dir
    from backend.tools.lib.exec_backend import registry

    class FakeBackend:
        target = None

        def resolve_path(self, path):
            self.target = path
            return path

        def file_stat(self, path):
            return {'exists': True, 'size': 16}

        def cat_file_bytes(self, path):
            return {'bytes': b'%PDF-1.7\nremote'}

    backend = FakeBackend()
    monkeypatch.setattr(registry, 'get_backend', lambda session_id, agent: backend)
    cases = (
        (
            {'id': 'remote', 'session_id': 'remote-session', 'workspace': '/remote/project'},
            '/workspace/docs/report.pdf',
            '/remote/project/docs/report.pdf',
        ),
        (
            {'id': 'remote', 'session_id': 'remote-session', 'workspace': '/remote/project'},
            'docs/report.pdf',
            '/remote/project/docs/report.pdf',
        ),
        (
            {'id': 'worker', 'session_id': 'worker-session', 'is_subagent': True},
            'report.pdf',
            f"{scratch_dir('worker')}/report.pdf",
        ),
    )

    for agent, path, expected in cases:
        data, name, mime, error = ap._read_path(agent, path)
        assert (data, name, mime, error) == (
            b'%PDF-1.7\nremote', 'report.pdf', '', ''
        )
        assert backend.target == expected


def test_reads_self_path_without_execution_backend(tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_text('agent notes')
    monkeypatch.setattr(ap, 'resolve_self_path', lambda agent_id, value: str(path))

    data, name, mime, error = ap._read_path(
        {'id': 'self-agent'}, '/_self/kb/notes.txt'
    )

    assert (data, name, mime, error) == (b'agent notes', 'notes.txt', '', '')


def test_reads_local_workspace_path(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_text('local notes')

    data, name, mime, error = ap._read_path(
        {
            'id': 'local-agent',
            'session_id': 'local-session',
            'sandbox_enabled': 0,
            'workspace': str(tmp_path),
        },
        'notes.txt',
    )

    assert (data, name, mime, error) == (b'local notes', 'notes.txt', '', '')


def test_rejects_oversized_document_before_model_call(tmp_path, monkeypatch):
    owner = _agent('large_document_agent')
    path = _attachment(tmp_path, owner, body=b'%PDF-1.7\nlarge')
    monkeypatch.setattr(ap, '_MAX_DOCUMENT_BYTES', 4)

    result = ap.execute(
        {'id': owner, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    )

    assert 'exceeds the' in result


def test_tool_schema_exposes_only_path_and_query():
    tool_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'tools',
        'analyze_document.json',
    )
    with open(tool_path, encoding='utf-8') as handle:
        parameters = json.load(handle)['function']['parameters']

    assert set(parameters['properties']) == {'path', 'query'}
    assert parameters['required'] == ['path']


@pytest.mark.parametrize(('name', 'mime'), [
    ('notes.txt', 'text/plain'),
    ('README.md', 'text/markdown'),
    ('script.py', 'text/x-python'),
    ('data.json', 'application/json'),
    ('table.csv', 'text/csv'),
    ('table.tsv', 'text/tab-separated-values'),
    ('ledger.iif', 'application/vnd.shana.informed.interchange'),
])
def test_text_based_files_are_routed_to_read_tools(
    tmp_path, monkeypatch, name, mime
):
    agent_id = _agent('text_route_agent')
    path = _attachment(tmp_path, agent_id, name=name, mime=mime, body=b'exact text')

    def fail_if_models_are_resolved(*args, **kwargs):
        raise AssertionError('text-based files must not call a native document model')

    monkeypatch.setattr(ap, '_resolve_document_models', fail_if_models_are_resolved)
    result = ap.execute(
        {'id': agent_id, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    )

    assert '`read_file`' in result
    assert '`read_attachment`' in result


def test_category_routing_skips_incompatible_primary(tmp_path, monkeypatch):
    agent_id = _agent('category_routing_agent')
    path = _attachment(
        tmp_path, agent_id, name='sheet.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        body=b'xlsx bytes',
    )
    db.set_setting('document_model_id', _model('pdf-only'))
    db.set_setting(
        'document_fallback_model_id',
        _model('spreadsheet-model', category='spreadsheet'),
    )
    calls = []

    class FakeClient:
        timeout = 60

        def __init__(self, model_config):
            calls.append(model_config['id'])

        def chat_completion(self, **kwargs):
            return _success('sheet answer')

    monkeypatch.setattr(ap, 'LLMClient', FakeClient)
    assert ap.execute(
        {'id': agent_id, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    ) == 'sheet answer'
    assert calls == ['spreadsheet-model']


def test_gemini_skips_binary_office_even_when_capability_is_checked(tmp_path):
    agent_id = _agent('gemini_office_agent')
    path = _attachment(
        tmp_path, agent_id, name='report.docx',
        mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        body=b'docx bytes',
    )
    db.set_setting('document_model_id', _model(
        'gemini-office', provider='google-gemini', base_url='', category='office'
    ))
    result = ap.execute(
        {'id': agent_id, 'session_id': 's1', 'document_enabled': 1},
        {'path': path},
    )
    assert 'No native office-document model' in result


def test_google_gemini_provider_is_seeded():
    provider = db.get_provider('google-gemini')
    assert provider['base_url'] == 'https://generativelanguage.googleapis.com/v1beta/openai'
    assert provider['api_format'] == 'openai'
