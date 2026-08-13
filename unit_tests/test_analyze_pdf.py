"""Native PDF tool validation and provider payload contracts."""

import os

from backend.llm_client import _convert_multimodal_to_claude
from backend.tools import analyze_pdf as ap
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
    path = tmp_path / name
    path.write_bytes(body)
    attachment_id = db.save_attachment(
        agent_id=agent_id,
        session_id=session_id,
        filename=os.path.basename(path),
        file_path=str(path),
        original_filename=name,
        mime_type=mime,
        file_type='document',
        size_bytes=len(body),
    )
    return attachment_id


def _model(model_id, *, provider='openrouter', api_format='openai',
           base_url='https://api.openai.com/v1', api_key='test-key'):
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
        'pdf_supported': 1,
    })
    return model_id


def _success(text='PDF answer'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': text}}]},
    }


def test_openai_receives_native_file_block(tmp_path, monkeypatch):
    agent_id = _agent()
    attachment_id = _attachment(tmp_path, agent_id)
    db.set_setting('pdf_model_id', _model('openai-pdf'))
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
        {'id': agent_id, 'session_id': 's1', 'pdf_enabled': 1},
        {'attachment_id': attachment_id, 'query': 'What is this?'},
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


def test_gemini_uses_direct_generate_content_with_inline_pdf(tmp_path, monkeypatch):
    agent_id = _agent('gemini_pdf_agent')
    attachment_id = _attachment(tmp_path, agent_id)
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
    db.set_setting('pdf_model_id', model_id)
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
        {'id': agent_id, 'session_id': 's1', 'pdf_enabled': 1},
        {'attachment_id': attachment_id},
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
    attachment_id = _attachment(tmp_path, agent_id)
    db.set_setting('pdf_model_id', _model('pdf-primary'))
    db.set_setting('pdf_fallback_model_id', _model('pdf-fallback'))
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
        {'id': agent_id, 'session_id': 's1', 'pdf_enabled': 1},
        {'attachment_id': attachment_id},
    )

    assert result == 'fallback answer'
    assert calls == ['pdf-primary', 'pdf-fallback']


def test_rejects_disabled_cross_session_non_pdf_and_bad_signature(tmp_path):
    owner = _agent('pdf_validation_agent')
    valid_id = _attachment(tmp_path, owner, session_id='owner-session')

    assert 'pdf_enabled=0' in ap.execute(
        {'id': owner, 'pdf_enabled': 0}, {'attachment_id': valid_id}
    )
    assert 'different session' in ap.execute(
        {'id': owner, 'session_id': 'other-session', 'pdf_enabled': 1},
        {'attachment_id': valid_id},
    )

    text_id = _attachment(
        tmp_path, owner, name='notes.txt', mime='text/plain', body=b'plain text'
    )
    assert 'not a PDF' in ap.execute(
        {'id': owner, 'session_id': 's1', 'pdf_enabled': 1},
        {'attachment_id': text_id},
    )

    fake_id = _attachment(
        tmp_path, owner, name='fake.pdf', body=b'not actually a PDF'
    )
    assert 'valid PDF signature' in ap.execute(
        {'id': owner, 'session_id': 's1', 'pdf_enabled': 1},
        {'attachment_id': fake_id},
    )

    assert 'positive integer' in ap.execute(
        {'id': owner, 'pdf_enabled': 1}, {'attachment_id': 1.5}
    )


def test_rejects_cross_agent_attachment(tmp_path):
    owner = _agent('pdf_owner')
    other = _agent('pdf_other')
    attachment_id = _attachment(tmp_path, owner)

    result = ap.execute(
        {'id': other, 'session_id': 's1', 'pdf_enabled': 1},
        {'attachment_id': attachment_id},
    )

    assert 'different agent' in result


def test_google_gemini_provider_is_seeded():
    provider = db.get_provider('google-gemini')
    assert provider['base_url'] == 'https://generativelanguage.googleapis.com/v1beta/openai'
    assert provider['api_format'] == 'openai'
