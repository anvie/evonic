"""Auto-resolve an agent's ``PATH`` from the target host's shell init files.

Remote execution uses ``bash -s``, which is non-interactive *and* non-login, so it
never sources ``~/.profile`` or ``~/.bashrc``.  Directories those files prepend to
``PATH`` (``~/bin``, ``~/.local/bin``, …) are therefore invisible to the agent even
though an interactive SSH session sees them.

When an agent has no ``PATH`` variable yet -- or its SSH/Evonet workplace has just
(re)connected -- we probe a login shell (``~/.profile``) and an interactive shell
(``~/.bashrc``) once, merge the results, and persist the value as the agent's
``PATH`` variable so later calls cost nothing.
"""

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 15
_THROTTLE_SECONDS = 60

_REMOTE_BACKEND_NAMES = (
    'SSHBackend',
    'RemoteWorkplaceBackend',
    'TunnelWorkplaceBackend',
)

# Each probe prints its own bracketed payload; rc-file noise (motd, neofetch,
# stray echoes) cannot corrupt the value and the last match wins.
_MARK_RE = re.compile(r'<<P>>([LI])(.*?)<<E>>', re.S)

_PROBE_SCRIPT = r'''
bash -lc 'printf "\n<<P>>L%s<<E>>" "$PATH"' 2>/dev/null </dev/null
bash -ic 'printf "\n<<P>>I%s<<E>>" "$PATH"' 2>/dev/null </dev/null
'''

_lock = threading.Lock()
_stale_workplaces: set = set()
_memo: dict = {}        # agent_id -> (workplace_id, path)
_last_attempt: dict = {}  # agent_id -> monotonic timestamp of the last probe


# ---------------------------------------------------------------------------
# Backend / workplace detection
# ---------------------------------------------------------------------------

def is_remote_backend(backend) -> bool:
    """True for SSH and Evonet (tunnel) backends -- the ones `bash -s` starves of PATH."""
    if backend is None:
        return False
    if type(backend).__name__ in _REMOTE_BACKEND_NAMES:
        return True
    try:
        from backend.tools.lib.backends.ssh_backend import SSHBackend
        from backend.workplaces.backends.remote_workplace import RemoteWorkplaceBackend
        from backend.workplaces.backends.tunnel_workplace import TunnelWorkplaceBackend
        return isinstance(backend, (SSHBackend, RemoteWorkplaceBackend, TunnelWorkplaceBackend))
    except Exception:
        return False


def workplace_id_of(agent: dict, backend) -> str:
    """Workplace id for this run: agent context first, then backend attributes."""
    agent = agent if isinstance(agent, dict) else {}
    wid = agent.get('workplace_id') or getattr(backend, '_workplace_id', '') or ''
    if not wid:
        session_id = getattr(backend, '_session_id', '') or ''
        if session_id.startswith('workplace:'):
            wid = session_id.split(':', 1)[1]
    return wid or ''


def mark_workplace_stale(workplace_id: str) -> None:
    """Flag a workplace so the next bash call re-resolves PATH (reconnect policy)."""
    if not workplace_id:
        return
    with _lock:
        _stale_workplaces.add(workplace_id)
    logger.debug('[shell_path] workplace %s marked for PATH re-resolution', workplace_id)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def parse_probe_output(output: str) -> tuple:
    """Return ``(login_path, interactive_path)`` -- last marked payload per shell mode."""
    login = interactive = ''
    for kind, value in _MARK_RE.findall(output or ''):
        value = value.strip('\r\n')
        if kind == 'L':
            login = value
        else:
            interactive = value
    return login, interactive


def merge_paths(login: str, interactive: str) -> str:
    """Dedupe login + interactive entries, preserving login order first."""
    merged, seen = [], set()
    for raw in (login, interactive):
        for entry in (raw or '').replace('\r', '').split(':'):
            entry = entry.strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            merged.append(entry)
    return ':'.join(merged)


def _is_usable_path(value: str) -> bool:
    if not value or any(ch in value for ch in '\n\r\0'):
        return False
    parts = value.split(':')
    return '/bin' in parts or '/usr/bin' in parts


