"""Tests for the Shared WhatsApp channel — one number, multiple agents.

Routing is pure config/DB lookup, so everything is testable without a
running Baileys bridge: channels are instantiated but never start()ed.
"""

import pytest

from backend.channels.whatsapp import WhatsAppChannel
from backend.channels.whatsapp_shared import SharedWhatsAppChannel, _lookup_route


# ── Pure helper ─────────────────────────────────────────────────────────────

def test_lookup_route_sender_hit():
    routes = {'628111': 'agent-a', '628222': 'agent-b'}
    assert _lookup_route(routes, '628111') == 'agent-a'


def test_lookup_route_alt_sender_fallback():
    routes = {'628111': 'agent-a'}
    assert _lookup_route(routes, '999888777', '628111') == 'agent-a'


def test_lookup_route_miss():
    routes = {'628111': 'agent-a'}
    assert _lookup_route(routes, '620000', '621111') is None


def test_lookup_route_empty():
    assert _lookup_route({}, '628111') is None
    assert _lookup_route(None, '628111') is None


# ── Registration ────────────────────────────────────────────────────────────

def test_get_channel_type():
    assert SharedWhatsAppChannel.get_channel_type() == 'whatsapp_shared'


def test_registered_in_channel_types():
    from backend.channels.registry import CHANNEL_TYPES
    assert CHANNEL_TYPES.get('whatsapp_shared') is SharedWhatsAppChannel


# ── Agent resolution ────────────────────────────────────────────────────────

@pytest.fixture
def shared_channel():
    """An agentless shared channel row in the test DB with two agents and routes."""
    from models.db import db
    db.create_agent({'id': 'agent-a', 'name': 'Agent A'})
    db.create_agent({'id': 'agent-b', 'name': 'Agent B'})
    db.create_agent({'id': 'agent-off', 'name': 'Disabled Agent', 'enabled': False})
    routes = {
        '628111': 'agent-a',
        '628222': 'agent-b',
        '628333': 'agent-off',       # disabled agent
        '628444': 'agent-ghost',     # nonexistent agent
        '120363000000000001': 'agent-b',  # group route
    }
    chan_id = db.create_channel({
        'agent_id': None,
        'type': 'whatsapp_shared',
        'name': 'Shared WA',
        'config': {'mode': 'open', 'routes': routes},
    })
    channel = db.get_channel(chan_id)
    return SharedWhatsAppChannel(chan_id, None, channel['config'])


def test_resolve_mapped_dm(shared_channel):
    assert shared_channel._resolve_agent(
        '628111', False, '628111@s.whatsapp.net') == 'agent-a'
    assert shared_channel._resolve_agent(
        '628222', False, '628222@s.whatsapp.net') == 'agent-b'


def test_resolve_dm_via_alt_sender(shared_channel):
    # LID-addressed chat: sender digits are LID, alt_sender carries the phone
    assert shared_channel._resolve_agent(
        '99988877766', False, '99988877766@lid',
        alt_sender='628111') == 'agent-a'


def test_resolve_unmapped_dm_returns_none_and_captures_inbox(shared_channel):
    from models.db import db
    payload = {'pushName': 'Budi', 'text': 'hello, I need help with my order'}
    assert shared_channel._resolve_agent(
        '620000', False, '620000@s.whatsapp.net',
        alt_sender='621111', payload=payload) is None
    entries = db.get_inbox(shared_channel.channel_id)
    assert len(entries) == 1
    e = entries[0]
    assert e['external_user_id'] == '620000'
    assert e['alt_user_id'] == '621111'
    assert e['push_name'] == 'Budi'
    assert e['last_message'] == 'hello, I need help with my order'
    assert not e['is_group']


def test_resolve_mapped_dm_does_not_capture_inbox(shared_channel):
    from models.db import db
    shared_channel._resolve_agent(
        '628111', False, '628111@s.whatsapp.net', payload={'pushName': 'X'})
    assert db.get_inbox(shared_channel.channel_id) == []


def test_repeat_unmapped_messages_upsert_single_inbox_row(shared_channel):
    from models.db import db
    for i in range(3):
        shared_channel._resolve_agent(
            '620000', False, '620000@s.whatsapp.net',
            payload={'pushName': 'Budi', 'text': f'msg {i}'})
    entries = db.get_inbox(shared_channel.channel_id)
    assert len(entries) == 1
    assert entries[0]['message_count'] == 3
    assert entries[0]['last_message'] == 'msg 2'


def test_resolve_mapped_group_ignores_sender_route(shared_channel):
    # Sender 628111 is routed to agent-a for DMs, but the group route wins
    assert shared_channel._resolve_agent(
        '628111', True, '120363000000000001@g.us') == 'agent-b'


def test_resolve_unmapped_group_returns_none_and_captures_group(shared_channel):
    from models.db import db
    # Even when the sender has a DM route, unmapped groups are dropped
    payload = {'pushName': 'Budi', 'group_name': 'Family Chat', 'text': 'hi bot'}
    assert shared_channel._resolve_agent(
        '628111', True, '120369999999999999@g.us', payload=payload) is None
    entries = db.get_inbox(shared_channel.channel_id)
    assert len(entries) == 1
    e = entries[0]
    assert e['external_user_id'] == '120369999999999999'  # group ID, not sender
    assert e['is_group']
    assert e['group_name'] == 'Family Chat'


def test_resolve_disabled_agent_returns_none(shared_channel):
    assert shared_channel._resolve_agent(
        '628333', False, '628333@s.whatsapp.net') is None


def test_resolve_missing_agent_returns_none(shared_channel):
    assert shared_channel._resolve_agent(
        '628444', False, '628444@s.whatsapp.net') is None


