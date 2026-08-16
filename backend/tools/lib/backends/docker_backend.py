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
from backend.tools._workspace import scratch_dir

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
        SANDBOX_PERSISTENT_CONTAINER_ENABLED,
    )
except ImportError:
    SANDBOX_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
    SANDBOX_IDLE_TIMEOUT = 1800
    SANDBOX_MEMORY_LIMIT = '512m'
    SANDBOX_CPU_LIMIT = '1'
    SANDBOX_NETWORK = 'bridge'
    SANDBOX_IMAGE = 'evonic-sandbox:latest'
    SANDBOX_MAX_CONTAINERS = 10
    SANDBOX_PERSISTENT_CONTAINER_ENABLED = True

# Directory containing the evonic helper package (mounted into the container)
_HELPERS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'runpy_helpers'))
_HELPERS_MOUNT = '/usr/local/lib/python3.11/site-packages/evonic'

# Host artifact registry root: BASE_DIR/shared/agents/<agent_id>/artifacts is
# the authoritative artifact location served by the web UI / list_artifacts /
# fetch_artifact.  It is bind-mounted into every sandbox container at
# /workspace/shared/agents/<agent_id>/artifacts so bash/runpy writes land on
# the same directory the UI reads (prevents silent artifact divergence).
#
# When an agent's workspace differs from BASE_DIR (e.g. an agent whose
# workspace is agents/<id>/), the sandbox's /workspace mount does NOT include
# that registry, so /workspace/shared/agents/<id>/artifacts would silently
# resolve to a DIFFERENT directory than the one the UI serves.  To keep the
# sandbox view and the host registry consistent we bind-mount the registry into
# the container at the same relative path.
_ARTIFACTS_ROOT = os.path.normpath(os.path.join(SANDBOX_WORKSPACE, 'shared', 'agents'))

# Bump when the container mount layout changes so existing containers are
# recreated with the new mounts (persistent containers survive restarts, so an
# in-memory version check is required to detect stale mounts).
_MOUNT_LAYOUT_VERSION = 2

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
        persistent_count = 0
        for sid, info in _containers.items():
            is_persistent = bool(info.get('persistent'))
            if is_persistent:
                persistent_count += 1
            containers.append({
                'session_id': sid[:12],
                'container_id': info['container_id'][:12],
                'container_name': info.get('container_name', ''),
                'agent_id': info.get('agent_id', ''),
                'created_at': info['created_at'],
                'last_used': info['last_used'],
                'workspace': info.get('workspace'),
                'first_call': info.get('first_call', False),
                'persistent': is_persistent,
            })
        return {
            'pool_size': len(_containers),
            'max_containers': SANDBOX_MAX_CONTAINERS,
            'idle_timeout': SANDBOX_IDLE_TIMEOUT,
            'persistent_count': persistent_count,
            'persistent_enabled': SANDBOX_PERSISTENT_CONTAINER_ENABLED,
            'containers': containers,
        }


def _list_persistent_stopped_containers() -> list:
    """Return names of evonic-managed persistent containers that are stopped.

    These were left behind by a previous `evonic` process and can be restarted
    transparently to preserve the agent's installed tools.
    """
    ps = _docker('ps', '-a',
                 '--filter', 'label=evonic.managed=1',
                 '--filter', 'label=evonic.persistent=1',
                 '--format', '{{.Names}} {{.State}}')
    if ps.returncode != 0:
        return []
    out = []
    for line in ps.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lower().startswith('exited'):
            out.append(parts[0])
    return out


def _restart_persistent_container(name: str) -> str:
    """Start a persistent container by name; return new container_id or ''."""
    res = _docker('start', name)
    if res.returncode != 0:
        logger.warning(f'Failed to start persistent container {name}: {res.stderr.strip()}')
        return ''
    return res.stdout.strip()


