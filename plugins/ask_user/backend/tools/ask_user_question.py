"""
ask_user_question.py — pause the agent loop and ask the user multiple-choice questions.

Registers the question (the plugin's web UI polls /api/ask_user/pending and
renders it as a card above the chat input), then blocks until one of:

* the user submits the card (POST /api/ask_user/answer) → status 'answered'
* the user closes the card (POST /api/ask_user/cancel) → status 'dismissed'
* the user sends a normal chat message instead → status 'replied_in_chat'
* the user presses Stop → status 'cancelled'

There is deliberately no timeout: the card stays open until the user acts.
``question_required`` / ``question_resolved`` are also journaled on the
session's chat stream for any client that wants them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_logger = logging.getLogger(__name__)

# The active turn is reaped as stale after 15 minutes without journal activity;
# journal a small heartbeat event well inside that window while we wait.
_HEARTBEAT_SECONDS = 60
_POLL_SECONDS = 0.5

_NO_HUMAN_USERS = ('__agent__', '__scheduler__', 'api:')


def _web_chat_unavailable_reason(agent: dict) -> str | None:
    if agent.get('is_simulation'):
        return 'simulation runs have no user to answer'
    if agent.get('channel_id'):
        return 'this conversation is on an external channel, not the Evonic web chat'
    user_id = str(agent.get('user_id') or '')
    if user_id.startswith(_NO_HUMAN_USERS) or agent.get('from_agent_id'):
        return 'this turn was not started by a human in the web chat'
    if not agent.get('session_id'):
        return 'no chat session is attached to this turn'
    return None


def _peek_chat_reply(session_id: str) -> str | None:
    """Return a message the user typed while the question was open, if any.

    The message is left in the session's inject queue: the agent loop drains
    it right after this tool returns, so it lands in the conversation as a
    normal user message (and the web UI clears its "Queued" badge)."""
    from backend.agent_runtime import agent_runtime
    get_queue = getattr(agent_runtime, '_get_inject_queue', None)
    if get_queue is None:
        return None
    inject_queue = get_queue(session_id)
    with inject_queue.mutex:
        if not inject_queue.queue:
            return None
        item = inject_queue.queue[0]
    content = item.get('content') if isinstance(item, dict) else item
    return str(content or '').strip() or '[empty message]'


def execute(agent: dict, args: dict) -> Any:
    from models.db import db
    if db.get_setting('plugin_enabled:ask_user') != '1':
        return {'error': 'Ask User Question plugin is disabled. Enable it in Plugins settings.'}

    from plugins.ask_user.store import build_result, normalize_questions, question_registry

    questions, error = normalize_questions((args or {}).get('questions'))
    if error:
        return {'error': error}

    reason = _web_chat_unavailable_reason(agent)
    if reason:
        return {
            'status': 'unavailable',
            'error': f'Interactive questions are unavailable: {reason}. '
                     'Ask the user in plain text instead.',
        }

    from backend.agent_runtime import agent_runtime
    try:
        from backend.realtime_store import realtime_store
    except ImportError:
        # Evonic builds without the realtime journal have no stale-turn reaper
        # either, so the tool works without journaling.
        realtime_store = None

    session_id = agent['session_id']
    agent_id = agent.get('id') or ''
    if agent_runtime.is_stop_requested(session_id):
        return build_result('cancelled', questions)

    turn_id = realtime_store.current_turn_id(session_id) if realtime_store else None
    pending = question_registry.create(session_id, agent_id, questions)

    def publish(event_type: str, payload: dict) -> None:
        # Journal writes with the turn id also refresh the turn's liveness.
        if realtime_store is None:
            return
        try:
            realtime_store.publish('chat', event_type, payload, agent_id=agent_id,
                                   session_id=session_id, turn_id=turn_id)
        except Exception:
            _logger.exception('ask_user: failed to journal %s', event_type)

    publish('question_required', {
        'question_id': pending.question_id,
        'questions': questions,
    })

    status, chat_reply = 'cancelled', None
    last_heartbeat = time.monotonic()
    try:
        while not pending.event.wait(timeout=_POLL_SECONDS):
            if agent_runtime.is_stop_requested(session_id):
                break
            chat_reply = _peek_chat_reply(session_id)
            if chat_reply is not None:
                status = 'replied_in_chat'
                break
            if turn_id and time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
                publish('ask_user_waiting', {'question_id': pending.question_id})
                last_heartbeat = time.monotonic()
        if pending.answers is not None:
            # The card won the race; any typed message still reaches the loop.
            status, chat_reply = 'answered', None
        elif pending.dismissed:
            status, chat_reply = 'dismissed', None
    finally:
        question_registry.remove(pending.question_id, status)

    result = build_result(status, questions, pending.answers, chat_reply)
    publish('question_resolved', {
        'question_id': pending.question_id,
        'status': status,
        'answers': pending.answers or [],
        'reply': chat_reply,
    })
    return result
