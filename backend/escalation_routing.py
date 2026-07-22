"""Route a human reply back to the delegated agent that requested it."""

from typing import Optional

from backend.logging_config import get_logger
from models.db import db

_logger = get_logger(__name__)


def route_pending_escalation_reply(
    originating_agent_id: str,
    originating_session_id: str,
    message: str,
) -> Optional[dict]:
    """Consume one pending correlation and resume its inter-agent session.

    Returns the notifier result when a pending escalation exists, otherwise None.
    Consumption is atomic, so concurrent/duplicate inbound processing cannot resume
    the delegated agent twice.
    """
    escalation = db.consume_pending_user_escalation(originating_session_id)
    if not escalation:
        return None
    if escalation.get('originating_agent_id') != originating_agent_id:
        _logger.error(
            "Escalation '%s' origin mismatch: expected '%s', received '%s'.",
            escalation.get('id'), escalation.get('originating_agent_id'),
            originating_agent_id,
        )
        return {'success': False, 'reason': 'origin_agent_mismatch'}

    metadata = escalation.get('metadata') or {}
    from backend.agent_runtime.notifier import notify_agent
    result = notify_agent(
        agent_id=escalation['requesting_agent_id'],
        tag='USER/ESCALATION-REPLY',
        message=message,
        session_id=escalation['requesting_session_id'],
        dedup=False,
        trigger_llm=True,
        metadata={
            'agent_message': True,
            'agent_reply': True,
            'escalation_reply': True,
            'escalation_id': escalation['id'],
            'from_agent_id': originating_agent_id,
            'report_to_id': escalation['external_user_id'],
            'report_to_channel_id': escalation.get('channel_id'),
            'session_id': originating_session_id,
            'agent_message_depth': metadata.get('agent_message_depth', 0),
            'reply_to_id': metadata.get('reply_to_id'),
        },
    )
    if not result.get('success'):
        _logger.error(
            "Escalation reply '%s' could not resume agent '%s' session '%s': %s",
            escalation['id'], escalation['requesting_agent_id'],
            escalation['requesting_session_id'], result,
        )
    return result
