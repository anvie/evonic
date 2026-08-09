"""Resolve the phone-number identity behind a WhatsApp channel user id.

A LID-addressed direct message reaches the session layer as bare LID digits:
`WhatsAppChannel.handle_callback` strips the namespace before `sender` becomes
the session `external_user_id`, and bare LID digits cannot be told apart from a
phone number by shape alone. The per-channel reply-route table remembers both
identities of every sender, so it is the only reliable source for the number a
tool can safely record as the user's WhatsApp contact.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

_logger = logging.getLogger(__name__)

# Namespaces whose local part is a real phone number.
PHONE_SERVERS = ('s.whatsapp.net', 'whatsapp.net', 'c.us')

EMPTY_IDENTITY: Dict[str, str] = {'user_jid': '', 'user_phone': '', 'user_id_namespace': ''}


def jid_namespace(jid: str) -> str:
    """Return the server part of a JID, or 'bare' when it carries none."""
    if not jid or '@' not in jid:
        return 'bare'
    return jid.rsplit('@', 1)[-1].lower()


def jid_digits(jid: str) -> str:
    """Return the digits of a JID's local part, dropping any device suffix."""
    local = jid.split('@', 1)[0].split(':', 1)[0] if jid else ''
    return ''.join(char for char in local if char.isdigit())


def identity_from_route(route: Optional[dict]) -> Dict[str, str]:
    """Derive the caller-facing identity fields from one persisted JID route."""
    if not isinstance(route, dict):
        return dict(EMPTY_IDENTITY)
    primary = str(route.get('primary') or '')
    alternate = str(route.get('alternate') or '')
    phone_jid = next((jid for jid in (primary, alternate) if jid_namespace(jid) in PHONE_SERVERS), '')
    return {
        'user_jid': primary,
        'user_phone': jid_digits(phone_jid),
        'user_id_namespace': jid_namespace(primary) if primary else '',
    }


def resolve_identity(channel_id: str, external_user_id: str) -> Dict[str, str]:
    """Look up the WhatsApp identity of a session user.

    Every field is empty when the channel is not WhatsApp or the sender has no
    learned route yet. An empty `user_phone` means "unknown" and must never be
    treated as a licence to fall back to the raw user id, which may be a LID.
    """
    if not channel_id or not external_user_id:
        return dict(EMPTY_IDENTITY)
    try:
        from models.db import db
        channel = db.get_channel(channel_id)
    except Exception:
        _logger.warning("WhatsApp identity lookup failed for channel %s", channel_id, exc_info=True)
        return dict(EMPTY_IDENTITY)
    if not channel or not str(channel.get('type') or '').startswith('whatsapp'):
        return dict(EMPTY_IDENTITY)
    config = channel.get('config')
    if not isinstance(config, dict):
        return dict(EMPTY_IDENTITY)
    return identity_from_route((config.get('jid_routes') or {}).get(str(external_user_id)))