def _startup_sweep() -> None:
    """Sweep state left over from previous (crashed) processes.

    - Restart stopped persistent containers so their installed tools survive
      across `evonic` restarts.
    - Destroy non-persistent orphans (containers not in the in-memory pool).
    """
    # 1. Restart persistent stopped containers.
    for name in _list_persistent_stopped_containers():
        logger.info(f'Startup sweep — restarting persistent container {name}')
        new_id = _restart_persistent_container(name)
        if not new_id:
            continue
        # Adopt the container into the pool so subsequent calls hit it without
        # having to recreate. The pool key is the agent_id (matches the
        # persistent-key contract in _get_or_create_container); we don't know
        # agent_id here, but we can set it later from the DockerBackend that
        # actually uses it. For now, key by container_name.
        # (Adoption is best-effort: if no DockerBackend picks it up on first
        # call, _get_or_create_container will re-create it.)
        with _pool_lock:
            # Don't clobber an existing pool entry.
            exists = any(info.get('container_name') == name for info in _containers.values())
            if not exists:
                _containers[name] = {
                    'container_id': new_id,
                    'container_name': name,
                    'agent_id': '',
                    'last_used': time.time(),
                    'created_at': time.time(),
                    'first_call': False,
                    'workspace': None,
                    'persistent': True,
                    'pool_key': name,
                }
                logger.info(f'Startup sweep — adopted persistent container {name} into pool')

    # 2. Destroy non-persistent orphans (only).
    result = _docker('ps', '-a',
                     '--filter', 'label=evonic.managed=1',
                     '--format', '{{.Names}} {{.State}}')
    if result.returncode != 0:
        return
    with _pool_lock:
        known_names = {info['container_name'] for info in _containers.values()}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        name, state = parts
        if name in known_names:
            continue
        if state.lower() == 'running':
            # Non-persistent running container: kill it.
            logger.info(f'Startup sweep — destroying orphan running container {name}')
            _docker('rm', '-f', name)
        elif state.lower().startswith('exited'):
            # Non-persistent exited container: remove silently.
            logger.info(f'Startup sweep — removing exited orphan container {name}')
            _docker('rm', name)


def _reconcile_with_docker() -> None:
    """Cross-check pool against live Docker state; fix divergence in both directions.

    - Orphans that are non-persistent: destroy.
    - Orphans that are persistent: leave alone (they'll be restarted by
      _startup_sweep on next process boot, or by manual intervention).
    - Phantoms (in pool but not in Docker): remove from pool.
    """
    result = _docker('ps', '-a',
                     '--filter', 'label=evonic.managed=1',
                     '--format', '{{.Names}} {{.Labels}}')
    if result.returncode != 0:
        return
    live_names = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        name, labels = parts
        is_persistent = 'evonic.persistent=1' in labels
        live_names[name] = is_persistent
    with _pool_lock:
        pool_snapshot = [(sid, info['container_name'], bool(info.get('persistent')))
                         for sid, info in _containers.items()]
    pool_names = {name for _, name, _ in pool_snapshot}

    # Orphans: in Docker but not in pool → destroy if non-persistent.
    for name in live_names:
        if name in pool_names:
            continue
        if live_names[name]:
            logger.warning(f'Reconcile — persistent orphan container {name} not in pool, leaving for startup')
            continue
        logger.warning(f'Reconcile — orphan container {name} not in pool, destroying')
        _docker('rm', '-f', name)

    # Phantoms: in pool but not in Docker → remove from pool (killed externally).
    for sid, name, _ in pool_snapshot:
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
                    if info.get('persistent'):
                        continue
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
    # 1. Stop persistent containers (do NOT remove) so they survive process exit.
    with _pool_lock:
        persistent_entries = [(sid, info) for sid, info in _containers.items()
                              if info.get('persistent')]
    for sid, info in persistent_entries:
        try:
            _docker('stop', info['container_id'])
        except Exception:
            pass

    # 2. Destroy all containers from the pool and remove them. Persistent ones
    #    are skipped so that `--restart=unless-stopped` left them stopped but
    #    present on disk, ready for the next `evonic start` to find and start.
    with _pool_lock:
        sids = list(_containers.keys())
    for sid in sids:
        with _pool_lock:
            info = _containers.get(sid)
        if info and info.get('persistent'):
            logger.info(f'Persistent container for {sid[:12]} kept on disk (stopped, not removed)')
            _containers.pop(sid, None)
            continue
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
        # Prefer evicting non-persistent entries first; persistent ones only evicted if no choice.
        non_persistent = [s for s, i in _containers.items() if not i.get('persistent')]
        if non_persistent:
            lru_sid = min(non_persistent, key=lambda s: _containers[s]['last_used'])
            is_persistent = False
        else:
            lru_sid = min(_containers, key=lambda s: _containers[s]['last_used'])
            is_persistent = True
        logger.warning(
            f'Max containers reached — evicting LRU session {lru_sid[:12]} '
            f'({"persistent" if is_persistent else "non-persistent"})'
        )
    _destroy_container(lru_sid)


