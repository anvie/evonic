"""Shared WhatsApp channel — one number serves multiple agents.

Inbound messages are routed to an agent by the sender's identity using the
``routes`` map in the channel config:

    { "routes": { "<digits>": "<agent_id>", ... } }

Keys are DM sender digits (phone or LID) or a WhatsApp group ID; values are
agent ids. Unmapped senders/groups are dropped silently — the routing table
is the allowlist. In groups the group ID decides the agent, regardless of
which participant sends.
"""

import logging
from typing import Optional

from backend.channels.whatsapp import WhatsAppChannel

_logger = logging.getLogger(__name__)


def _lookup_route(routes: dict, sender: str, alt_sender: str = '') -> Optional[str]:
    """Resolve sender digits to an agent id, trying the alternate identifier
    (phone digits when the chat is LID-addressed) as fallback."""
    if not routes:
        return None
    return routes.get(sender) or (routes.get(alt_sender) if alt_sender else None)


class SharedWhatsAppChannel(WhatsAppChannel):
    @staticmethod
    def get_channel_type() -> str:
        return 'whatsapp_shared'

    def _resolve_agent(self, sender: str, is_group: bool, jid: str,
                       alt_sender: str = '', payload: Optional[dict] = None) -> Optional[str]:
        from models.db import db
        # Read routes fresh from the DB so admin edits apply without restart
        # (config PUTs do not restart running channels).
        channel = db.get_channel(self.channel_id)
        config = (channel or {}).get('config') or {}
        routes = config.get('routes') or {}
        if is_group:
            group_id = jid.split('@')[0] if '@' in jid else jid
            agent_id = routes.get(group_id)
        else:
            agent_id = _lookup_route(routes, sender, alt_sender)
        if not agent_id:
            # Unrestricted mode accepts only unmatched direct messages. Groups
            # remain route-only so their mention/reply security is unchanged.
            if not is_group and config.get('access_mode', 'assigned_only') == 'unrestricted':
                agent_id = config.get('default_agent_id') or None
                # A malformed runtime config fails closed and must not fall
                # through to the assigned-senders inbox workflow.
                if not agent_id:
                    _logger.warning(
                        'Shared unrestricted channel %s has no default agent',
                        self.channel_id)
                    return None
                if agent_id:
                    _logger.info(
                        'Shared unrestricted fallback: channel=%s sender=%s agent=%s',
                        self.channel_id, sender, agent_id)
            if agent_id:
                agent = db.get_agent(agent_id)
                if not agent or not agent.get('enabled'):
                    _logger.warning(
                        'Shared WhatsApp default agent %s is missing/disabled (channel %s)',
                        agent_id, self.channel_id)
                    return None
                return agent_id
            # DIAGNOSTIC: show the active channel_id and its actual route keys
            # so a route-on-the-wrong-channel (or unsaved route) is obvious.
            _logger.info(
                "Shared route MISS: channel=%s sender=%s alt=%s is_group=%s route_keys=%s",
                self.channel_id, sender, alt_sender, is_group, list(routes.keys()))
            # Capture the unknown sender so the admin can assign them from
            # the Shared Channel settings page (still no reply to the sender).
            p = payload or {}
            db.record_inbox_sender(
                self.channel_id,
                (jid.split('@')[0] if '@' in jid else jid) if is_group else sender,
                alt_user_id=alt_sender,
                push_name=p.get('pushName') or '',
                is_group=is_group,
                group_name=p.get('group_name') or '',
                preview=p.get('text') or '')
            return None
        agent = db.get_agent(agent_id)
        if not agent or not agent.get('enabled'):
            _logger.warning(
                "Shared WhatsApp route for %s points to missing/disabled agent %s (channel %s)",
                sender, agent_id, self.channel_id)
            return None
        return agent_id

    def _gate_sender(self, sender: str, is_group: bool, jid: str, text: str,
                     push_name: str, payload: dict) -> bool:
        # The routing table is the allowlist — _resolve_agent already dropped
        # unmapped senders, so no pairing/name-collection flow applies here.
        return True
