import os
from io import BytesIO

from app import app
from backend.agent_runtime import agent_runtime
from backend.agent_runtime.context import build_message_entry
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
                {'filename': 'first.png', 'mime_type': 'image/png', 'size_bytes': 1024, 'file_path': 'data/first.png'},
                {'filename': 'second.png', 'mime_type': 'image/png', 'size_bytes': 2048, 'file_path': 'data/second.png'},
            ],
        },
    }

    content = build_message_entry(msg, {})['content']

    assert '[Attachment #1: first.png (image/png, 1.0 KB)]' in content
    assert '[Attachment #2: second.png (image/png, 2.0 KB)]' in content
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
