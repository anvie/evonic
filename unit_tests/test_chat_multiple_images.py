import os
from io import BytesIO

from app import app
from backend.agent_runtime import agent_runtime
from backend.agent_runtime.context import (
    build_message_entry, build_session_attachment_manifest,
    sync_session_attachment_manifest,
)
from backend.agent_runtime.runtime import _append_attachment_context
from models.chatlog import _reconstruct_llm_messages
from routes import agents as agents_route
from routes import sessions as sessions_route


def test_build_message_entry_lists_each_attachment_for_image_tools():
    msg = {
        'role': 'user',
        'content': 'Bandingkan [Image #1] dan [Image #2].',
        'metadata': {
            'image_url': 'data:image/jpeg;base64,first',
            'attachment_infos': [
                {'attachment_id': 11, 'filename': 'first.png', 'mime_type': 'image/png', 'size_bytes': 1024, 'file_path': 'data/first.png'},
                {'attachment_id': 12, 'filename': 'second.png', 'mime_type': 'image/png', 'size_bytes': 2048, 'file_path': 'data/second.png'},
            ],
        },
    }

    content = build_message_entry(msg, {})['content']

    assert '[Attachment #1: first.png (image/png, 1.0 KB)]' in content
    assert '[Attachment #2: second.png (image/png, 2.0 KB)]' in content
    assert 'Attachment ID: 11' in content
    assert 'Attachment ID: 12' in content
    assert content.count('Use the `describe_image` tool') == 2


def test_jsonl_reconstruction_preserves_every_attachment_info():
    infos = [
        {'filename': 'first.png', 'mime_type': 'image/png', 'size_bytes': 1024, 'file_path': 'data/first.png'},
        {'filename': 'second.png', 'mime_type': 'image/png', 'size_bytes': 2048, 'file_path': 'data/second.png'},
    ]
    messages = _reconstruct_llm_messages([{
        'type': 'user', 'content': 'Bandingkan [Image #1] dan [Image #2].',
        'metadata': {'attachment_infos': infos},
    }])

    assert messages == [{
        'role': 'user', 'content': 'Bandingkan [Image #1] dan [Image #2].',
        'attachment_infos': infos,
    }]


def test_jsonl_reconstruction_preserves_legacy_singular_attachment_info():
    info = {'filename': 'legacy.png', 'mime_type': 'image/png', 'size_bytes': 42, 'file_path': 'data/legacy.png'}
    messages = _reconstruct_llm_messages([{
        'type': 'user', 'content': '[Image]', 'metadata': {'attachment_info': info},
    }])

    assert messages == [{'role': 'user', 'content': '[Image]', 'attachment_info': info}]


def test_runtime_attachment_context_lists_all_jsonl_images_with_absolute_paths():
    content = _append_attachment_context(
        'Bandingkan [Image #1] dan [Image #2].',
        [
            {'filename': 'first.png', 'mime_type': 'image/png', 'size_bytes': 1024, 'file_path': 'data/first.png'},
            {'filename': 'second.png', 'mime_type': 'image/png', 'size_bytes': 2048, 'file_path': 'data/second.png'},
        ],
        None, {}, True,
    )

    assert '[Attachment #1: first.png (image/png, 1.0 KB)]' in content
    assert '[Attachment #2: second.png (image/png, 2.0 KB)]' in content
    assert f'File path: {os.path.abspath("data/first.png")}' in content
    assert f'File path: {os.path.abspath("data/second.png")}' in content
    assert content.count('Use the `describe_image` tool') == 2


def test_legacy_singular_attachment_context_is_unchanged():
    content = _append_attachment_context(
        '[Image]', None,
        {'filename': 'legacy.png', 'mime_type': 'image/png', 'size_bytes': 42, 'file_path': 'data/legacy.png'},
        {}, True,
    )

    assert '[Attachment: legacy.png (image/png, 42 B)]' in content
    assert '[Attachment #' not in content
    assert f'File path: {os.path.abspath("data/legacy.png")}' in content
    assert content.count('Use the `describe_image` tool') == 1


def test_invalid_plural_attachment_metadata_falls_back_to_legacy_attachment():
    legacy = {'filename': 'legacy.png', 'mime_type': 'image/png', 'size_bytes': 42, 'file_path': 'data/legacy.png'}
    msg = {
        'role': 'user', 'content': '[Image]',
        'metadata': {'image_url': 'data:image/png;base64,legacy', 'attachment_infos': ['invalid'], 'attachment_info': legacy},
    }

    db_content = build_message_entry(msg, {})['content']
    runtime_content = _append_attachment_context('[Image]', ['invalid'], legacy, {}, True)

    for content in (db_content, runtime_content):
        assert '[Attachment: legacy.png (image/png, 42 B)]' in content
        assert '[Attachment #' not in content
        assert content.count('Use the `describe_image` tool') == 1


