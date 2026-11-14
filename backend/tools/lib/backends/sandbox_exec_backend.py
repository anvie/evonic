"""
SandboxExecBackend — runs tool calls inside a pitura sandbox machine.

The machine's own devproxy serves `POST /exec` (PIT-400, `sandbox/cmd/devproxy/main.go`
`serveExec`), so a tool call runs in the same checkout the preview serves — no
SSH, no second clone, no save step to reconcile:

    POST https://<machine>-sandbox.pitura.id/exec
    Authorization: Bearer <SANDBOX_EXEC_TOKEN>
    {"command": "bun run build", "cwd": "/workspace", "timeout": 600}

    → application/x-ndjson, one frame per line:
        {"stream":"stdout","data":"…"} | {"stream":"stderr","data":"…"}
        {"exitCode":0,"durationMs":1234}          (always last)
        {"exitCode":-1,"durationMs":…,"timedOut":true}

The endpoint and its per-machine token are handed to this process per chat request
by the dashboard (`X-Pitura-Exec-Url` / `X-Pitura-Exec-Token`, PIT-401), so nothing
here is configured by hand and nothing outlives the session.

`register_session` is keyed by agent: the pitura dashboard maps one project to one
evonic agent, and evonic refuses a second concurrent turn on a busy agent
(`plugins/agentapi/routes.py`, 409), so one entry per agent is one session.

ponytail: in-process, per-agent. Add a session-id key when one agent serves two
machines at once.
"""

import base64
import json
import logging
import shlex
import threading
import time
import urllib.error
import urllib.request

from backend.tools.lib.exec_backend import ExecutionBackend, parse_file_stat_output

_logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE = '/workspace'
# The machine caps the command itself (`timeout` in the request); this only has
# to outlast that cap plus the handshake, or the caller sees a truncated stream
# and cannot tell it from a command that never finished.
DEADLINE_SLACK = 30

_sessions = {}
_sessions_lock = threading.Lock()


def register_session(agent_id: str, url: str, token: str, workspace: str = DEFAULT_WORKSPACE) -> None:
    """Point this agent's tools at a machine's `/exec` for the rest of the session."""
    if not (agent_id and url and token):
        return
    with _sessions_lock:
        _sessions[agent_id] = {'url': url, 'token': token,
                               'workspace_path': workspace or DEFAULT_WORKSPACE}
    _logger.info('[sandbox_exec] agent=%s tools run in %s', agent_id, url)


def clear_session(agent_id: str) -> None:
    """Forget a registration — a request that names no machine is the docker path again."""
    if not agent_id:
        return
    with _sessions_lock:
        if _sessions.pop(agent_id, None) is not None:
            _logger.info('[sandbox_exec] agent=%s tools back on the default backend', agent_id)


def session_for(agent_id: str) -> dict:
    """The machine this agent's tools run in, or `{}`."""
    if not agent_id:
        return {}
    with _sessions_lock:
        return dict(_sessions.get(agent_id) or {})


