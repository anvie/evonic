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
