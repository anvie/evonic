"""
runts plugin lifecycle — registers the pre-execution tool guard.

The core injection guard (backend/tools/injection_guard.py) maps argument
fields per *built-in* tool name, so a plugin tool is outside it by
construction.  This guard closes that gap through the plugin API
(``register_tool_guard``, as plugins/workflow_guard/handler.py does): it
validates and limits the ``code`` field before anything is dispatched, and
fails closed when the ENABLED variable is off.

The content safety scan (HMADS) stays in the handler, where the full agent
context — ``safety_checker_enabled``, ``is_super``, ``_skip_safety`` — is
available; the guard hook only receives agent_id and a slim gate context.
"""

from __future__ import annotations

import importlib

from backend.plugin_manager import register_tool_guard, unregister_tool_guard

_PACKAGE = __name__.rsplit('.', 1)[0]
_tool = None


def _runts_module():
    """Import the tool module once (same file the registry dispatches to)."""
    global _tool
    if _tool is None:
        _tool = importlib.import_module(f'{_PACKAGE}.backend.tools.runts')
    return _tool


def tool_guard(agent_id, tool_name, args, context=None):
    if tool_name != 'runts':
        return None
    runts = _runts_module()

    if not runts._as_bool(runts._config().get('ENABLED'), True):
        return {'block': True, 'error': 'runts is disabled by the administrator.'}

    code = (args or {}).get('code')
    if not isinstance(code, str) or not code.strip():
        return {'block': True, 'error': "runts requires a non-empty string 'code'."}

    size = len(code.encode('utf-8'))
    if size > runts.MAX_CODE_BYTES:
        return {'block': True, 'error': (
            f'runts code is {size} bytes; the limit is {runts.MAX_CODE_BYTES} bytes. '
            'Write a .ts file into the workspace and run it in a second call.')}
    return None


def on_enable(sdk=None):
    register_tool_guard(tool_guard)


def on_disable(sdk=None):
    unregister_tool_guard(tool_guard)
