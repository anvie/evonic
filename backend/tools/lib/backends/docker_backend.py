"""
DockerBackend — runs bash and Python inside a persistent Docker container.

This is the default execution backend. Each session gets one container, lazily
created on first call and reused for subsequent calls. The host workspace is
mounted at /workspace. Containers are destroyed automatically on idle timeout,
LRU eviction, or process exit.

Extracted from the original runpy.py and bash.py container pool logic.
"""

import atexit
import logging
import os
import re
import signal
import subprocess
import threading
import time

from backend.tools.lib.exec_backend import ExecutionBackend, truncate
from backend.tools.lib.process_tracker import process_tracker

logger = logging.getLogger(__name__)

try:
    from config import (
        SANDBOX_WORKSPACE,
        SANDBOX_IDLE_TIMEOUT,
        SANDBOX_MEMORY_LIMIT,
        SANDBOX_CPU_LIMIT,
        SANDBOX_NETWORK,
        SANDBOX_IMAGE,
        SANDBOX_MAX_CONTAINERS,
    )
except ImportError:
    SANDBOX_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
    SANDBOX_IDLE_TIMEOUT = 1800
    SANDBOX_MEMORY_LIMIT = '512m'
    SANDBOX_CPU_LIMIT = '1'
    SANDBOX_NETWORK = 'bridge'
    SANDBOX_IMAGE = 'evonic-sandbox:latest'
    SANDBOX_MAX_CONTAINERS = 10

# Directory containing the evonic helper package (mounted into the container)
_HELPERS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'runpy_helpers'))
_HELPERS_MOUNT = '/usr/local/lib/python3.11/site-packages/evonic'

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB

# PATH prefix prepended to every bash script so evonic/bin binaries take priority.
# The rg() wrapper fixes a stdin-inheritance bug: when `bash -s` reads from a pipe,
# child processes inherit that pipe as stdin and rg reads EOF instead of searching.
_EVONIC_BIN = f'{_HELPERS_MOUNT}/bin'
_PATH_PREFIX = (
    f'export PATH={_EVONIC_BIN}:$PATH\n'
    'rg() { if [ ! -t 0 ]; then command rg "$@" .; else command rg "$@"; fi; }\n'
    'export -f rg\n'
)

# ---------------------------------------------------------------------------
# Module-level container pool (shared across all DockerBackend instances)
# ---------------------------------------------------------------------------

_CONTAINER_PREFIX = 'evonic-'

_containers: dict = {}   # session_id -> {container_id, container_name, agent_id, last_used, created_at, first_call, workspace}
_pool_lock = threading.Lock()
_reaper_thread: threading.Thread = None
_monitor_thread: threading.Thread = None


def _ensure_reaper_running() -> None:
    global _reaper_thread
    with _pool_lock:
        if _reaper_thread is not None and _reaper_thread.is_alive():
            return
    t = threading.Thread(target=_reaper_loop, daemon=True, name='docker-backend-reaper')
    t.start()
    with _pool_lock:
        _reaper_thread = t


def _ensure_monitor_running() -> None:
    global _monitor_thread
    with _pool_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
    t = threading.Thread(target=_monitor_loop, daemon=True, name='docker-backend-monitor')
    t.start()
    with _pool_lock:
        _monitor_thread = t


def _monitor_loop() -> None:
    while True:
        time.sleep(60)
        try:
            try:
                fd_count = len(os.listdir(f'/proc/{os.getpid()}/fd'))
            except Exception:
                fd_count = -1

            with _pool_lock:
                count = len(_containers)
                at_limit = count >= SANDBOX_MAX_CONTAINERS
                stale_count = sum(1 for info in _containers.values()
                                if time.time() - info['last_used'] > SANDBOX_IDLE_TIMEOUT)

            if fd_count > 400:
                logger.critical(f'FD count={fd_count} — approaching limit, shutting down to prevent cascade')
                os.kill(os.getpid(), signal.SIGTERM)

            log_method = logger.warning if at_limit or stale_count > 0 else logger.info
            log_method(f'Pool status: {count}/{SANDBOX_MAX_CONTAINERS} containers, {stale_count} stale, fd={fd_count}')
            if at_limit:
                logger.warning('pool at capacity — LRU eviction will occur on next allocation')
        except Exception:
            logger.error('Monitor loop error', exc_info=True)