def probe_shell_path(backend, timeout: int = _PROBE_TIMEOUT) -> str:
    """Run the login+interactive probe on the target host. Returns '' on failure."""
    try:
        result = backend.run_bash(_PROBE_SCRIPT, timeout, {}) or {}
    except Exception as e:
        logger.debug('[shell_path] probe failed: %s', e)
        return ''
    stdout = result.get('stdout') or ''
    if not stdout:
        logger.debug('[shell_path] probe returned no output: %s', result.get('error'))
        return ''
    login, interactive = parse_probe_output(stdout)
    value = merge_paths(login, interactive)
    if not _is_usable_path(value):
        logger.debug('[shell_path] probe produced unusable PATH: %r', value[:200])
        return ''
    return value


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _agent_id(agent: dict) -> str:
    agent = agent if isinstance(agent, dict) else {}
    return agent.get('id') or agent.get('agent_id') or ''


def _is_simulation(agent) -> bool:
    try:
        from backend.tools.lib.simulation_scope import is_simulation
        return bool(is_simulation(agent if isinstance(agent, dict) else None))
    except Exception:
        return False


def _stored_path(agent_id: str) -> str:
    try:
        from models.db import db
        return (db.get_agent_variables_dict(agent_id) or {}).get('PATH') or ''
    except Exception as e:
        logger.debug('[shell_path] reading stored PATH failed: %s', e)
        return ''


def _persist(agent_id: str, value: str) -> None:
    try:
        from models.db import db
        db.set_agent_variable(agent_id, 'PATH', value)
        logger.info('[shell_path] stored PATH for agent=%s (%d entries)',
                    agent_id, value.count(':') + 1)
    except Exception as e:
        logger.debug('[shell_path] storing PATH failed: %s', e)


# ---------------------------------------------------------------------------
# Memo / throttle / stale bookkeeping
# ---------------------------------------------------------------------------

def _is_stale(workplace_id: str) -> bool:
    if not workplace_id:
        return False
    with _lock:
        return workplace_id in _stale_workplaces


def _clear_stale(workplace_id: str) -> None:
    if not workplace_id:
        return
    with _lock:
        _stale_workplaces.discard(workplace_id)


def _memo_get(agent_id: str, workplace_id: str):
    with _lock:
        entry = _memo.get(agent_id)
    if entry and entry[0] == workplace_id:
        return entry[1]
    return None


def _memo_put(agent_id: str, workplace_id: str, value: str) -> None:
    with _lock:
        _memo[agent_id] = (workplace_id, value)


def _throttled(agent_id: str) -> bool:
    with _lock:
        last = _last_attempt.get(agent_id)
    return bool(last) and (time.monotonic() - last) < _THROTTLE_SECONDS


def _mark_attempt(agent_id: str) -> None:
    with _lock:
        _last_attempt[agent_id] = time.monotonic()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def ensure_path_env(agent: dict, backend, env: dict) -> dict:
    """Inject a resolved PATH into *env* for SSH/Evonet runs. Never raises.

    Precedence:
      1. PATH already in env (agent variable or tool-supplied)      -> untouched
      2. non-SSH/Evonet backend, or a simulation                    -> untouched
      3. memoised value for the same workplace, not stale           -> injected
      4. persisted value, not stale                                 -> injected
      5. throttled after a failed probe                             -> injected if stored
      6. probe -> validate -> persist -> inject
    """
    env = env if isinstance(env, dict) else {}
    try:
        if (env.get('PATH') or '').strip():
            return env
        if not is_remote_backend(backend):
            return env
        agent_id = _agent_id(agent)
        if not agent_id or _is_simulation(agent):
            return env

        workplace_id = workplace_id_of(agent, backend)
        stale = _is_stale(workplace_id)

        if not stale:
            cached = _memo_get(agent_id, workplace_id)
            if cached:
                env['PATH'] = cached
                return env

        stored = _stored_path(agent_id)
        if stored and not stale:
            _memo_put(agent_id, workplace_id, stored)
            env['PATH'] = stored
            return env

        if not stale and _throttled(agent_id):
            if stored:
                env['PATH'] = stored
            return env

        _mark_attempt(agent_id)
        resolved = probe_shell_path(backend)
        if not resolved:
            if stored:
                env['PATH'] = stored
            return env

        _memo_put(agent_id, workplace_id, resolved)
        if resolved != stored:
            _persist(agent_id, resolved)
        _clear_stale(workplace_id)
        env['PATH'] = resolved
    except Exception as e:
        logger.debug('[shell_path] ensure_path_env skipped: %s', e)
    return env