def test_session_attachment_manifest_is_metadata_only_and_skips_missing_files(monkeypatch, tmp_path):
    live = tmp_path / 'live.png'
    live.write_bytes(b'image bytes')
    records = [
        {'id': 2, 'filename': 'missing.png', 'mime_type': 'image/png',
         'size_bytes': 99, 'file_path': str(tmp_path / 'missing.png')},
        {'id': 1, 'filename': 'live.png', 'mime_type': 'image/png',
         'size_bytes': 11, 'file_path': str(live), 'payload': 'data:image/png;base64,SECRET'},
    ]
    monkeypatch.setattr('backend.agent_runtime.context.db.list_session_attachments',
                        lambda session_id, agent_id: records)

    manifest = build_session_attachment_manifest('session-1', 'agent-1')

    assert 'id=1' in manifest and str(live) in manifest
    assert 'id=2' not in manifest
    assert 'base64' not in manifest and 'SECRET' not in manifest


def test_manifest_survives_pruned_history_and_deduplicates_visible_ids(monkeypatch, tmp_path):
    first = tmp_path / 'first.png'
    second = tmp_path / 'second.png'
    first.write_bytes(b'1')
    second.write_bytes(b'2')
    records = [
        {'id': 12, 'filename': 'second.png', 'mime_type': 'image/png',
         'size_bytes': 1, 'file_path': str(second)},
        {'id': 11, 'filename': 'first.png', 'mime_type': 'image/png',
         'size_bytes': 1, 'file_path': str(first)},
    ]
    monkeypatch.setattr('backend.agent_runtime.context.db.list_session_attachments',
                        lambda session_id, agent_id: records)
    messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'system', 'content': '## Prior conversation summary\npruned'},
        {'role': 'user', 'content': 'Current\nAttachment ID: 12\nFile path: x'},
    ]

    sync_session_attachment_manifest(messages, 'session-1', 'agent-1')

    manifests = [m['content'] for m in messages
                 if m['role'] == 'system' and m['content'].startswith('## Session Attachments')]
    assert len(manifests) == 1
    assert 'id=11' in manifests[0] and 'id=12' not in manifests[0]
    assert messages[1]['content'] == manifests[0]


def test_api_chat_accepts_repeated_files_and_preserves_image_order(monkeypatch):
    uploads = []
    monkeypatch.setattr(agents_route.db, 'get_agent', lambda agent_id: {'id': agent_id})
    monkeypatch.setattr(agents_route.db, 'get_or_create_session', lambda *args: 'session-1')
    monkeypatch.setattr(agents_route.db, 'get_agent_attachment_config', lambda agent_id: {'enabled': True, 'max_size_mb': 20})

    def process(file, *args):
        uploads.append(file.filename)
        return {
            'image_url': f'data:image/jpeg;base64,{file.filename}',
            'text_prefix': None,
            'attachment_info': {'filename': file.filename, 'mime_type': 'image/png', 'is_image': True, 'attachment_id': len(uploads)},
        }

    captured = {}
    monkeypatch.setattr(sessions_route, '_process_upload', process)
    monkeypatch.setattr(agent_runtime, 'handle_message', lambda *args, **kwargs: captured.update(kwargs) or {
        'response': 'ok', 'tool_trace': [], 'timeline': [],
    })

    with app.test_request_context('/api/agents/a/chat', method='POST', data={
        'message': 'Compare [Image #1] and [Image #2].',
        'user_id': 'web_test',
        'files': [(BytesIO(b'first'), 'first.png'), (BytesIO(b'second'), 'second.png')],
    }):
        response = agents_route.api_chat('a')

    assert response.status_code == 200
    assert uploads == ['first.png', 'second.png']
    assert captured['image_url'].endswith('first.png')
    assert [info['filename'] for info in captured['metadata']['attachment_infos']] == uploads


def test_session_attachment_manifest_is_metadata_only_and_excludes_stale_files(monkeypatch, tmp_path):
    live = tmp_path / 'live.png'
    live.write_bytes(b'png')
    records = [
        {'id': 2, 'filename': 'stale.png', 'mime_type': 'image/png',
         'size_bytes': 99, 'file_path': str(tmp_path / 'missing.png')},
        {'id': 1, 'filename': 'live.png', 'mime_type': 'image/png',
         'size_bytes': 3, 'file_path': str(live), 'binary': 'base64,SECRET'},
    ]
    monkeypatch.setattr('backend.agent_runtime.context.db.list_session_attachments',
                        lambda session_id, agent_id: records)

    manifest = build_session_attachment_manifest('session-1', 'agent-1')

    assert 'id=1' in manifest and str(live) in manifest
    assert 'stale.png' not in manifest
    assert 'base64' not in manifest and 'SECRET' not in manifest


def test_session_attachment_manifest_persists_outside_pruned_history(monkeypatch, tmp_path):
    live = tmp_path / 'persist.txt'
    live.write_text('payload')
    monkeypatch.setattr('backend.agent_runtime.context.db.list_session_attachments',
                        lambda session_id, agent_id: [{
                            'id': 7, 'filename': 'persist.txt', 'mime_type': 'text/plain',
                            'size_bytes': 7, 'file_path': str(live),
                        }])
    messages = [{'role': 'system', 'content': 'system'},
                {'role': 'system', 'content': '## Prior conversation summary\npruned'},
                {'role': 'user', 'content': 'continue'}]

    sync_session_attachment_manifest(messages, 'session-1', 'agent-1')
    sync_session_attachment_manifest(messages, 'session-1', 'agent-1')

    manifests = [m for m in messages if str(m.get('content')).startswith('## Session Attachments')]
    assert len(manifests) == 1
    assert 'id=7' in manifests[0]['content'] and str(live) in manifests[0]['content']
