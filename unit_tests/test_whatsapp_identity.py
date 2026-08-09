"""Tests for resolving the phone identity behind a WhatsApp channel user id.

A LID-addressed DM reaches the session layer as bare LID digits, so the route
table is the only place the sender's real number survives. Route parsing is a
pure function; only the channel lookup needs the DB stubbed out.
"""

import pytest

from backend.channels import whatsapp_identity
from backend.channels.whatsapp_identity import (
    identity_from_route,
    jid_digits,
    jid_namespace,
    resolve_identity,
)


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_jid_namespace_reads_the_server_part():
    assert jid_namespace('6281234567890@s.whatsapp.net') == 's.whatsapp.net'
    assert jid_namespace('123456789012345@lid') == 'lid'
    assert jid_namespace('120363218467385331@g.us') == 'g.us'


def test_jid_namespace_marks_a_bare_identity():
    assert jid_namespace('6281234567890') == 'bare'
    assert jid_namespace('') == 'bare'


def test_jid_digits_drops_the_device_suffix():
    assert jid_digits('6281234567890:12@s.whatsapp.net') == '6281234567890'
    assert jid_digits('6281234567890@s.whatsapp.net') == '6281234567890'
    assert jid_digits('') == ''


# ── Route interpretation ────────────────────────────────────────────────────

def test_lid_route_exposes_the_alternate_phone():
    identity = identity_from_route({
        'primary': '123456789012345@lid',
        'alternate': '6281234567890@s.whatsapp.net',
    })
    assert identity == {
        'user_jid': '123456789012345@lid',
        'user_phone': '6281234567890',
        'user_id_namespace': 'lid',
    }


def test_phone_route_exposes_its_own_number():
    identity = identity_from_route({'primary': '6281234567890@s.whatsapp.net'})
    assert identity['user_phone'] == '6281234567890'
    assert identity['user_id_namespace'] == 's.whatsapp.net'


def test_lid_route_without_alternate_yields_no_phone():
    identity = identity_from_route({'primary': '123456789012345@lid'})
    assert identity['user_phone'] == ''
    assert identity['user_id_namespace'] == 'lid'


def test_group_route_yields_no_phone():
    identity = identity_from_route({'primary': '120363218467385331@g.us'})
    assert identity['user_phone'] == ''
    assert identity['user_id_namespace'] == 'g.us'


def test_missing_route_yields_empty_identity():
    assert identity_from_route(None)['user_phone'] == ''
    assert identity_from_route({})['user_id_namespace'] == ''


# ── Channel lookup ──────────────────────────────────────────────────────────

class _FakeDb:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


@pytest.fixture
def stub_channel(monkeypatch):
    def apply(channel):
        module = type(whatsapp_identity)('models.db')
        module.db = _FakeDb(channel)
        monkeypatch.setitem(__import__('sys').modules, 'models.db', module)
    return apply


def test_resolve_identity_reads_the_persisted_route(stub_channel):
    stub_channel({
        'type': 'whatsapp',
        'config': {'jid_routes': {'123456789012345': {
            'primary': '123456789012345@lid',
            'alternate': '6281234567890@s.whatsapp.net',
        }}},
    })
    assert resolve_identity('wa-1', '123456789012345')['user_phone'] == '6281234567890'


def test_resolve_identity_covers_shared_whatsapp_channels(stub_channel):
    stub_channel({
        'type': 'whatsapp_shared',
        'config': {'jid_routes': {'62811': {'primary': '62811@s.whatsapp.net'}}},
    })
    assert resolve_identity('wa-1', '62811')['user_phone'] == '62811'


def test_resolve_identity_ignores_other_channel_types(stub_channel):
    stub_channel({'type': 'telegram', 'config': {'jid_routes': {'62811': {'primary': '62811@s.whatsapp.net'}}}})
    assert resolve_identity('tg-1', '62811')['user_phone'] == ''


def test_resolve_identity_handles_unknown_sender(stub_channel):
    stub_channel({'type': 'whatsapp', 'config': {'jid_routes': {}}})
    assert resolve_identity('wa-1', '123456789012345')['user_phone'] == ''


def test_resolve_identity_handles_missing_channel(stub_channel):
    stub_channel(None)
    assert resolve_identity('wa-1', '62811') == whatsapp_identity.EMPTY_IDENTITY


def test_resolve_identity_requires_both_arguments():
    assert resolve_identity('', '62811')['user_phone'] == ''
    assert resolve_identity('wa-1', '')['user_phone'] == ''
