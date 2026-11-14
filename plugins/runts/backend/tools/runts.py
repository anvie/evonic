"""
runts — run a JavaScript/TypeScript snippet with bun in the agent's workplace.

The execution backend is resolved per session exactly like bash.py / runpy.py:
the workplace (Docker container, bwrap, SSH/remote, tunnel) when one is
assigned, the default sandbox otherwise.  ``LocalBackend`` — an agent with
``sandbox_enabled=0`` and no workplace — is refused: nothing here runs on
Evonic's own process, and a project whose machine is missing must say so
instead of quietly executing on the host.

Why ``bun -e``: the snippets these agents write are multi-line TS with imports;
``bash`` needs heredocs and quote escaping for that, which is the exact failure
runpy exists to avoid for Python.  The code is passed as one shell-quoted argv
string (``shlex.quote``), so no escaping is left to the model.

Stateless per call: ``bun -e`` starts a fresh process, so nothing survives
between calls.  runpy promises "persistent state per session" because it keeps
a container alive; this tool deliberately does not — persist by writing into
the workspace instead.
"""

from __future__ import annotations

import shlex

from backend.tools.lib.agent_env import build_agent_env
from backend.tools.lib.exec_backend import registry, truncate
from backend.tools.lib.shell_path import ensure_path_env

try:  # same pipeline bash.py / runpy.py run their code through
    from backend.tools.lib.safety_pipeline import get_safety_pipeline, should_skip_safety
except ImportError:  # pragma: no cover - safety_pipeline ships with the app
    get_safety_pipeline = None
    should_skip_safety = lambda agent: True

PLUGIN_ID = 'runts'
TOOL_NAME = 'runts'

# Defaults mirror plugin.json; used only when the plugin config cannot be read.
_DEFAULTS = {
    'ENABLED': True,
    'TIMEOUT_SECONDS': 60,
    'MAX_OUTPUT_BYTES': 65536,
    'BUN_BIN': 'bun',
}

# One argv string is capped at MAX_ARG_STRLEN (131072 bytes) by the kernel, and
# the command is `bun -e <code>`; 120000 leaves room for the rest of the line.
MAX_CODE_BYTES = 120_000

_TIMEOUT_CEILING = 300  # same ceiling runpy applies to its timeout


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _config() -> dict:
    """Plugin variables (plugin.json defaults merged with the DB overrides)."""
    try:
        from backend.plugin_manager import plugin_manager
        cfg = plugin_manager.get_plugin_config(PLUGIN_ID) or {}
    except Exception:
        cfg = {}
    return {**_DEFAULTS, **{k: v for k, v in cfg.items() if v not in (None, '')}}


def _safety(code: str, agent: dict) -> dict:
    """HMADS verdict for *code*, matching bash.py's call."""
    if get_safety_pipeline is None or should_skip_safety(agent) \
            or not agent.get('safety_checker_enabled', 1) or agent.get('is_super'):
        return {'level': 'safe', 'score': 0, 'reasons': [], 'blocked_patterns': [], 'approval_info': {}}
    # TypeScript is not Python, so the bash pattern set is the applicable one:
    # a TS snippet that shells out or wipes a tree matches the shell rules.
    return get_safety_pipeline().check(code, tool_type='bash', agent_context=agent)


def execute(agent: dict, args: dict) -> dict:
    agent = agent or {}
    args = args or {}
    cfg = _config()

    if not _as_bool(cfg.get('ENABLED'), True):
        return {'error': 'runts is disabled by the administrator.'}

    code = args.get('code')
    if not isinstance(code, str) or not code.strip():
        return {'error': "Missing required argument: 'code'"}

    size = len(code.encode('utf-8'))
    if size > MAX_CODE_BYTES:
        return {'error': (f'Code is {size} bytes; runts accepts at most {MAX_CODE_BYTES} '
                          'bytes per call. Write a .ts file into the workspace and run it '
                          'in a second call instead.')}

    safety = _safety(code, agent)
    if safety['level'] == 'dangerous':
        return {
            'error': 'Execution blocked by heuristic safety system',
            'level': 'dangerous',
            'score': safety['score'],
            'reasons': safety['reasons'],
            'blocked_patterns': safety['blocked_patterns'],
        }
    if safety['level'] == 'requires_approval':
        return {
            'error': 'Code requires manual approval before execution',
            'level': 'requires_approval',
            'score': safety['score'],
            'reasons': safety['reasons'],
            'blocked_patterns': safety['blocked_patterns'],
            'approval_info': safety['approval_info'],
        }

    raw_timeout = cfg.get('TIMEOUT_SECONDS', _DEFAULTS['TIMEOUT_SECONDS'])
    try:
        timeout = max(1, min(int(raw_timeout), _TIMEOUT_CEILING))
    except (TypeError, ValueError):
        timeout = _DEFAULTS['TIMEOUT_SECONDS']

    try:
        max_bytes = max(1024, int(cfg.get('MAX_OUTPUT_BYTES', _DEFAULTS['MAX_OUTPUT_BYTES'])))
    except (TypeError, ValueError):
        max_bytes = _DEFAULTS['MAX_OUTPUT_BYTES']

    bun_bin = str(cfg.get('BUN_BIN') or _DEFAULTS['BUN_BIN'])

    session_id = agent.get('session_id') or 'default'
    try:
        backend = registry.get_backend(session_id, agent)
    except Exception as exc:
        # A missing/unreachable workplace must never fall back to the host.
        return {'error': f'runts: no execution backend for this workplace: {exc}'}

    # `type(...) is` on purpose: BwrapBackend subclasses LocalBackend and is a
    # real sandbox, so only the bare host backend is refused.
    from backend.tools.lib.backends.local_backend import LocalBackend
    if type(backend) is LocalBackend:
        return {'error': (
            'runts refuses to run on the Evonic host: this agent has no sandbox and no '
            'workplace (sandbox_enabled=0). Enable the sandbox, or assign a workplace whose '
            'checkout should hold the code.')}

    env = build_agent_env(agent)
    env = ensure_path_env(agent, backend, env)

    command = f'{shlex.quote(bun_bin)} -e {shlex.quote(code)}'
    result = backend.run_bash(command, timeout, env)

    if result.get('error'):
        return {'error': result['error'], 'exit_code': result.get('exit_code', -1)}
    if result.get('exit_code') == 127:
        return {
            'error': (f'runts: {bun_bin!r} is not installed in this workplace. '
                      'Install bun there, or point the BUN_BIN plugin variable at its path.'),
            'stdout': truncate(result.get('stdout', ''), max_bytes),
            'stderr': truncate(result.get('stderr', ''), max_bytes),
            'exit_code': 127,
            'execution_time': result.get('execution_time', 0),
        }

    return {
        'stdout': truncate(result.get('stdout', ''), max_bytes),
        'stderr': truncate(result.get('stderr', ''), max_bytes),
        'exit_code': result.get('exit_code'),
        'execution_time': result.get('execution_time'),
    }
