"""Disposable sessions: flags and cleanup.

A session can be flagged as not-to-be-remembered (``save_memory=False`` on ``notify_agent``) and/or as expiring
(``ephemeral=True``). The memory pipeline asks ``is_memory_blocked``; ``purge_expired`` removes sessions whose time
is up (messages deleted, session archived). Purging runs whenever an ephemeral session is created and can be
called by anything that wants a sweep.
"""

import time
from typing import Optional

from backend.logging_config import get_logger
from models.db import db

_logger = get_logger(__name__)


def mark(session_id: str, agent_id: str, *, save_memory: bool = True, ttl: Optional[int] = None) -> None:
    db.set_session_flags(session_id, agent_id, save_memory=save_memory,
                         expires_at=(time.time() + max(60, int(ttl))) if ttl else None)
    if ttl:
        purge_expired()


def is_memory_blocked(session_id: str) -> bool:
    try:
        return bool(db.get_session_flags(session_id)['no_memory'])
    except Exception:
        return False


#: Tools that save things to an agent's long-term memory, and the file tools that can write into its knowledge base.
MEMORY_WRITE_TOOLS = frozenset({'remember', 'memorize', 'save_memory', 'evomem', 'forget_memory'})
FILE_WRITE_TOOLS = frozenset({'write_file', 'patch', 'str_replace', 'edit_file', 'append_file'})
KB_PATH_MARK = '/_self/kb'


def refuse_memory_write(fn_name: str, args: Optional[dict], agent_context: Optional[dict]) -> Optional[dict]:
    """An error result when a tool would save to the agent's memory from a session that must not be remembered
    (a background request, a session flagged ``save_memory=False``, or a turn started by a message with that
    flag); None when the call may go ahead.
    The session flag only stops the automatic summarizing, so an agent that chooses to call ``remember`` itself
    has to be stopped here."""
    from backend.agent_oneshot import is_oneshot_external_id
    writes_memory = fn_name in MEMORY_WRITE_TOOLS
    if not writes_memory and fn_name in FILE_WRITE_TOOLS:
        target = str((args or {}).get('path') or (args or {}).get('file_path') or '')
        writes_memory = KB_PATH_MARK in target
    if not writes_memory or not agent_context:
        return None
    if (is_oneshot_external_id(agent_context.get('user_id')) or agent_context.get('no_memory_turn')
            or is_memory_blocked(agent_context.get('session_id') or '')):
        return {'error': 'This is a background request that is not remembered, so nothing can be saved to memory '
                         'or the knowledge base from it. Do the task and reply.'}
    return None


def purge_expired(now: Optional[float] = None) -> int:
    """Remove ephemeral sessions that have expired. Returns how many."""
    removed = 0
    for row in db.expired_session_flags(now):
        try:
            if db.delete_session(row['session_id'], agent_id=row['agent_id']):
                removed += 1
        except Exception:
            _logger.exception("ephemeral_sessions: could not remove session %s", row['session_id'])
        db.drop_session_flags(row['session_id'])
    return removed
