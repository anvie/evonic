"""Regression tests: inbound WhatsApp images and voice notes must reach the agent."""

import base64
import io

import pytest

from backend.channels.whatsapp import WhatsAppChannel, _format_attachment_marker


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(buf, 'PNG')
    return buf.getvalue()


def _capture(monkeypatch, tmp_path, **media):
    from backend.agent_runtime import agent_runtime
    from models.db import db

    db.create_agent({'id': 'agent-media', 'name': 'Media Agent'})
    db.update_agent('agent-media', {'attachments_enabled': True})
    channel_id = db.create_channel({
        'agent_id': 'agent-media', 'type': 'whatsapp',
        'name': 'WhatsApp Media', 'config': {'mode': 'open'},
    })
    channel = WhatsAppChannel(channel_id, 'agent-media', {'mode': 'open'})
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(channel, '_resolve_agent', lambda *a, **k: 'agent-media')
    monkeypatch.setattr(channel, '_gate_sender', lambda *a, **k: True)
    monkeypatch.setattr(agent_runtime, 'handle_message',
                        lambda *a, **k: captured.update(args=a, kwargs=k) or {'buffered': True})
    text = media.pop('text', '')
    channel.handle_callback({
        'from': 'user-1', 'jid': 'user-1@lid', 'message_id': 'wa-media-1',
        'text': text, 'is_group': False, **media,
    })
    return captured


@pytest.mark.parametrize('text', ['', 'please look at this picture'])
def test_inbound_image_reaches_agent_with_attachment_marker(monkeypatch, tmp_path, text):
    image = {'base64': base64.b64encode(_png()).decode(), 'mimetype': 'image/png'}
    captured = _capture(monkeypatch, tmp_path, text=text, image=image)

    assert captured, 'handle_message was never called'
    message = captured['args'][2]
    assert message.startswith(text or '[Image]')
    assert '[Attached: ' in message and 'image/png' in message


def test_inbound_audio_reaches_agent_with_attachment_marker(monkeypatch, tmp_path):
    audio = {'base64': base64.b64encode(b'OggS fake audio').decode(), 'mimetype': 'audio/ogg'}
    captured = _capture(monkeypatch, tmp_path, audio=audio)

    assert captured, 'handle_message was never called'
    assert '[Attached: ' in captured['args'][2]


def test_marker_falls_back_to_filename():
    marker = _format_attachment_marker({
        'attachment_id': 7, 'filename': 'pic.png', 'mime_type': 'image/png',
        'size_bytes': 10, 'file_path': 'data/pic.png',
    })
    assert '[Attached: pic.png ' in marker
