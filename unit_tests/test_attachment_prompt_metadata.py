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


def test_attachment_note_prefers_workplace_path_over_host_path():
    """Sandboxed agents must see a path their container can actually open."""
    info = {
        'attachment_id': 187,
        'filename': 'probe.tar.gz',
        'mime_type': 'application/gzip',
        'size_bytes': 3145728,
        'file_path': os.path.join('data', 'attachments', 'asep', 'asep-030df24d', 'probe.tar.gz'),
        'workplace_path': '/workspace/evonic-asep-scratchpad/attachments/asep-030df24d/dead_beef_probe.tar.gz',
    }

    note = context.build_attachment_note(info)

    assert 'File path: /workspace/evonic-asep-scratchpad/attachments/asep-030df24d/' in note
    assert 'data/attachments' not in note


class _FakeBackend:
    """Identity-resolving backend that records staged writes."""

    def __init__(self):
        self.writes = []
        self._sizes = {}

    def resolve_path(self, path):
        return path

    def write_file_bytes(self, path, data, create_dirs=True):
        self.writes.append(path)
        self._sizes[path] = len(data)
        return {'ok': True}

    def file_stat(self, path):
        return {'exists': path in self._sizes, 'size': self._sizes.get(path, -1)}


def test_upload_stages_attachment_under_session_scoped_workplace_path(tmp_path, monkeypatch):
    """Upload-time staging must use the real session id, not the 'default' bucket."""
    monkeypatch.chdir(tmp_path)
    agent_id = 'workplace_upload_agent'
    session_id = f'{agent_id}-abc123'
    db.create_agent({'id': agent_id, 'name': agent_id, 'system_prompt': ''})

    backend = _FakeBackend()
    monkeypatch.setattr(
        'backend.tools.lib.exec_backend.registry.get_backend',
        lambda sid, agent: backend,
    )
    # The DB row has no session_id — the upload path must supply it.
    monkeypatch.setattr(
        'routes.sessions.db.get_agent',
        lambda aid: {'id': aid, 'sandbox_enabled': True},
    )
    upload = FileStorage(
        stream=io.BytesIO(b'payload\n'),
        filename='note.txt',
        content_type='text/plain',
    )

    result = _process_upload(
        upload, agent_id, session_id,
        external_user_id='user-1', channel_id='channel-1',
    )

    info = result['attachment_info']
    assert info['workplace_path'] == backend.writes[0]
    assert info['workplace_path'].startswith(
        f'/tmp/evonic-{agent_id}-scratchpad/attachments/{session_id}/')