def _get_or_create_container(
    session_id: str,
    agent_id: str = '',
    workspace: str = None,
    persistent: bool = False,
) -> tuple:
    """Return (container_id, None) or (None, error_string).

    When ``persistent`` is True, the container is keyed by ``agent_id`` (not
    ``session_id``) so every session of the same main agent reuses it,
    configured with ``--restart=unless-stopped`` and without ``--rm`` so
    it survives ``evonic`` restarts with all installed state preserved.
    """
    effective_workspace = os.path.abspath(workspace if workspace else SANDBOX_WORKSPACE)
    needs_destroy = False
    pool_key = (agent_id or session_id) if persistent else session_id
    with _pool_lock:
        if pool_key in _containers:
            info = _containers[pool_key]
            if (info.get('workspace') != effective_workspace
                    or info.get('mount_version') != _MOUNT_LAYOUT_VERSION):
                logger.info(f'Workspace/mount changed for {("persistent " if persistent else "")}{pool_key[:12]} — recreating container')
                needs_destroy = True
            else:
                info['last_used'] = time.time()
                return info['container_id'], None

    if needs_destroy:
        _destroy_container(pool_key)

    with _pool_lock:
        count = len(_containers)
    if count >= SANDBOX_MAX_CONTAINERS:
        _evict_lru()

    name = _container_name(pool_key, agent_id if persistent else '')
    effective_workspace = os.path.abspath(workspace if workspace else SANDBOX_WORKSPACE)
    scratch = scratch_dir(agent_id)
    created_at = time.time()

    # Bind-mount the agent's host artifact registry into the container at the
    # same relative path the sandbox-visible path convention uses, so that
    # /workspace/shared/agents/<id>/artifacts/ always points at the SAME
    # directory the web UI / list_artifacts / fetch_artifact serve.  Without
    # this, agents whose workspace differs from BASE_DIR would silently append
    # to a sandbox copy the UI never reads (artifact divergence bug).
    artifacts_mounts = []
    if agent_id and _ARTIFACTS_ROOT:
        registry_dir = os.path.join(_ARTIFACTS_ROOT, agent_id, 'artifacts')
        # Skip when the workspace mount already exposes the registry at the
        # same container path (workspace == BASE_DIR): bind-mounting a
        # directory onto itself is redundant.
        ws_relative = os.path.join(effective_workspace, 'shared', 'agents',
                                   agent_id, 'artifacts')
        if os.path.realpath(ws_relative) != os.path.realpath(registry_dir):
            os.makedirs(registry_dir, exist_ok=True)
            artifacts_mounts = [
                '-v', f'{registry_dir}:/workspace/shared/agents/{agent_id}/artifacts:rw',
            ]

    cmd = [
        'run', '-d',
        *(('--rm',) if not persistent else ()),
        '--restart=unless-stopped' if persistent else '--restart=no',
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
        '--label', f'evonic.persistent={1 if persistent else 0}',
        '-v', f'{effective_workspace}:/workspace:rw',
        *artifacts_mounts,
        '-v', f'{_HELPERS_DIR}:{_HELPERS_MOUNT}:ro',
        '-w', '/workspace',
        '-e', f'SCRATCH={scratch}',
        SANDBOX_IMAGE,
        'sh', '-c', f'mkdir -p {scratch} && exec sleep infinity',
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
        _containers[pool_key] = {
            'container_id': container_id,
            'container_name': name,
            'agent_id': agent_id,
            'last_used': created_at,
            'created_at': created_at,
            'first_call': True,
            'workspace': effective_workspace,
            'mount_version': _MOUNT_LAYOUT_VERSION,
            'persistent': persistent,
            'pool_key': pool_key,
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

    def __init__(self, session_id: str, agent_id: str = '', workspace: str = None,
                 is_subagent: bool = False, is_explorer: bool = False,
                 container_session_id: str = None, container_workspace: str = None,
                 persistent: bool = False):
        self._session_id = session_id
        self._container_session_id = container_session_id or session_id
        self._owns_container = not bool(container_session_id)
        self._agent_id = agent_id
        self._persistent = persistent
        self._workspace = workspace
        self._container_workspace = container_workspace or workspace
        # Normal sub-agents run with cwd = their scratchpad so their relative-path
        # writes stay out of the project root.  Explorer sub-agents have their own
        # explicit workspace and must NOT be redirected to the scratchpad.  The
        # dir is created at container start (see _get_or_create_container).
        self._workdir = scratch_dir(agent_id) if (is_subagent and not is_explorer) else None

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
        effective = os.path.abspath(
            self._container_workspace if self._container_workspace else SANDBOX_WORKSPACE)
        if path == effective or path.startswith(effective + os.sep):
            return '/workspace' + path[len(effective):]
        # The host artifact registry is bind-mounted into the container at
        # /workspace/shared/agents/<id>/artifacts; translate host registry
        # paths to that container path (file tools resolve the sandbox path to
        # the host registry via resolve_workspace_path).
        if self._agent_id and _ARTIFACTS_ROOT:
            registry = os.path.join(_ARTIFACTS_ROOT, self._agent_id, 'artifacts')
            if path == registry or path.startswith(registry + os.sep):
                rel = path[len(registry):]
                return f'/workspace/shared/agents/{self._agent_id}/artifacts{rel}'
        return path

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        # on_output (live streaming) is not supported for Docker exec; output is
        # returned batched. Accepted for API compatibility.
        # Abort if a /stop landed in the race window just before this call.
        if process_tracker.is_stop_pending(self._session_id):
            return {'error': 'Execution stopped by user', 'exit_code': -9, 'execution_time': 0.0}
        container_id, err = _get_or_create_container(
            self._container_session_id, agent_id=self._agent_id,
            workspace=self._container_workspace,
            persistent=self._persistent,
        )
        if err:
            return {'error': err}

        env_args = []
        for k, v in env.items():
            env_args.extend(['-e', f'{k}={v}'])

        workdir_args = ['-w', self._workdir] if self._workdir else []
        cmd = ['exec', '-i'] + workdir_args + env_args + [container_id, 'bash', '-s']
        t0 = time.time()
        proc = _docker_popen(*cmd)
        process_tracker.register(self._session_id, proc, proc.pid, container_id=container_id)
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
        # Abort if a /stop landed in the race window just before this call.
        if process_tracker.is_stop_pending(self._session_id):
            return {'error': 'Execution stopped by user', 'exit_code': -9, 'execution_time': 0.0}
        with _pool_lock:
            info = _containers.get(self._container_session_id, {})
            is_first = info.get('first_call', False)

        container_id, err = _get_or_create_container(
            self._container_session_id, agent_id=self._agent_id,
            workspace=self._container_workspace,
            persistent=self._persistent,
        )
        if err:
            return {'error': err}

        with _pool_lock:
            info = _containers.get(self._container_session_id, {})
            is_first = info.get('first_call', False)

        result = self._run_code(container_id, code, timeout, env)

        if _is_container_gone(result):
            logger.info(
                f'Container {container_id[:12]} gone — recreating for pool session '
                f'{self._container_session_id[:12]}')
            with _pool_lock:
                _containers.pop(self._container_session_id, None)
            container_id, err = _get_or_create_container(
                self._container_session_id, agent_id=self._agent_id,
                workspace=self._container_workspace,
                persistent=self._persistent,
            )
            if err:
                return {'error': err}
            with _pool_lock:
                info = _containers.get(self._container_session_id, {})
                is_first = info.get('first_call', False)
            result = self._run_code(container_id, code, timeout, env)

        if is_first and 'error' not in result:
            with _pool_lock:
                if self._container_session_id in _containers:
                    _containers[self._container_session_id]['first_call'] = False
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

        workdir_args = ['-w', self._workdir] if self._workdir else []
        cmd = ['exec', '-i'] + workdir_args + env_args + [container_id, 'python3', '-']
        t0 = time.time()
        proc = _docker_popen(*cmd)
        process_tracker.register(self._session_id, proc, proc.pid, container_id=container_id)
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
        container_id, err = _get_or_create_container(
            self._container_session_id, agent_id=self._agent_id,
            workspace=self._container_workspace,
            persistent=self._persistent,
        )
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
            'except IsADirectoryError:\n'
            f'    print(json.dumps({{"error": "Path is a directory, not a file: " + {_json.dumps(path)}}}))\n'
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

    def cat_file_bytes(self, path: str) -> dict:
        """Read a file as raw bytes from inside the container.

        Uses ``docker exec`` to stream the file content (base64-encoded for
        binary safety), which can read from tmpfs mounts that ``docker cp``
        cannot access.
        """
        import json as _json, base64 as _b64
        code = (
            'import base64, json\n'
            f'p = {_json.dumps(path)}\n'
            'try:\n'
            '    with open(p, "rb") as f:\n'
            '        data = f.read()\n'
            '    print(json.dumps({"data": base64.b64encode(data).decode("ascii")}))\n'
            'except Exception as e:\n'
            '    print(json.dumps({"error": str(e)}))\n'
        )
        r = self._container_exec_python(code, timeout=60)
        if 'error' in r:
            return r
        try:
            result = _json.loads(r.get('stdout', '{}'))
        except Exception:
            return {'error': 'Failed to parse response from container'}
        if 'error' in result:
            return result
        data = _b64.b64decode(result['data'])
        return {'bytes': data}

    def delete_file(self, path: str) -> dict:
        """Delete a file from inside the container.

        Uses ``docker exec`` to run ``os.remove(path)`` inside the container.
        """
        import json as _json
        code = (
            'import json, os\n'
            f'p = {_json.dumps(path)}\n'
            'try:\n'
            '    os.remove(p)\n'
            '    print(json.dumps({"ok": True}))\n'
            'except FileNotFoundError:\n'
            '    print(json.dumps({"error": "File not found"}))\n'
            'except PermissionError:\n'
            '    print(json.dumps({"error": "Permission denied"}))\n'
            'except IsADirectoryError:\n'
            '    print(json.dumps({"error": "Path is a directory, not a file"}))\n'
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

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        """Write raw bytes to a file inside the Docker container.

        Uses base64-encoded Python exec for binary-safe transfer, same
        pattern as write_file but accepts bytes directly.
        """
        import json as _json, base64 as _b64
        encoded = _b64.b64encode(data).decode('ascii')
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
            f'    print(json.dumps({{\"error\": \"Permission denied writing: \" + {_json.dumps(path)}}}))\n'
            'except IsADirectoryError:\n'
            f'    print(json.dumps({{\"error\": \"Path is a directory: \" + {_json.dumps(path)}}}))\n'
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
            self._container_session_id, agent_id=self._agent_id,
            workspace=self._container_workspace,
            persistent=self._persistent,
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
            self._container_session_id, agent_id=self._agent_id,
            workspace=self._container_workspace,
            persistent=self._persistent,
        )
        if err:
            return {'error': err}
        result = _docker('cp', host_path, f'{container_id}:{container_path}')
        if result.returncode != 0:
            return {'error': result.stderr.strip() or 'docker cp in failed'}
        return {'ok': True}

    def destroy(self) -> dict:
        if not self._owns_container:
            return {'result': 'shared_container_retained'}
        return _destroy_container(self._container_session_id)

    def status(self) -> dict:
        with _pool_lock:
            info = _containers.get(self._container_session_id)
        if info:
            return {
                'backend': 'docker',
                'container_id': info['container_id'][:12],
                'workspace': info.get('workspace'),
                'created_at': info.get('created_at'),
                'last_used': info.get('last_used'),
            }
        return {'backend': 'docker', 'container_id': None, 'detail': 'No container yet (will be created on first use).'}
