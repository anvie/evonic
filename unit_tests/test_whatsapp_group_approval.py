"""Tests for WhatsApp group approval gating on dedicated channels.

Unapproved groups must surface a pending approval request (visible in the
channel modal) instead of being dropped silently. Once the admin approves
the group, every member can chat with the agent via @mention.

Channels are instantiated but never start()ed — no Baileys bridge needed.
"""

import pytest

from backend.channels.whatsapp import WhatsAppChannel
from backend.channels.whatsapp_shared import SharedWhatsAppChannel

GROUP_JID = '120363000000000001@g.us'
GROUP_ID = '120363000000000001'


@pytest.fixture
def wa_channel():
    """A dedicated (restricted) WhatsApp channel with an empty allowlist."""
    from models.db import db
    db.create_agent({'id': 'agent-wa', 'name': 'WA Agent'})
    chan_id = db.create_channel({
        'agent_id': 'agent-wa',
        'type': 'whatsapp',
        'name': 'Dedicated WA',
        'config': {'mode': 'restricted', 'allowed_users': []},
    })
    channel = db.get_channel(chan_id)
    ch = WhatsAppChannel(chan_id, 'agent-wa', channel['config'])
    # No bridge in unit tests — record outbound sends instead of HTTP POSTs.
    sent = []
    ch._do_send = lambda user, text, *a, **kw: sent.append((user, text))
    ch._sent = sent
    return ch


def _gate_group(channel, group_name='Panitia Muktamar', sender='628111'):
    return channel._gate_sender(sender, True, GROUP_JID, 'hello bot',
                                'Budi', {'group_name': group_name})


def test_unapproved_group_creates_pending_approval(wa_channel):
    from models.db import db
    assert _gate_group(wa_channel) is False
    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1
    assert pend[0]['external_user_id'] == GROUP_ID
    assert pend[0]['user_name'] == 'Panitia Muktamar'
    assert pend[0]['pair_code']


def test_group_approval_is_idempotent(wa_channel):
    from models.db import db
    assert _gate_group(wa_channel) is False
    assert _gate_group(wa_channel, sender='628222') is False
    assert _gate_group(wa_channel) is False
    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1


def test_group_name_falls_back_to_group_id(wa_channel):
    from models.db import db
    assert _gate_group(wa_channel, group_name='') is False
    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert pend[0]['user_name'] == GROUP_ID


def test_approved_group_passes_gate(wa_channel):
    from models.db import db
    _gate_group(wa_channel)
    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert db.approve_pending(pend[0]['id']) is True
    # Group is now in the allowlist with its display name recorded.
    assert db.is_user_allowed(wa_channel.channel_id, GROUP_ID) is True
    cfg = db.get_channel(wa_channel.channel_id)['config']
    assert cfg['user_names'].get(GROUP_ID) == 'Panitia Muktamar'
    # Gate passes and no new pending approval is created.
    assert _gate_group(wa_channel) is True
    assert db.get_pending_approvals(wa_channel.channel_id) == []


def test_dm_flow_untouched_by_group_approval(wa_channel):
    """DMs from unapproved users keep the pairing flow — no group record."""
    from models.db import db
    assert wa_channel._gate_sender(
        '628999', False, '628999@s.whatsapp.net', 'hi', 'Andi', {}) is False
    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1
    assert pend[0]['external_user_id'] == '628999'


def test_shared_channel_gate_unaffected():
    """Shared channels use the routing table as allowlist — always True."""
    from models.db import db
    db.create_agent({'id': 'agent-b', 'name': 'Agent B'})
    chan_id = db.create_channel({
        'agent_id': None,
        'type': 'whatsapp_shared',
        'name': 'Shared WA',
        'config': {'mode': 'open', 'routes': {GROUP_ID: 'agent-b'}},
    })
    channel = db.get_channel(chan_id)
    shared = SharedWhatsAppChannel(chan_id, None, channel['config'])
    assert shared._gate_sender('628111', True, GROUP_JID, 'hi',
                               'Budi', {'group_name': 'X'}) is True
    assert db.get_pending_approvals(chan_id) == []
