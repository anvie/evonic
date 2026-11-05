"""Tools a plugin lends to one inter-agent session, for as long as that session lives.

A plugin that has to be *talked to* (Privnt asking an agent to sort its queue, for example) needs the
agent to have the plugin's own tool while the conversation lasts. Two things must hold:

* the tool is the plugin's own — an id must be ``plugin:<owner>:<fn>`` with its owner named — so no
  agent can borrow a core tool or another plugin's tool;
* the lending is scoped: it is remembered per ``session_id`` and it expires (``GRANT_TTL_SECONDS``), so
  a lent tool never becomes part of the agent's assignment and never follows it into another session.

Only inter-agent sessions (``external_user_id`` starting with ``__agent__``) can hold grants: a person's
conversation can never be given a tool by a message that was routed into it.

Everything here is Flask-free on purpose: agent turns run in worker threads with no application context.
"""

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

_logger = logging.getLogger(__name__)

GRANT_TTL_SECONDS = 72 * 3600      # three days; refreshed each time a tool is lent again
MAX_LENT = 8                       # tools one session may hold at once
_LEND_TOOL_ID_RE = re.compile(r'^plugin:([A-Za-z0-9_]+):([A-Za-z0-9_]+)$')
_INTER_AGENT_PREFIX = '__agent__'


def is_inter_agent_session(external_user_id: Optional[str]) -> bool:
    """True for an agent-to-agent session — the only kind that may hold lent tools."""
    return str(external_user_id or '').startswith(_INTER_AGENT_PREFIX)


def parse(tool_id: str) -> Optional[Tuple[str, str]]:
    """``plugin:<owner>:<fn>`` -> ``(owner, fn)``; None when the id is not a plugin tool id."""
    match = _LEND_TOOL_ID_RE.match(str(tool_id or '').strip())
    return (match.group(1), match.group(2)) if match else None


def lent_tool_defs(owner: str, tool_ids: Iterable[str]) -> List[Tuple[str, dict]]:
    """The ``(tool_id, function_def)`` pairs of ``tool_ids`` that ``owner`` may really lend.

    A tool is dropped when the owner plugin is disabled or unknown, when the id belongs to another
    plugin or is malformed, when the tool would shadow a core tool, or when there is no backend file to
    run. The caller supplies the turn's tool list; adding is left to it.
    """
    owner = str(owner or '').strip()
    ids = [str(t or '').strip() for t in (tool_ids or []) if str(t or '').strip()]
    if not owner or not ids:
        return []
    try:
        from backend.plugin_manager import plugin_manager
        from backend.tools import tool_registry
        from backend.tools.registry import BUILTIN_TOOL_IDS

        if not plugin_manager.is_plugin_enabled(owner):
            _logger.warning('Lent tools ignored: plugin %r is not enabled', owner)
            return []
        defs = {d.get('id'): d for d in plugin_manager.get_all_plugin_tool_defs()
                if isinstance(d, dict)}
        core_names = {d.get('function', {}).get('name')
                      for d in tool_registry.get_tool_defs_from_json() if isinstance(d, dict)}
    except Exception:
        _logger.warning('Lent tools ignored: the plugin registry is unavailable', exc_info=True)
        return []

    found = []
    for tool_id in ids[:MAX_LENT]:
        parsed = parse(tool_id)
        if not parsed or parsed[0] != owner:
            _logger.warning('Lent tool %r refused: not a tool of plugin %r', tool_id, owner)
            continue
        fn_name = parsed[1]
        fn = (defs.get(tool_id) or {}).get('function') or {}
        if not fn or fn.get('name') != fn_name:
            _logger.warning('Lent tool %r refused: the plugin does not declare it', tool_id)
            continue
        if fn_name in BUILTIN_TOOL_IDS or fn_name in core_names:
            _logger.warning('Lent tool %r refused: it would shadow a core tool', tool_id)
            continue
        try:
            backend = plugin_manager.find_plugin_tool_backend(fn_name, owner)[0]
        except Exception:
            backend = None
        if backend is None:
            _logger.warning('Lent tool %r refused: no backend implementation', tool_id)
            continue
        found.append((tool_id, fn))
    return found


def grant(session_id: str, agent_id: str, owner: str, tool_ids: Iterable[str],
          granted_by: str = None) -> List[str]:
    """Remember ``owner``'s tools for ``session_id``. Returns the ids that were really granted.

    Refused (with a log line) when the session is not an inter-agent one, when the agent is unknown, or
    when no id survives ``lent_tool_defs``. Never raises: borrowing a tool must not be able to break a
    turn.
    """
    if not session_id or not agent_id:
        return []
    try:
        from models.db import db

        session = db.get_session_with_details(session_id) or {}
        if session.get('agent_id') != agent_id:
            _logger.warning('Lent tools refused for session %r: it is not owned by %r', session_id, agent_id)
            return []
        if not is_inter_agent_session(session.get('external_user_id')):
            _logger.warning('Lent tools refused for session %r: only agent-to-agent sessions may hold them',
                            session_id)
            return []
        found = lent_tool_defs(owner, tool_ids)
        if not found:
            return []
        ids = [tool_id for tool_id, _ in found]
        db.grant_session_tools(session_id, ids, owner, agent_id=agent_id,
                               granted_by=granted_by, ttl_seconds=GRANT_TTL_SECONDS)
        _logger.info('Lent %s to session %s (owner=%s, by=%s, ttl=%ss)',
                     ', '.join(ids), session_id, owner, granted_by or 'unknown', GRANT_TTL_SECONDS)
        return ids
    except Exception:
        _logger.warning('Lent tools could not be granted for session %r', session_id, exc_info=True)
        return []


def grants_for(session_id: str, agent_id: str) -> List[Dict[str, str]]:
    """The grants still valid for ``session_id``: ``[{tool_id, owner, granted_by, granted_at, expires_at}]``.

    Grants whose plugin has since been disabled are revoked here, so turning a plugin off takes its lent
    tools away immediately. Never raises.
    """
    if not session_id or not agent_id:
        return []
    try:
        from backend.plugin_manager import plugin_manager
        from models.db import db

        rows = db.list_session_tool_grants(session_id, agent_id=agent_id)
        live, dead = [], []
        for row in rows:
            try:
                enabled = plugin_manager.is_plugin_enabled(row.get('owner'))
            except Exception:
                enabled = True                       # an unreadable registry never takes a tool away mid-turn
            (live if enabled else dead).append(row)
        if dead:
            try:
                db.revoke_session_tools(session_id, agent_id=agent_id, tool_ids=[r['tool_id'] for r in dead])
            except Exception:
                pass
        return live
    except Exception:
        _logger.warning('Lent tools could not be read for session %r', session_id, exc_info=True)
        return []


def revoke(session_id: str, agent_id: str, tool_ids: Optional[Iterable[str]] = None) -> int:
    """Drop the grants of ``session_id`` (all of them when ``tool_ids`` is None)."""
    if not session_id or not agent_id:
        return 0
    try:
        from models.db import db
        return db.revoke_session_tools(session_id, agent_id=agent_id,
                                       tool_ids=list(tool_ids) if tool_ids else None)
    except Exception:
        _logger.warning('Lent tools could not be revoked for session %r', session_id, exc_info=True)
        return 0
