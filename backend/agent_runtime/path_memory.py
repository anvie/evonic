"""Remember the directories an agent's tools touch, so it can `recall(mode='paths')`
them later instead of searching the filesystem again. No LLM call, no filesystem access."""

import logging
import posixpath
import re
from typing import Any, Dict, Set

log = logging.getLogger(__name__)

_FILE_TOOLS = frozenset({'read_file', 'write_file', 'str_replace', 'patch'})
_DIR_TOOLS = frozenset({'tree', 'stats'})
_CD_RE = re.compile(r'(?:^|[;&|(\n]|&&)\s*cd\s+("[^"]*"|\'[^\']*\'|[^\s;&|)]+)')
_IGNORED_PREFIXES = ('/tmp', '/_self', '/proc', '/dev', '/sys')


def _clean(path: str, workspace: str) -> str:
    """Absolute normalized form of `path`, or '' if it should not be remembered."""
    path = (path or '').strip()
    if not path or '$' in path or '`' in path or path[0] in '~-':
        return ''
    if not path.startswith('/'):
        if not workspace:
            return ''
        path = posixpath.join(workspace, path)
    path = posixpath.normpath(path)
    if path == '/' or any(path == p or path.startswith(p + '/') for p in _IGNORED_PREFIXES):
        return ''
    return path


def extract_dirs(tool_name: str, args: Dict[str, Any], workspace: str) -> Set[str]:
    """Directories a tool call worked in."""
    args = args or {}
    raw = []
    if tool_name in _FILE_TOOLS:
        fp = args.get('file_path')
        if isinstance(fp, str):
            raw.append(posixpath.dirname(fp) or '.')
    elif tool_name in _DIR_TOOLS:
        p = args.get('path')
        if isinstance(p, str):
            raw.append(p)
    elif tool_name == 'bash':
        script = args.get('script') or args.get('command')
        if isinstance(script, str):
            raw.extend(m.strip('"\'') for m in _CD_RE.findall(script))
    return {d for d in (_clean(p, workspace) for p in raw) if d}


def on_tool_executed(event: Dict[str, Any]) -> None:
    """event_stream listener for `tool_executed`."""
    try:
        payload = event.get('payload', {})
        agent_id = payload.get('agent_id')
        if not agent_id or payload.get('has_error'):
            return
        tool_name = payload.get('tool_name')
        if tool_name not in _FILE_TOOLS | _DIR_TOOLS and tool_name != 'bash':
            return
        from models.db import db
        from backend.ephemeral_sessions import is_memory_blocked
        if is_memory_blocked(payload.get('session_id')):
            return
        agent = db.get_agent(agent_id)
        if not agent:
            return
        from backend.agent_runtime.context import _resolve_workspace
        dirs = extract_dirs(tool_name, payload.get('tool_args'), _resolve_workspace(agent))
        if dirs:
            db.touch_dirs(agent_id, sorted(dirs))
    except Exception:
        log.warning("path_memory: failed to record dirs", exc_info=True)