def get_pool_status() -> dict:
    """Return current pool state for monitoring/debugging."""
    with _pool_lock:
        containers = []
        for sid, info in _containers.items():
            containers.append({
                'session_id': sid[:12],
                'container_id': info['container_id'][:12],
                'container_name': info.get('container_name', ''),
                'agent_id': info.get('agent_id', ''),
                'created_at': info['created_at'],
                'last_used': info['last_used'],
                'workspace': info.get('workspace'),
                'first_call': info.get('first_call', False)
            })
        return {
            'pool_size': len(_containers),
            'max_containers': SANDBOX_MAX_CONTAINERS,
            'idle_timeout': SANDBOX_IDLE_TIMEOUT,
            'containers': containers
        }


def _startup_sweep() -> None:
    """Destroy evonic containers left over from previous (crashed) processes."""
    result = _docker('ps', '--filter', 'label=evonic.managed=1', '--format', '{{.Names}}')
    if result.returncode != 0:
        return
    live_names = {n.strip() for n in result.stdout.splitlines() if n.strip()}
    if not live_names:
        return
    with _pool_lock:
        known_names = {info['container_name'] for info in _containers.values()}
    orphans = live_names - known_names
    for name in orphans:
        logger.info(f'Startup sweep — destroying orphan container {name}')
        _docker('rm', '-f', name)


def _reconcile_with_docker() -> None:
    """Cross-check pool against live Docker state; fix divergence in both directions."""
    result = _docker('ps', '--filter', 'label=evonic.managed=1', '--format', '{{.Names}}')
    if result.returncode != 0:
        return
    live_names = {n.strip() for n in result.stdout.splitlines() if n.strip()}
    with _pool_lock:
        pool_snapshot = [(sid, info['container_name']) for sid, info in _containers.items()]
    pool_names = {name for _, name in pool_snapshot}

    # Orphans: in Docker but not in pool → destroy (leftover from a previous crash)
    for name in live_names - pool_names:
        logger.warning(f'Reconcile — orphan container {name} not in pool, destroying')
        _docker('rm', '-f', name)

    # Phantoms: in pool but not in Docker → remove from pool (killed externally)
    for sid, name in pool_snapshot:
        if name not in live_names:
            logger.warning(f'Reconcile — container {name} vanished externally, removing from pool')
            with _pool_lock:
                _containers.pop(sid, None)


def _reaper_loop() -> None:
    _startup_sweep()
    while True:
        time.sleep(60)
        try:
            deadline = time.time() - SANDBOX_IDLE_TIMEOUT
            stale = []
            with _pool_lock:
                for sid, info in list(_containers.items()):
                    if info['last_used'] < deadline:
                        stale.append(sid)
            for sid in stale:
                with _pool_lock:
                    info = _containers.get(sid)
                    if not info or info['last_used'] >= deadline:
                        continue
                logger.info(f'Idle timeout — destroying container for session {sid[:12]}')
                _destroy_container(sid)
            _reconcile_with_docker()
        except Exception:
            logger.error('Reaper loop error', exc_info=True)


@atexit.register
def _cleanup_all() -> None:
    with _pool_lock:
        sids = list(_containers.keys())
    for sid in sids:
        _destroy_container(sid)


def _container_name(session_id: str, agent_id: str = '') -> str:
    safe_session = re.sub(r'[^a-zA-Z0-9_.-]', '-', session_id)
    return f'{_CONTAINER_PREFIX}{safe_session}'