class SandboxExecBackend(ExecutionBackend):
    """Executes commands over a sandbox machine's own `POST /exec`."""

    def __init__(self, config=None, workplace_id: str = ''):
        config = config or {}
        self._url = (config.get('url') or '').strip()
        self._token = (config.get('token') or '').strip()
        self._workspace = config.get('workspace_path') or DEFAULT_WORKSPACE
        self._workplace_id = workplace_id

    def set_workspace(self, path: str) -> None:
        self._workspace = path

    def resolve_path(self, path: str) -> str:
        """Make relative paths absolute against the machine's checkout."""
        if not path.startswith('/'):
            return self._workspace.rstrip('/') + '/' + path
        return path

    # ------------------------------------------------------------------
    # The one network call every method funnels through
    # ------------------------------------------------------------------

    def _exec(self, command: str, timeout: int, on_output=None) -> dict:
        """POST one command and read the ndjson frames to the exit code.

        Returns {stdout, stderr, exit_code, execution_time}. A transport fault
        is a result with `exit_code: -1` and the reason in `stderr`, not an
        exception: a tool call must answer, and the agent needs to read why.
        """
        if not self._url or not self._token:
            return {'stdout': '', 'stderr': 'sandbox exec is not configured '
                    '(url and token are required)', 'exit_code': -1,
                    'execution_time': 0}
        body = json.dumps({'command': command, 'cwd': self._workspace,
                           'timeout': max(1, int(timeout))}).encode('utf-8')
        req = urllib.request.Request(self._url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Authorization', f'Bearer {self._token}')
        started = time.time()
        out, err, exit_code = [], [], -1
        try:
            with urllib.request.urlopen(req, timeout=max(1, int(timeout)) + DEADLINE_SLACK) as resp:
                for raw in resp:
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stream = frame.get('stream')
                    if stream in ('stdout', 'stderr'):
                        text = frame.get('data') or ''
                        if not text:
                            continue
                        (out if stream == 'stdout' else err).append(text)
                        if stream == 'stdout' and on_output is not None:
                            try:
                                on_output(text)
                            except Exception:  # a broken sink must not lose output
                                pass
                    elif 'exitCode' in frame:
                        exit_code = int(frame.get('exitCode', -1))
                        if frame.get('timedOut'):
                            err.append(f"\ncommand timed out after {timeout}s")
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:400]
            err.append(f'exec answered {e.code}: {detail}')
        except Exception as e:  # URLError, socket timeout, reset
            err.append(f'exec unreachable: {e}')
        return {'stdout': ''.join(out), 'stderr': ''.join(err),
                'exit_code': exit_code, 'execution_time': time.time() - started}

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _with_env(self, script: str, env: dict) -> str:
        """`env` is applied in front of the script, the way a shell export would.

        The machine runs one command string; the keys are validated by the
        caller (`validate_env_keys`), so quoting them back is enough.
        """
        if not env:
            return script
        exports = ' '.join(f'{k}={shlex.quote(str(v))}' for k, v in env.items())
        return f'export {exports}\n{script}'

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        return self._exec(self._with_env(script, env), timeout, on_output=on_output)

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        # The sandbox image is node-first: when python3 is missing the machine
        # answers exit 127 with that message, which is the honest result.
        script = f"python3 - <<'__PYEOF__'\n{code}\n__PYEOF__"
        return self._exec(self._with_env(script, env), timeout)

    # ------------------------------------------------------------------
    # File I/O — every operation is one command in the same environment, so a
    # path the agent writes is the path the dev server reads.
    # ------------------------------------------------------------------

    def file_exists(self, path: str) -> bool:
        r = self._exec(f'test -e {shlex.quote(self.resolve_path(path))} && echo yes || echo no', 10)
        return r.get('stdout', '').strip() == 'yes'

    def file_stat(self, path: str) -> dict:
        p = shlex.quote(self.resolve_path(path))
        # A NUL byte cannot be passed as a shell argument, so the binary test is
        # a byte count: a text file is the same size after `tr -d '\000'`.
        r = self._exec(
            f'if [ ! -e {p} ]; then echo "exists=0 size=0 is_binary=0"; '
            f'else n=$(head -c 8192 {p} | wc -c); '
            f'm=$(head -c 8192 {p} | tr -d \'\\000\' | wc -c); '
            f'b=0; [ "$n" -ne "$m" ] && b=1; '
            f'echo "exists=1 size=$(wc -c < {p}) is_binary=$b"; fi',
            15,
        )
        return parse_file_stat_output(r.get('stdout', ''))

    def read_file(self, path: str) -> dict:
        r = self._exec(f'cat {shlex.quote(self.resolve_path(path))}', 30)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'read failed'}
        return {'content': r.get('stdout', '')}

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        return self._write_bytes(path, content.encode('utf-8'), create_dirs)

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        return self._write_bytes(path, data, create_dirs)

    def _write_bytes(self, path: str, data: bytes, create_dirs: bool) -> dict:
        p = self.resolve_path(path)
        script = ''
        if create_dirs and '/' in p:
            script += f'mkdir -p {shlex.quote(p.rsplit("/", 1)[0])}\n'
        encoded = base64.b64encode(data).decode('ascii')
        script += f'printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(p)}\n'
        r = self._exec(script, 60)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'write failed'}
        return {'ok': True}

    def cat_file_bytes(self, path: str) -> dict:
        p = shlex.quote(self.resolve_path(path))
        r = self._exec(f'base64 -w0 < {p}', 60)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'read failed'}
        try:
            return {'bytes': base64.b64decode(r.get('stdout', ''), validate=False)}
        except Exception as e:
            return {'error': f'undecodable base64: {e}'}

    def delete_file(self, path: str) -> dict:
        r = self._exec(f'rm -f {shlex.quote(self.resolve_path(path))}', 15)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'rm failed'}
        return {'ok': True}

    def make_dirs(self, path: str) -> dict:
        r = self._exec(f'mkdir -p {shlex.quote(self.resolve_path(path))}', 15)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'mkdir failed'}
        return {'ok': True}

    # ------------------------------------------------------------------
    # Lifecycle — the machine belongs to the dashboard's session, not to this
    # backend, so there is nothing here to tear down.
    # ------------------------------------------------------------------

    def destroy(self) -> dict:
        return {'result': 'ok'}

    def status(self) -> dict:
        return {
            'backend': 'sandbox_exec',
            'status': 'connected' if (self._url and self._token) else 'unconfigured',
            'url': self._url,
            'workspace': self._workspace,
        }
