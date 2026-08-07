"""Regression tests for inbound WhatsApp document delivery."""

import base64
from pathlib import Path

from backend.channels.whatsapp import (
    WhatsAppChannel,
    _decode_document_payload,
    _sanitize_attachment_filename,
)


_PDF_BYTES = b'%PDF-1.7\nEvonic test document\n'


def _document_payload(**overrides):
    document = {
        'base64': base64.b64encode(_PDF_BYTES).decode('ascii'),
        'mimetype': 'application/pdf',
        'filename': 'meeting-notes.pdf',
        'file_length': len(_PDF_BYTES),
    }
    document.update(overrides)
    return document


def _channel(monkeypatch):
    from models.db import db

    db.create_agent({
        'id': 'agent-doc',
        'name': 'Document Agent',
    })
    db.update_agent('agent-doc', {
        'attachments_enabled': True,
        'attachment_max_size_mb': 20,
    })
    channel_id = db.create_channel({
        'agent_id': 'agent-doc',
        'type': 'whatsapp',
        'name': 'WhatsApp Documents',
        'config': {'mode': 'open'},
    })
    channel = WhatsAppChannel(channel_id, 'agent-doc', {'mode': 'open'})
    monkeypatch.setattr(channel, '_remember_jid_route',
                        lambda user_id, primary, alternate='': channel._jid_map.update({user_id: primary}))
    return channel


def _capture_document_callback(monkeypatch, tmp_path, *, text='', document=None,
                               sender='lid-user', jid='lid-user@lid',
                               alt_sender='628111', alt_jid='628111@s.whatsapp.net',
                               download_failed=False):
    from backend.agent_runtime import agent_runtime
    from models.db import db

    channel = _channel(monkeypatch)
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(channel, '_resolve_agent', lambda *args, **kwargs: 'agent-doc')
    monkeypatch.setattr(channel, '_gate_sender', lambda *args, **kwargs: True)
    monkeypatch.setattr(agent_runtime, 'handle_message',
                        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs) or {'buffered': True})

    channel.handle_callback({
        'from': sender,
        'jid': jid,
        'alt_sender': alt_sender,
        'alt_jid': alt_jid,
        'message_id': 'wa-document-1',
        'content_type': 'documentMessage',
        'payload_keys': ['documentMessage'],
        'text': text,
        'document': document,
        'document_download_failed': download_failed,
    })
    captured['channel'] = channel
    captured['attachments'] = db.list_session_attachments(
        db.get_or_create_session('agent-doc', sender, channel.channel_id), 'agent-doc')
    return captured


def test_document_payload_decoding_rejects_malformed_or_mismatched_data():
    assert _decode_document_payload({'base64': 'not-valid-base64!'}) is None
    assert _decode_document_payload(_document_payload(file_length=999)) is None
    assert _decode_document_payload({'base64': ''}) is None


def test_document_filename_is_path_safe_and_bounded():
    assert _sanitize_attachment_filename('../../private/report name.pdf') == 'report_name.pdf'
    assert _sanitize_attachment_filename(r'..\\..\\report.pdf') == 'report.pdf'
    assert len(_sanitize_attachment_filename('a' * 200 + '.pdf')) == 120


def test_captionless_pdf_is_persisted_and_delivered_to_lid_routed_agent(monkeypatch, tmp_path):
    captured = _capture_document_callback(
        monkeypatch, tmp_path, document=_document_payload())

    assert captured['args'][0:4] == (
        'agent-doc', 'lid-user', captured['args'][2], captured['channel'].channel_id)
    assert captured['args'][2].startswith('[Document]\n[Attached: meeting-notes.pdf ')
    assert 'id=' in captured['args'][2]
    metadata = captured['kwargs']['metadata']
    assert metadata['channel_message_id'] == 'wa-document-1'
    attachment = metadata['attachment_info']
    assert attachment['original_filename'] == 'meeting-notes.pdf'
    assert attachment['mime_type'] == 'application/pdf'
    assert attachment['size_bytes'] == len(_PDF_BYTES)
    assert Path(attachment['file_path']).read_bytes() == _PDF_BYTES
    assert captured['channel']._jid_map['lid-user'] == 'lid-user@lid'
    assert captured['attachments'][0]['file_type'] == 'document'


def test_captioned_pdf_preserves_caption_and_attachment_marker(monkeypatch, tmp_path):
    captured = _capture_document_callback(
        monkeypatch, tmp_path, text='Please review this file',
        document=_document_payload(filename='../unsafe report.pdf'))

    delivered = captured['args'][2]
    assert delivered.startswith('Please review this file\n[Attached: unsafe_report.pdf ')
    assert captured['kwargs']['metadata']['attachment_info']['original_filename'] == 'unsafe_report.pdf'


def test_document_download_failure_is_visible_without_fake_attachment(monkeypatch, tmp_path):
    captured = _capture_document_callback(
        monkeypatch, tmp_path, document=None, download_failed=True)

    assert captured['args'][2] == '[Document download failed]'
    assert 'attachment_info' not in captured['kwargs']['metadata']
    assert captured['attachments'] == []


def test_malformed_document_without_caption_remains_dropped(monkeypatch, tmp_path):
    captured = _capture_document_callback(
        monkeypatch, tmp_path, document={'base64': 'invalid!'})

    assert 'args' not in captured
    assert captured['attachments'] == []