def _docker(*args, input_data: str = None, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = ['docker'] + list(args)
    return subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _docker_popen(*args) -> subprocess.Popen:
    """Like _docker() but returns a Popen object for interruptible execution.

    The caller is responsible for calling proc.communicate(input=..., timeout=...)
    in a polling loop to allow external kill via process_tracker.
    """
    cmd = ['docker'] + list(args)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _evict_lru() -> None:
    with _pool_lock:
        if not _containers:
            return
        lru_sid = min(_containers, key=lambda s: _containers[s]['last_used'])
    logger.warning(f'Max containers reached — evicting LRU session {lru_sid[:12]}')
    _destroy_container(lru_sid)


def _get_or_create_container(session_id: str, agent_id: str = '', workspace: str = None) -> tuple:
    """Return (container_id, None) or (None, error_string)."""
    effective_workspace = os.path.abspath(workspace if workspace else SANDBOX_WORKSPACE)
    needs_destroy = False
    with _pool_lock:
        if session_id in _containers:
            info = _containers[session_id]
            if info.get('workspace') != effective_workspace:
                logger.info(f'Workspace changed for session {session_id[:12]} — recreating container')
                needs_destroy = True
            else:
                info['last_used'] = time.time()
                return info['container_id'], None

    if needs_destroy:
        _destroy_container(session_id)

    with _pool_lock:
        count = len(_containers)
    if count >= SANDBOX_MAX_CONTAINERS:
        _evict_lru()

    name = _container_name(session_id, agent_id)
    effective_workspace = os.path.abspath(workspace if workspace else SANDBOX_WORKSPACE)
    created_at = time.time()

    cmd = [
        'run', '-d',
        '--rm',
        '--name', name,
        f'--memory={SANDBOX_MEMORY_LIMIT}',
        f'--cpus={SANDBOX_CPU_LIMIT}',
        f'--network={SANDBOX_NETWORK}',
        '--pids-limit=256',
        #'--read-only',
        '--tmpfs', '/tmp:rw,exec,size=3000m',
        '--tmpfs', '/root:rw,size=16m',
        '--label', 'evonic.managed=1',
        '--label', f'evonic.pid={os.getpid()}',
        '--label', f'evonic.created_at={created_at:.0f}',
        '-v', f'{effective_workspace}:/workspace:rw',
        '-v', f'{_HELPERS_DIR}:{_HELPERS_MOUNT}:ro',
        '-w', '/workspace',
        SANDBOX_IMAGE,
        'sleep', 'infinity',
    ]

    result = _docker(*cmd)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if 'already in use' in stderr or 'Conflict' in stderr:
            logger.info(f'Stale container found for {name} — removing and retrying')
            rm_result = _docker('rm', '-f', name)
            if rm_result.returncode != 0:
                logger.warning(f'Failed to remove stale container {name}: {rm_result.stderr.strip()}')
            result = _docker(*cmd)

    if result.returncode != 0:
        return None, f'Failed to start container: {result.stderr.strip()}'

    container_id = result.stdout.strip()
    with _pool_lock:
        _containers[session_id] = {
            'container_id': container_id,
            'container_name': name,
            'agent_id': agent_id,
            'last_used': created_at,
            'created_at': created_at,
            'first_call': True,
            'workspace': effective_workspace,
        }
    _ensure_reaper_running()
    _ensure_monitor_running()
    return container_id, None


def _destroy_container(session_id: str) -> dict:
    with _pool_lock:
        info = _containers.pop(session_id, None)

    if info is None:
        return {'result': 'no_container', 'detail': 'No active container for this session.'}

    container_id = info['container_id']
    result = _docker('rm', '-f', container_id)
    if result.returncode == 0:
        return {'result': 'container_destroyed', 'container_id': container_id[:12]}
    logger.warning(f'docker rm failed for {container_id[:12]}: {result.stderr.strip()} - re-adding to pool')
    with _pool_lock:
        _containers[session_id] = info
    return {'error': f'docker rm failed: {result.stderr.strip()}'}


# ---------------------------------------------------------------------------
# evonic helpers registry (first-call discovery metadata)
# ---------------------------------------------------------------------------

_REGISTRY_CODE = (
    "import json, importlib, inspect, evonic\n"
    "out = {}\n"
    "out['evonic'] = [n for n in dir(evonic) if not n.startswith('_') and inspect.isfunction(getattr(evonic,n)) and getattr(getattr(evonic,n),'__module__','') == 'evonic']\n"
    "mods = ['display','files','http']\n"
    "for m in mods:\n"
    "    mod = importlib.import_module(f'evonic.{m}')\n"
    "    out[f'evonic.{m}'] = [n for n in dir(mod) if not n.startswith('_') and inspect.isfunction(getattr(mod,n)) and getattr(getattr(mod,n),'__module__','').startswith(f'evonic.{m}')]\n"
    "print(json.dumps(out))\n"
)

_CONTAINER_GONE_PHRASES = ('no such container', 'is not running', 'cannot exec in a stopped')


def _is_container_gone(result: dict) -> bool:
    if 'error' not in result and result.get('exit_code', 0) == 0:
        return False
    combined = (result.get('stderr', '') + result.get('error', '')).lower()
    return any(p in combined for p in _CONTAINER_GONE_PHRASES)


def _get_available_helpers(container_id: str) -> dict:
    try:
        r = _docker('exec', '-i', container_id, 'python3', '-',
                    input_data=_REGISTRY_CODE, timeout=15)
        if r.returncode == 0:
            import json
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# DockerBackend
# ---------------------------------------------------------------------------

class DockerBackend(ExecutionBackend):
    """Executes bash/python inside a persistent Docker container."""

    def __init__(self, session_id: str, agent_id: str = '', workspace: str = None):
        self._session_id = session_id
        self._agent_id = agent_id
        self._workspace = workspace

    # ------------------------------------------------------------------
    # Path resolution — translate host paths to /workspace mount point
    # inside the container.
    # ------------------------------------------------------------------

    def resolve_path(self, path: str) -> str:
        """Convert a host filesystem path to the container's /workspace view.

        The host workspace is mounted at /workspace inside the container.
        Paths that fall within the host workspace are translated to their
        /workspace counterpart; all other paths pass through unchanged.
        """
        effective = os.path.abspath(self._workspace if self._workspace else SANDBOX_WORKSPACE)
        if path.startswith(effective):
            return '/workspace' + path[len(effective):]
        return path

    def run_bash(self, script: str, timeout: int, env: dict) -> dict:
        container_id, err = _get_or_create_container(self._session_id, agent_id=self._agent_id, workspace=self._workspace)
        if err:
            return {'error': err}

        env_args = []
        for k, v in env.items():
            env_args.extend(['-e', f'{k}={v}'])

        cmd = ['exec', '-i'] + env_args + [container_id, 'bash', '-s']
        t0 = time.time()
        proc = _docker_popen(*cmd)
        process_tracker.register(self._session_id, proc, proc.pid)
        try:
            stdout, stderr = self._poll_proc(proc, _PATH_PREFIX + script, timeout + 5, t0)
            if stdout is None:
                # Process was killed externally
                return {
                    'error': 'Execution stopped by user',
                    'exit_code': -9,
                    'execution_time': round(time.time() - t0, 3),
                }
        finally:
            process_tracker.unregister(self._session_id)

        elapsed = round(time.time() - t0, 3)
        with _pool_lock:
            for info in _containers.values():
                if info['container_id'] == container_id:
                    info['last_used'] = time.time()
                    break

        return {
            'stdout': truncate(stdout, _MAX_OUTPUT_BYTES),
            'stderr': truncate(stderr, _MAX_OUTPUT_BYTES),
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        with _pool_lock:
            info = _containers.get(self._session_id, {})
            is_first = info.get('first_call', False)

        container_id, err = _get_or_create_container(self._session_id, agent_id=self._agent_id, workspace=self._workspace)
        if err:
            return {'error': err}

        with _pool_lock:
            info = _containers.get(self._session_id, {})
            is_first = info.get('first_call', False)

        result = self._run_code(container_id, code, timeout, env)

        if _is_container_gone(result):
            logger.info(f'Container {container_id[:12]} gone — recreating for session {self._session_id[:12]}')
            with _pool_lock:
                _containers.pop(self._session_id, None)
            container_id, err = _get_or_create_container(self._session_id, agent_id=self._agent_id, workspace=self._workspace)
            if err:
                return {'error': err}
            with _pool_lock:
                info = _containers.get(self._session_id, {})
                is_first = info.get('first_call', False)
            result = self._run_code(container_id, code, timeout, env)

        if is_first and 'error' not in result:
            with _pool_lock:
                if self._session_id in _containers:
                    _containers[self._session_id]['first_call'] = False
            helpers = _get_available_helpers(container_id)
            if helpers:
                result['available_helpers'] = helpers

        return result

    @staticmethod
    def _poll_proc(proc, input_data: str, timeout: int, t0: float):
        """Poll a Popen process in 1s intervals, returning (stdout, stderr).

        Returns (None, None) if the process was killed externally (by
        process_tracker).  Raises no exceptions — timeout is detected
        internally and stored as proc._timed_out flag.
        """
        deadline = t0 + timeout
        while True:
            try:
                stdout, stderr = proc.communicate(input=input_data, timeout=1)
                input_data = None  # only send input on first call
                # Process finished — check if it was killed by signal
                if proc.returncode is not None and proc.returncode < 0:
                    return None, None
                return stdout, stderr
            except subprocess.TimeoutExpired:
                input_data = None  # already consumed
                # Check if killed externally during the 1s window
                if proc.poll() is not None:
                    if proc.returncode < 0:
                        return None, None
                    # Process exited with code >= 0 — read remaining output
                    try:
                        stdout, stderr = proc.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout, stderr = proc.communicate(timeout=2)
                    return stdout, stderr
                # Check deadline
                if time.time() > deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    return None, None  # caller interprets as timeout

    def _run_code(self, container_id: str, code: str, timeout: int, env: dict) -> dict:
        env_args = []
        for k, v in env.items():
            env_args.extend(['-e', f'{k}={v}'])

        cmd = ['exec', '-i'] + env_args + [container_id, 'python3', '-']
        t0 = time.time()
        proc = _docker_popen(*cmd)
        process_tracker.register(self._session_id, proc, proc.pid)
        try:
            stdout, stderr = self._poll_proc(proc, code, timeout + 5, t0)
            if stdout is None:
                if proc.returncode is not None and proc.returncode < 0:
                    return {
                        'error': 'Execution stopped by user',
                        'exit_code': -9,
                        'execution_time': round(time.time() - t0, 3),
                    }
                return {
                    'error': f'Execution timed out after {timeout}s',
                    'exit_code': -1,
                    'execution_time': round(time.time() - t0, 3),
                }
        finally:
            process_tracker.unregister(self._session_id)

        elapsed = round(time.time() - t0, 3)
        with _pool_lock:
            for info in _containers.values():
                if info['container_id'] == container_id:
                    info['last_used'] = time.time()
                    break

        return {
            'stdout': truncate(stdout, _MAX_OUTPUT_BYTES),
            'stderr': truncate(stderr, _MAX_OUTPUT_BYTES),
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    # ------------------------------------------------------------------
    # File I/O — run inside the container via docker exec + python3
    # ------------------------------------------------------------------

    def _container_exec_python(self, code: str, timeout: int = 30) -> dict:
        container_id, err = _get_or_create_container(self._session_id, agent_id=self._agent_id, workspace=self._workspace)
        if err:
            return {'error': err}
        cmd = ['exec', '-i', container_id, 'python3', '-']
        try:
            proc = _docker(*cmd, input_data=code, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            return {'error': f'Operation timed out after {timeout}s'}
        with _pool_lock:
            for info in _containers.values():
                if info['container_id'] == container_id:
                    info['last_used'] = time.time()
                    break
        if proc.returncode != 0:
            return {'error': proc.stderr.strip() or 'Docker exec failed'}
        return {'stdout': proc.stdout, 'exit_code': 0}

    def file_exists(self, path: str) -> bool:
        import json as _json
        r = self._container_exec_python(
            f"import os, json; print(json.dumps(os.path.exists({_json.dumps(path)})))")
        if 'error' in r:
            return False
        return r.get('stdout', '').strip() == 'true'

    def file_stat(self, path: str) -> dict:
        import json as _json
        code = (
            'import json, os\n'
            f'p = {_json.dumps(path)}\n'
            'if not os.path.exists(p):\n'
            '    print(json.dumps({"exists": False}))\n'
            'else:\n'
            '    sz = os.path.getsize(p)\n'
            '    isb = False\n'
            '    if sz > 0:\n'
            '        with open(p, "rb") as f:\n'
            '            isb = b"\\x00" in f.read(8192)\n'
            '    print(json.dumps({"exists": True, "size": sz, "is_binary": isb}))\n'
        )
        r = self._container_exec_python(code)
        if 'error' in r:
            return {'exists': False}
        try:
            return _json.loads(r.get('stdout', '{}'))
        except Exception:
            return {'exists': False}

    def read_file(self, path: str) -> dict:
        import json as _json, base64 as _b64
        code = (
            'import base64, json\n'
            f'p = {_json.dumps(path)}\n'
            'try:\n'
            '    with open(p, "rb") as f:\n'
            '        data = f.read()\n'
            '    print(json.dumps({"content": base64.b64encode(data).decode("ascii")}))\n'
            'except Exception as e:\n'
            '    print(json.dumps({"error": str(e)}))\n'
        )
        r = self._container_exec_python(code, timeout=30)
        if 'error' in r:
            return r
        try:
            result = _json.loads(r.get('stdout', '{}'))
        except Exception:
            return {'error': 'Failed to parse response from container'}
        if 'error' in result:
            return result
        data = _b64.b64decode(result['content']).decode('utf-8', errors='replace')
        return {'content': data}

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        import json as _json, base64 as _b64
        encoded = _b64.b64encode(content.encode('utf-8')).decode('ascii')
        mkdirs = 'True' if create_dirs else 'False'
        code = (
            'import base64, json, os\n'
            f'p = {_json.dumps(path)}\n'
            f'data = base64.b64decode({_json.dumps(encoded)})\n'
            f'mk = {mkdirs}\n'
            'try:\n'
            '    if mk:\n'
            '        os.makedirs(os.path.dirname(p), exist_ok=True)\n'
            '    with open(p, "wb") as f:\n'
            '        f.write(data)\n'
            '    print(json.dumps({"ok": True}))\n'
            'except PermissionError:\n'
            f'    print(json.dumps({{"error": "Permission denied writing: " + {_json.dumps(path)}}}))\n'
            'except IsADirectoryError:\n'
            f'    print(json.dumps({{"error": "Path is a directory: " + {_json.dumps(path)}}}))\n'
            'except Exception as e:\n'
            '    print(json.dumps({"error": str(e)}))\n'
        )
        r = self._container_exec_python(code, timeout=30)
        if 'error' in r:
            return r
        try:
            return _json.loads(r.get('stdout', '{}'))
        except Exception:
            return {'error': 'Failed to parse response from container'}

    def make_dirs(self, path: str) -> dict:
        import json as _json
        code = (
            'import json, os\n'
            f'p = {_json.dumps(path)}\n'
            'try:\n'
            '    os.makedirs(p, exist_ok=True)\n'
            '    print(json.dumps({"ok": True}))\n'
            'except Exception as e:\n'
            '    print(json.dumps({"error": str(e)}))\n'
        )
        r = self._container_exec_python(code, timeout=30)
        if 'error' in r:
            return r
        try:
            return _json.loads(r.get('stdout', '{}'))
        except Exception:
            return {'error': 'Failed to parse response from container'}

    def docker_cp_out(self, container_path: str, host_path: str) -> dict:
        """Copy a file from the container to the host filesystem."""
        container_id, err = _get_or_create_container(
            self._session_id, agent_id=self._agent_id, workspace=self._workspace,
        )
        if err:
            return {'error': err}
        os.makedirs(os.path.dirname(host_path) or '.', exist_ok=True)
        result = _docker('cp', f'{container_id}:{container_path}', host_path)
        if result.returncode != 0:
            return {'error': result.stderr.strip() or 'docker cp out failed'}
        return {'ok': True}

    def docker_cp_in(self, host_path: str, container_path: str) -> dict:
        """Copy a file from the host filesystem into the container."""
        container_id, err = _get_or_create_container(
            self._session_id, agent_id=self._agent_id, workspace=self._workspace,
        )
        if err:
            return {'error': err}
        result = _docker('cp', host_path, f'{container_id}:{container_path}')
        if result.returncode != 0:
            return {'error': result.stderr.strip() or 'docker cp in failed'}
        return {'ok': True}

    def destroy(self) -> dict:
        return _destroy_container(self._session_id)

    def status(self) -> dict:
        with _pool_lock:
            info = _containers.get(self._session_id)
        if info:
            return {
                'backend': 'docker',
                'container_id': info['container_id'][:12],
                'workspace': info.get('workspace'),
                'created_at': info.get('created_at'),
                'last_used': info.get('last_used'),
            }
        return {'backend': 'docker', 'container_id': None, 'detail': 'No container yet (will be created on first use).'}
