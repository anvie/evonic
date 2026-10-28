"""Regression tests: mid-loop injected user messages must keep attachment notes."""

import queue
from types import SimpleNamespace

from backend.agent_runtime import runtime as rt

IMAGE_INFO = {
    'attachment_id': 4344,
    'filename': 'IMG_0142.jpg',
    'mime_type': 'image/jpeg',
    'size_bytes': 292 * 1024,
    'file_path': '/srv/evonic/data/attachments/a/s/IMG_0142.jpg',
}


def _no_tools(monkeypatch):
    monkeypatch.setattr(rt.db, 'get_agent_tools', lambda agent_id: [])


def test_injected_content_includes_image_attachment_note(monkeypatch):
    _no_tools(monkeypatch)
    content = rt._injected_user_content(
        '[Image #1] ini contohnya', {'attachment_infos': [IMAGE_INFO]}, {}, 'agent')

    assert content.startswith('[Image #1] ini contohnya')
    assert 'Attachment ID: 4344' in content
    assert f"File path: {IMAGE_INFO['file_path']}" in content
    assert 'describe_image' in content


def test_injected_content_uses_legacy_singular_attachment_info(monkeypatch):
    _no_tools(monkeypatch)
    content = rt._injected_user_content(
        '[Image]', {'attachment_info': IMAGE_INFO}, {}, 'agent')

    assert 'Attachment ID: 4344' in content


def test_injected_content_omits_describe_hint_without_vision(monkeypatch):
    _no_tools(monkeypatch)
    content = rt._injected_user_content(
        '[Image]', {'attachment_infos': [IMAGE_INFO]}, {'vision_enabled': 0}, 'agent')

    assert 'Attachment ID: 4344' in content
    assert 'describe_image' not in content


def test_injected_content_without_attachments_is_unchanged(monkeypatch):
    _no_tools(monkeypatch)
    assert rt._injected_user_content('halo', {}, {}, 'agent') == 'halo'


def test_send_as_user_injection_keeps_attachment_note(monkeypatch):
    _no_tools(monkeypatch)
    monkeypatch.setattr(rt.db, 'get_session_with_details', lambda sid: {
        'agent_id': 'agent', 'external_user_id': 'user', 'channel_id': None})
    monkeypatch.setattr(rt.db, 'add_chat_message', lambda *a, **k: 1)
    monkeypatch.setattr(rt.db, 'get_agent', lambda agent_id: {'name': 'Agent'})
    monkeypatch.setattr(rt.chatlog_manager, 'get',
                        lambda *a: SimpleNamespace(append=lambda entry: None))
    monkeypatch.setattr(rt.event_stream, 'emit', lambda *a, **k: None)

    inject_queue = queue.Queue()
    runtime = rt.AgentRuntime.__new__(rt.AgentRuntime)
    runtime._prefetcher = SimpleNamespace(invalidate=lambda sid: None)
    runtime._is_busy = lambda sid: True
    runtime._get_inject_queue = lambda sid: inject_queue

    assert runtime.send_as_user(
        'sess', '[Image #1] lihat ini', metadata={'attachment_infos': [IMAGE_INFO]}) is True

    item = inject_queue.get_nowait()
    assert item['role'] == 'user'
    assert 'Attachment ID: 4344' in item['content']
    assert 'describe_image' in item['content']