def test_resolve_reads_routes_fresh_from_db(shared_channel):
    """Admin route edits apply without a channel restart."""
    from models.db import db
    config = db.get_channel(shared_channel.channel_id)['config']
    config['routes']['628555'] = 'agent-b'
    db.update_channel(shared_channel.channel_id, {'config': config})
    assert shared_channel._resolve_agent(
        '628555', False, '628555@s.whatsapp.net') == 'agent-b'


def test_unrestricted_dm_fallback_uses_default_agent_without_inbox(shared_channel):
    from models.db import db
    config = db.get_channel(shared_channel.channel_id)['config']
    config.update({'access_mode': 'unrestricted', 'default_agent_id': 'agent-a'})
    db.update_channel(shared_channel.channel_id, {'config': config})
    assert shared_channel._resolve_agent(
        '620000', False, '620000@s.whatsapp.net', payload={'text': 'hello'}) == 'agent-a'
    assert db.get_inbox(shared_channel.channel_id) == []


def test_unrestricted_explicit_route_and_lid_route_keep_precedence(shared_channel):
    from models.db import db
    config = db.get_channel(shared_channel.channel_id)['config']
    config.update({'access_mode': 'unrestricted', 'default_agent_id': 'agent-b'})
    config['routes']['628777'] = 'agent-a'
    config['routes']['628888'] = 'agent-a'
    db.update_channel(shared_channel.channel_id, {'config': config})
    assert shared_channel._resolve_agent(
        '628777', False, '628777@s.whatsapp.net') == 'agent-a'
    assert shared_channel._resolve_agent(
        '999888', False, '999888@lid', alt_sender='628888') == 'agent-a'


def test_unrestricted_missing_or_disabled_default_fails_closed(shared_channel):
    from models.db import db
    config = db.get_channel(shared_channel.channel_id)['config']
    config.update({'access_mode': 'unrestricted', 'default_agent_id': 'agent-off'})
    db.update_channel(shared_channel.channel_id, {'config': config})
    assert shared_channel._resolve_agent(
        '620000', False, '620000@s.whatsapp.net') is None
    config['default_agent_id'] = 'agent-ghost'
    db.update_channel(shared_channel.channel_id, {'config': config})
    assert shared_channel._resolve_agent(
        '620001', False, '620001@s.whatsapp.net') is None


def test_unrestricted_group_still_requires_explicit_group_route(shared_channel):
    from models.db import db
    config = db.get_channel(shared_channel.channel_id)['config']
    config.update({'access_mode': 'unrestricted', 'default_agent_id': 'agent-a'})
    db.update_channel(shared_channel.channel_id, {'config': config})
    assert shared_channel._resolve_agent(
        '628111', True, '120369999999999999@g.us') is None


# ── Gate + base behavior ────────────────────────────────────────────────────

def test_shared_gate_sender_always_true(shared_channel):
    # The routing table is the allowlist — no pairing flow for shared channels
    assert shared_channel._gate_sender(
        '620000', False, '620000@s.whatsapp.net', 'hi', 'Budi', {}) is True


def test_base_resolve_agent_returns_bound_agent():
    channel = WhatsAppChannel('chan-1', 'agent-x', {'mode': 'open'})
    assert channel._resolve_agent(
        '628111', False, '628111@s.whatsapp.net') == 'agent-x'
    assert channel._resolve_agent(
        '628111', True, '12036000@g.us', alt_sender='628999') == 'agent-x'


# ── Inbound callback ──────────────────────────────────────────────────────────

_IMAGE = {'base64': 'aW1hZ2UtYnl0ZXM=', 'mimetype': 'image/jpeg'}


def _capture_callback(monkeypatch, shared_channel, payload):
    from backend.agent_runtime import agent_runtime
    from models.db import db

    db.update_agent('agent-a', {'vision_enabled': False})
    captured = {'saved': []}

    def save_attachment(session_id, sender, image_bytes, mime_type, agent_id=None):
        captured['saved'].append((session_id, sender, image_bytes, mime_type, agent_id))
        return {'attachment_id': 42, 'filename': 'image.jpg', 'is_image': True}

    monkeypatch.setattr(shared_channel, '_save_image_attachment', save_attachment)
    monkeypatch.setattr(agent_runtime, 'handle_message',
                        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs) or {'buffered': True})
    shared_channel.handle_callback({
        'from': '628111',
        'jid': '628111@s.whatsapp.net',
        'message_id': 'msg-1',
        **payload,
    })
    return captured


def test_captionless_image_reaches_routed_vision_disabled_agent(monkeypatch, shared_channel):
    captured = _capture_callback(monkeypatch, shared_channel, {'text': '', 'image': _IMAGE})

    assert captured['args'][0:4] == ('agent-a', '628111', '[Image]', shared_channel.channel_id)
    assert captured['kwargs']['image_url'] is None
    assert captured['kwargs']['metadata']['attachment_info']['attachment_id'] == 42
    assert captured['saved'][0][1:] == ('628111', b'image-bytes', 'image/jpeg', 'agent-a')


def test_captioned_image_preserves_caption(monkeypatch, shared_channel):
    captured = _capture_callback(
        monkeypatch, shared_channel, {'text': 'Please inspect this receipt', 'image': _IMAGE})

    assert captured['args'][2] == 'Please inspect this receipt'
    assert captured['kwargs']['metadata']['attachment_info']['attachment_id'] == 42


def test_empty_payload_remains_dropped(monkeypatch, shared_channel):
    captured = _capture_callback(monkeypatch, shared_channel, {'text': ''})

    assert 'args' not in captured
    assert captured['saved'] == []
