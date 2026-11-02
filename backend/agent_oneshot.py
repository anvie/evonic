"""One-shot questions to an agent, answered in a fresh, disposable session.

``ask()`` delivers a ``[<tag>] <message>`` system message to an agent in a brand-new session (so no earlier
context can leak in), runs the agent's normal turn, and returns the final answer. It reuses the
agent-to-agent reply path (``agent_messaging``), so the agent does not need to know it is being asked
programmatically.

``purge()`` deletes the messages of such sessions once they are older than a given age, so they can be read
for a while (to see what was asked and answered) and then disappear.

Plugins and other server code use this when they need a model's answer but want it produced by a configured
agent (its model, limits and usage accounting) rather than by a direct provider call.

One-shot sessions are never turned into memory: the summarizer's knowledge pipeline skips them (see
``is_oneshot_external_id``), so a question and its answer cannot leave notes behind in the asked agent's knowledge
base.
"""

import time
import uuid
from queue import Empty, Queue
from typing import Optional

from backend.logging_config import get_logger
from models.db import db

_logger = get_logger(__name__)

PREFIX = '__agent__'
MARKER = 'oneshot__'                     # external ids look like __agent__oneshot__<sender>_<random>
DEFAULT_TIMEOUT = 300


def _kind_prefix(sender: str) -> str:
    return f'{PREFIX}{MARKER}{sender}_'


def is_oneshot_external_id(external_user_id: Optional[str]) -> bool:
    """True for the session of a one-shot question (these are never summarized into memory)."""
    return bool(external_user_id) and str(external_user_id).startswith(PREFIX + MARKER)


def _blocked(agent: dict, agent_id: str, acl_sender: Optional[str]) -> Optional[str]:
    """Why this agent may not be asked right now (focus mode, the sender's messaging ACL), or None."""
    if acl_sender:
        from backend.tools.lib.messaging_acl import check_messaging_acl
        error = check_messaging_acl(db.get_agent(acl_sender), agent_id)
        if error:
            return error
    if agent.get('enable_agent_state'):
        try:
            from backend.agent_state import AgentState
            raw = db.get_agent_state(agent_id=agent_id)
            state = AgentState.deserialize(raw) if raw else None
            if state and state.focus:
                return f"Agent '{agent_id}' is focused ({state.focus_reason or 'no reason given'}) and cannot be interrupted."
        except Exception:
            pass                                       # an unreadable state never blocks a question
    return None


def resolve_human_route(agent_id: str, prefer: str = 'auto') -> Optional[dict]:
    """Where a message to the person this agent serves should go: its active channel session, otherwise its web
    chat session (``prefer='web'`` goes to the web chat first, for testing). None when it has neither."""
    from backend.agent_runtime import notifier
    if prefer != 'web':
        uid, cid = notifier._resolve_agent_target(agent_id)
        sid = db.get_session_id(agent_id, uid, cid) if uid else None
        if sid:
            return {'kind': 'channel', 'session_id': sid, 'external_user_id': uid, 'channel_id': cid}
    web = notifier._resolve_web_fallback(agent_id)
    if web:
        return {'kind': 'web', 'session_id': web['id'], 'external_user_id': web['external_user_id'], 'channel_id': None}
    return None


def ask(agent_id: str, message: str, *, tag: str = 'SYSTEM', sender: str = 'oneshot',
        timeout: int = DEFAULT_TIMEOUT, metadata: Optional[dict] = None, acl_sender: Optional[str] = None,
        ttl: int = 7200) -> dict:
    """Ask ``agent_id`` once. Returns ``{'success', 'reply', 'session_id', 'waited_seconds', 'error', 'timed_out'}``.

    ``sender`` names the requester (letters, digits, underscore); every call gets its own session
    ``__agent__oneshot__<sender>_<random>``. The session is left in place for ``purge()`` to remove later.
    ``acl_sender`` is an agent whose messaging ACL must allow the question (as for agent-to-agent messages);
    agents in focus mode are not interrupted. The session is never remembered and is removed after ``ttl``
    seconds."""
    from backend.agent_runtime.notifier import notify_agent
    from backend.tools import agent_messaging as am

    agent = db.get_agent(agent_id)
    if not agent:
        return {'success': False, 'error': f"Agent '{agent_id}' not found.", 'reply': None, 'session_id': None}
    if not agent.get('is_super') and not agent.get('enabled', True):
        return {'success': False, 'error': f"Agent '{agent.get('name', agent_id)}' is disabled.", 'reply': None,
                'session_id': None}

    blocked = _blocked(agent, agent_id, acl_sender)
    if blocked:
        return {'success': False, 'error': blocked, 'reply': None, 'session_id': None}

    sender_key = f'{MARKER}{sender}_{uuid.uuid4().hex[:10]}'
    reply_to_id = str(uuid.uuid4())
    queue: Queue = Queue()
    with am._WAIT_REGISTRY_LOCK:
        am._WAIT_REGISTRY[reply_to_id] = queue            # registered before dispatch: an instant reply cannot be missed
    meta = {'agent_message': True, 'from_agent_id': sender_key, 'from_agent_name': tag.split('/')[-1],
            'agent_message_depth': 1, 'reply_to_id': reply_to_id, 'report_to_id': None,
            'skip_auto_forward': True,                  # the answer goes to the asker, never on to a person
            **(metadata or {})}
    started = time.time()
    try:
        sent = notify_agent(agent_id=agent_id, tag=tag, message=message, external_user_id=f'{PREFIX}{sender_key}',
                            channel_id=None, dedup=False, metadata=meta,
                            save_memory=False, ephemeral=True, ephemeral_ttl=ttl)
        session_id = sent.get('session_id')
        if not sent.get('success'):
            return {'success': False, 'reply': None, 'session_id': session_id,
                    'error': f"Message delivery failed: {sent.get('reason', 'unknown')}."}
        try:
            reply = queue.get(timeout=max(5, int(timeout)))
        except Empty:
            _logger.warning("agent_oneshot: no answer from '%s' within %ss (session %s).", agent_id, timeout, session_id)
            return {'success': False, 'reply': None, 'session_id': session_id, 'timed_out': True,
                    'waited_seconds': round(time.time() - started, 1), 'error': 'timed out waiting for the answer'}
        return {'success': True, 'reply': reply, 'session_id': session_id,
                'waited_seconds': round(time.time() - started, 1), 'error': None}
    finally:
        with am._WAIT_REGISTRY_LOCK:
            am._WAIT_REGISTRY.pop(reply_to_id, None)


def purge(agent_id: str, *, sender: str = 'oneshot', older_than_seconds: int = 7200) -> int:
    """Delete one-shot sessions of ``agent_id`` (made by ``sender``) not touched for ``older_than_seconds``.
    Returns how many were removed. Only sessions this module creates are ever touched."""
    like = _kind_prefix(sender).replace('_', r'\_') + '%'
    cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - max(0, older_than_seconds)))
    chat = db._chat_db(agent_id)                 # each agent's sessions live in its own chat database
    with chat._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM chat_sessions WHERE agent_id = ? AND external_user_id LIKE ? ESCAPE '\\' "
            "AND (archived IS NULL OR archived = 0) AND updated_at < ?", (agent_id, like, cutoff)).fetchall()
    removed = 0
    for row in rows:
        try:
            removed += bool(db.delete_session(row[0], agent_id=agent_id))     # also removes the chat log files
        except Exception:
            _logger.exception("agent_oneshot: could not remove session %s", row[0])
    return removed
