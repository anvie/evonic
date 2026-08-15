"""Regression coverage for authoritative attachment IDs in LLM messages."""

import io
import os

from werkzeug.datastructures import FileStorage

from backend.agent_runtime import context
from backend.tools import read_attachment
from models.db import db
from routes.sessions import _process_upload


def _upload_attachment(agent_id: str, session_id: str = 'aisyah-75433064'):
    body = b'authoritative attachment content\n'
    upload = FileStorage(
        stream=io.BytesIO(body),
        filename='note.txt',
        content_type='text/plain',
    )
    result = _process_upload(
        upload,
        agent_id,
        session_id,
        external_user_id='user-1',
        channel_id='channel-1',
    )
    info = result['attachment_info']
    return info['attachment_id'], info['file_path'], body


def _attachment_info(attachment_id: int, path, size_bytes: int):
    return {
        'attachment_id': attachment_id,
        'filename': 'note.txt',
        'mime_type': 'text/plain',
        'size_bytes': size_bytes,
        'file_path': str(path),
    }


def test_uploaded_attachment_id_reaches_model_message_and_resolves(tmp_path, monkeypatch):
    """The DB-generated ID must be visible in the exact message sent to an LLM."""
    monkeypatch.chdir(tmp_path)
    agent_id = 'attachment_prompt_agent'
    db.create_agent({'id': agent_id, 'name': agent_id, 'system_prompt': ''})
    attachment_id, path, body = _upload_attachment(agent_id)

    model_request = {'messages': [{'role': 'user', 'content': '[Attached file: note.txt]'}]}
    context.append_attachment_note(
        model_request['messages'][0],
        _attachment_info(attachment_id, path, len(body)),
    )
    model_message = model_request['messages'][0]

    assert attachment_id > 0
    assert f'Attachment ID: {attachment_id}' in model_message['content']
    assert 'aisyah-75433064' in model_message['content']
    assert 'Attachment ID: 75433064' not in model_message['content']

    result = read_attachment.execute(
        {'id': agent_id}, {'attachment_id': attachment_id}
    )
    assert '1: authoritative attachment content' in result['result']


def test_persisted_message_metadata_repairs_legacy_attachment_text(tmp_path):
    """SQLite-restored messages expose the ID even when old text omitted it."""
    attachment_info = _attachment_info(
        184,
        tmp_path / 'data' / 'attachments' / 'aisyah' / 'aisyah-75433064' / 'note.txt',
        2048,
    )
    persisted_message = {
        'role': 'user',
        'content': '[Attached file: note.txt]',
        'metadata': {'attachment_info': attachment_info},
    }

    model_message = context.build_message_entry(
        persisted_message,
        {'id': 'aisyah', 'audio_enabled': False},
    )

    assert model_message['content'].startswith('[Attached file: note.txt]')
    assert 'Attachment ID: 184' in model_message['content']
    assert 'File path:' in model_message['content']


def test_attachment_note_keeps_media_tool_guidance(tmp_path):
    info = {
        'attachment_id': 186,
        'filename': 'voice.ogg',
        'mime_type': 'audio/ogg',
        'size_bytes': 512,
        'file_path': os.path.join('data', 'attachments', 'agent', 'session', 'voice.ogg'),
    }

    note = context.build_attachment_note(info, audio_enabled=True)

    assert 'Attachment ID: 186' in note
    assert 'transcribe_audio' in note


def test_document_uploads_stay_metadata_only_and_get_native_tool_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent_id = 'pdf_upload_agent'
    db.create_agent({'id': agent_id, 'name': agent_id, 'system_prompt': ''})
    upload = FileStorage(
        stream=io.BytesIO(b'%PDF-1.7\nTOP SECRET BODY MUST NOT ENTER PROMPT'),
        filename='report.pdf',
        content_type='application/pdf',
    )

    result = _process_upload(upload, agent_id, 'pdf-session', 'user-1', 'channel-1')

    assert result['text_prefix'] is None
    note = context.build_attachment_note(result['attachment_info'], document_enabled=True)
    assert 'TOP SECRET BODY' not in note
    assert 'analyze_document' in note
    assert f"Attachment ID: {result['attachment_info']['attachment_id']}" in note
    assert 'with path `' in note
    assert 'analyze_document' not in context.build_attachment_note(
        result['attachment_info'], document_enabled=False
    )
    without_id = dict(result['attachment_info'])
    without_id.pop('attachment_id')
    assert 'analyze_document' in context.build_attachment_note(without_id)
    without_path = dict(result['attachment_info'])
    without_path.pop('file_path')
    assert 'analyze_document' not in context.build_attachment_note(without_path)

    text_upload = FileStorage(
        stream=io.BytesIO(b'TEXT BODY MUST NOT ENTER PROMPT'),
        filename='notes.txt',
        content_type='text/plain',
    )
    text_result = _process_upload(
        text_upload, agent_id, 'text-session', 'user-1', 'channel-1'
    )
    assert text_result['text_prefix'] is None
    text_note = context.build_attachment_note(text_result['attachment_info'])
    assert 'TEXT BODY' not in text_note
    assert 'read_attachment' in text_note
    assert 'attachment_id=' in text_note
    assert 'analyze_document' not in text_note
    assert 'read_attachment' in context.build_attachment_note(
        text_result['attachment_info'], document_enabled=False
    )
