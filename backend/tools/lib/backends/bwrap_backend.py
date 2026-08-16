"""
BwrapBackend — lightweight per-agent sandbox via bubblewrap (bwrap).

Used when sandbox_enabled=1 and SANDBOX_BACKEND=bwrap, or for 'bwrap'
workplaces. Linux-only.

Persistent-sandbox model: a long-lived **keeper** process
(`bwrap … sleep infinity`) per session holds the mount/PID/UTS/IPC/user
namespaces; every command joins those namespaces via unprivileged nsenter
(valid because the user namespace is owned by the same uid). The host
rootfs is mounted read-only, the agent workspace is bound rw at
/workspace, and a persistent home lives at /home/agent (backed by
<workspace>/.home on the host). The agent gets its own hostname, PID
namespace, and a private tmpfs /tmp — it "feels like its own OS" with
near-zero overhead: no daemon, no image, just one sleeping process.

Persistence guarantees while the keeper lives:
- Background processes (web servers, tunnels) reparent to bwrap's init
  and survive between commands.
- /tmp is a single tmpfs for the keeper's lifetime, so tmux/screen
  sockets persist and the long_running_guard tmux workflow works.

Lifecycle:
- Session-keyed keepers are destroyed after SANDBOX_IDLE_TIMEOUT of
  inactivity (like Docker containers).
- Workplace keepers (session ids starting with 'workplace-bwrap-') are
  exempt from idle reaping — they live until the workplace is
  disconnected or the server exits.
- --die-with-parent ties every keeper (and its daemons) to the Evonic
  server process: a server restart tears down all sandboxes; no orphan
  processes are possible.

If unprivileged user namespaces are disabled on the host, bwrap fails with
a uid-map error; check `sysctl kernel.unprivileged_userns_clone` (Debian)
or `kernel.apparmor_restrict_unprivileged_userns` (Ubuntu 24.04+).
"""

import atexit
import base64
import hashlib
import json
import logging
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from backend.tools.lib.exec_backend import truncate
from backend.tools.lib.process_tracker import process_tracker
from backend.tools.lib.backends.local_backend import LocalBackend, _MAX_OUTPUT_BYTES
from backend.tools._workspace import scratch_dir

try:
    from config import (
        SANDBOX_WORKSPACE,
        SANDBOX_NETWORK,
        SANDBOX_BWRAP_ULIMIT_V_MB,
        SANDBOX_IDLE_TIMEOUT,
    )
except ImportError:
    SANDBOX_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
    SANDBOX_NETWORK = 'bridge'
    SANDBOX_BWRAP_ULIMIT_V_MB = 0
    SANDBOX_IDLE_TIMEOUT = 1800

logger = logging.getLogger(__name__)

# Directory containing the evonic helper package. Bubblewrap cannot bind this
# path when an ancestor (commonly the service user's home) is mode 0700, so a
# content-addressed copy is staged under a traversable runtime directory first.
_HELPERS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'runpy_helpers'))
_HELPERS_RUNTIME_ROOT = os.path.join(tempfile.gettempdir(), f'evonic-bwrap-{os.getuid()}', 'helpers')
_helpers_stage_lock = threading.Lock()
# Helpers are bound at a top-level path: mount points inside read-only binds
# (/usr, /opt, …) cannot be created by bwrap, but the sandbox root is a tmpfs
# where top-level directories can.
_HELPERS_MOUNT_PARENT = '/evonic-helpers'
_HELPERS_MOUNT = f'{_HELPERS_MOUNT_PARENT}/evonic'

# PATH prefix prepended to every bash script so evonic/bin binaries take priority.
# The rg() wrapper fixes a stdin-inheritance bug: when `bash -s` reads from a pipe,
# child processes inherit that pipe as stdin and rg reads EOF instead of searching.
_EVONIC_BIN = f'{_HELPERS_MOUNT}/bin'
_PATH_PREFIX = (
    f'export PATH={_EVONIC_BIN}:$PATH\n'
    'rg() { if [ ! -t 0 ]; then command rg "$@" .; else command rg "$@"; fi; }\n'
    'export -f rg\n'
)

_HOME_MOUNT = '/home/agent'
_HOME_SUBDIR = '.home'  # host dir under the workspace backing /home/agent

# Host artifact registry root.  save_artifact/list_artifacts/fetch_artifact and
# the web UI all read/write BASE_DIR/shared/agents/<agent_id>/artifacts on the
# HOST.  When an agent's workspace differs from BASE_DIR, the sandbox's
# /workspace bind does NOT include that registry, so /workspace/shared/agents/
# <id>/artifacts would silently resolve to a DIFFERENT directory than the one
# the UI serves.  We bind the registry into the sandbox at the same relative
# path and map it back in _to_host so bash/runpy and file tools stay
# consistent with the host registry (prevents silent artifact divergence).
_ARTIFACTS_ROOT = os.path.normpath(os.path.join(SANDBOX_WORKSPACE, 'shared', 'agents'))

# Bump when the sandbox bind layout changes so existing keepers are recreated
# with the new binds (keepers survive within a process lifetime, so an
# in-memory version check is required to detect stale layouts).
_KEEPER_LAYOUT_VERSION = 2

_USERNS_HINT = (
    ' Hint: bubblewrap needs unprivileged user namespaces — check '
    '`sysctl kernel.unprivileged_userns_clone` (Debian) or '
    '`kernel.apparmor_restrict_unprivileged_userns` (Ubuntu 24.04+).'
)


_NSENTER_READY_TIMEOUT = 1.0
_NSENTER_READY_INTERVAL = 0.01
_NSENTER_EXEC_NOT_READY = 'failed to execute /usr/bin/bash: No such file or directory'


def _helpers_digest(source: str) -> str:
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, source).encode()
            digest.update(rel + b'\0')
            with open(path, 'rb') as handle:
                for chunk in iter(lambda: handle.read(65536), b''):
                    digest.update(chunk)
    return digest.hexdigest()[:16]


def _stage_helpers(source: str = _HELPERS_DIR, runtime_root: str = _HELPERS_RUNTIME_ROOT) -> str:
    """Copy helpers to a traversable, immutable-by-convention bind source."""
    digest = _helpers_digest(source)
    destination = os.path.join(runtime_root, digest)
    with _helpers_stage_lock:
        os.makedirs(runtime_root, mode=0o755, exist_ok=True)
        os.chmod(os.path.dirname(runtime_root), 0o755)
        os.chmod(runtime_root, 0o755)
        if not os.path.isdir(destination):
            staging = tempfile.mkdtemp(prefix=f'.{digest}-', dir=runtime_root)
            try:
                shutil.copytree(source, staging, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                for root, dirs, files in os.walk(staging):
                    os.chmod(root, 0o755)
                    for name in files:
                        path = os.path.join(root, name)
                        os.chmod(path, 0o755 if os.access(path, os.X_OK) else 0o644)
                try:
                    os.rename(staging, destination)
                except FileExistsError:
                    shutil.rmtree(staging)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    return destination


def _nsenter_probe_argv(inner_pid: int) -> list:
    """Build a minimal nsenter probe command — same namespace flags as _nsenter_argv."""
    return ['nsenter', '--preserve-credentials', '-U', '-m', '-u', '-i', '-p',
            '-t', str(inner_pid), '--',
            '/usr/bin/bash', '-c', 'exit 0']


def _wait_for_nsenter_ready(inner_pid: int, timeout: float = _NSENTER_READY_TIMEOUT,
                            interval: float = _NSENTER_READY_INTERVAL):
    """Return ``(probe, failure)`` once a new bwrap child is nsenter-ready.

    Bubblewrap can report its child PID just before its mount setup is visible.
    Retry only that narrow startup signature; all other failures are final.
    """
    deadline = time.monotonic() + timeout
    probe = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return probe, 'readiness'
        try:
            probe = subprocess.run(
                _nsenter_probe_argv(inner_pid), capture_output=True, text=True,
                timeout=max(0.001, remaining),
            )
        except subprocess.TimeoutExpired:
            return None, 'timeout'
        if probe.returncode == 0:
            return probe, None
        stderr = (probe.stderr or '').strip()
        if _NSENTER_EXEC_NOT_READY.lower() not in stderr.lower():
            return probe, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return probe, 'readiness'
        time.sleep(min(interval, remaining))


def _nsenter_failure_message(probe, failure: str | None) -> str | None:
    if failure == 'timeout':
        return 'nsenter probe timed out — the bwrap sandbox may not be responding.'
    if probe is None or probe.returncode == 0:
        return None
    stderr = (probe.stderr or '').strip()
    if failure == 'readiness':
        return (f'The bwrap sandbox did not finish initializing before the nsenter readiness deadline. '
                f'Details: {stderr or "sandbox executable not ready"}.')
    lowered = stderr.lower()
    if any(phrase in lowered for phrase in ('operation not permitted', 'permission denied',
                                              'reassociate to namespace')):
        return (f'nsenter cannot join the bwrap sandbox namespaces on this host. '
                f'Details: {stderr or "permission denied"}. '
                f'The bwrap backend is incompatible with this environment '
                f'(e.g. WSL2 has known namespace limitations).')
    return (f'nsenter failed while checking bwrap sandbox readiness. '
            f'Details: {stderr or "unknown nsenter failure"}.')


def _exited_process_stderr(proc, wait_timeout: float = 0.1) -> str:
    """Return stderr only when a process has exited (or exits promptly)."""
    try:
        if proc.poll() is None:
            proc.wait(timeout=wait_timeout)
        if proc.poll() is None:
            return ''
        return (proc.stderr.read() or '').strip()
    except (AttributeError, OSError, ValueError, subprocess.TimeoutExpired):
        return ''


def _bwrap_startup_failure_message(stderr: str) -> str:
    """Explain a primary bwrap startup failure without blaming nsenter."""
    stderr = (stderr or '').strip()
    match = re.search(r"Can't find source path (.+?): Permission denied(?:\n|$)", stderr,
                      flags=re.IGNORECASE)
    if match:
        source = match.group(1).strip()
        return (f'Bubblewrap could not access bind source {source}: permission denied. '
                f'Check that the Evonic service user has execute/traverse permission on every '
                f'parent directory leading to this source. A private parent directory (for '
                f'example, a home directory with mode 0700) can make the source inaccessible '
                f'after Bubblewrap enters its user namespace. Do not weaken filesystem '
                f'permissions automatically; move or stage the bind source under a suitably '
                f'traversable directory, or adjust permissions according to your security policy. '
                f'Bubblewrap details: {stderr}')
    return f'Bubblewrap keeper exited during startup. Details: {stderr or "no error output"}.'


def _check_nsenter_capability() -> str | None:
    """Probe whether nsenter can actually join all namespaces created by bwrap.

    On some platforms (notably WSL2), ``nsenter -U`` works but entering
    additional namespaces (mount, pid, ipc, uts) fails with
    ``Operation not permitted``.  This function starts a throwaway bwrap
    sandbox running ``sleep infinity`` (same as the real keeper), tries a
    full nsenter probe, and terminates the sandbox immediately.

    Returns an error string on failure, or None if the probe succeeded.
    """
    r_fd, w_fd = os.pipe()
    argv = _AVAILABILITY_PROBE_BWRAP_ARGV + ['--json-status-fd', str(w_fd), 'sleep', 'infinity']
    proc = None
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
            pass_fds=(w_fd,), start_new_session=True,
        )
    except OSError:
        os.close(r_fd)
        os.close(w_fd)
        return None       # bwrap itself failed — _availability_error will report it
    os.close(w_fd)
    inner_pid = _read_child_pid(r_fd, proc)

    if inner_pid is None:
        _destroy_probe(proc, r_fd)
        return None       # bwrap failed — not a nsenter-compatibility issue

    probe, failure = _wait_for_nsenter_ready(inner_pid)
    _destroy_probe(proc, r_fd)
    return _nsenter_failure_message(probe, failure)


def _destroy_probe(proc, r_fd):
    """Kill the probe bwrap process and close its status fd."""
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
            except Exception:
                pass
    try:
        os.close(r_fd)
    except OSError:
        pass


def _availability_error() -> str:
    """Return an error message if bwrap cannot run on this host, else None."""
    if sys.platform != 'linux':
        return (f'bwrap sandbox backend is Linux-only (current platform: {sys.platform}). '
                f'Set SANDBOX_BACKEND=docker or disable the sandbox for this agent.')
    if shutil.which('bwrap') is None:
        return ('bubblewrap is not installed. Install it with: sudo apt install bubblewrap '
                '(Debian/Ubuntu) — or set SANDBOX_BACKEND=docker.')
    if shutil.which('nsenter') is None:
        return ('nsenter (util-linux) is required for the persistent bwrap sandbox. '
                'Install util-linux — or set SANDBOX_BACKEND=docker.')
    # Runtime probe: check that nsenter can actually join the namespaces
    # that bwrap creates.  This catches WSL2 and other environments with
    # broken user-namespace + additional-namespace support.
    nsenter_err = _check_nsenter_capability()
    if nsenter_err:
        return nsenter_err
    return None


# bwrap argv used exclusively for the availability probe (no bind-mounts needed).
_AVAILABILITY_PROBE_BWRAP_ARGV = ['bwrap', '--ro-bind', '/usr', '/usr', '--ro-bind', '/etc', '/etc', '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/sbin', '/sbin', '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--unshare-pid', '--unshare-uts', '--unshare-ipc', '--unshare-user', '--die-with-parent']


def _sanitize_hostname(name: str) -> str:
    h = re.sub(r'[^a-zA-Z0-9-]+', '-', (name or '').lower()).strip('-')[:63]
    return h or 'agent'


# ---------------------------------------------------------------------------
# Module-level keeper pool (shared across all BwrapBackend instances —
# the registry constructs a fresh instance per tool call, so keeper state
# must not live on the instance)
# ---------------------------------------------------------------------------

_WORKPLACE_PREFIX = 'workplace-bwrap-'   # exempt from idle reaping
_KEEPER_SPAWN_TIMEOUT = 10.0             # seconds to wait for the child-pid JSON line

_keepers: dict = {}   # session_id -> {proc, inner_pid, status_fd, created_at, last_used, workspace, hostname}
_pool_lock = threading.Lock()
_reaper_thread: threading.Thread = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _keeper_alive(info: dict) -> bool:
    return info['proc'].poll() is None and _pid_alive(info['inner_pid'])


def _stale_sessions(now: float) -> list:
    """Session ids whose keeper is idle past the timeout (workplaces exempt)."""
    deadline = now - SANDBOX_IDLE_TIMEOUT
    with _pool_lock:
        return [sid for sid, info in _keepers.items()
                if not sid.startswith(_WORKPLACE_PREFIX) and info['last_used'] < deadline]


def _destroy_keeper(session_id: str) -> dict:
    with _pool_lock:
        info = _keepers.pop(session_id, None)
    if info is None:
        return {'result': 'no_keeper', 'detail': 'No active sandbox for this session.'}
    proc = info['proc']
    try:
        # Killing bwrap kills the namespace init (PID 1), and the kernel then
        # kills every process in the namespace — daemons included.
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.close(info['status_fd'])
    except OSError:
        pass
    return {'result': 'sandbox_destroyed',
            'detail': 'bwrap keeper and all sandbox processes terminated.'}


def _ensure_reaper_running() -> None:
    global _reaper_thread
    with _pool_lock:
        if _reaper_thread is not None and _reaper_thread.is_alive():
            return
    t = threading.Thread(target=_reaper_loop, daemon=True, name='bwrap-backend-reaper')
    t.start()
    with _pool_lock:
        _reaper_thread = t


def _reaper_loop() -> None:
    while True:
        time.sleep(60)
        try:
            for sid in _stale_sessions(time.time()):
                logger.info(f'Idle timeout — destroying bwrap keeper for session {sid[:12]}')
                _destroy_keeper(sid)
        except Exception:
            logger.error('bwrap reaper loop error', exc_info=True)


@atexit.register
def _cleanup_all() -> None:
    with _pool_lock:
        sids = list(_keepers.keys())
    for sid in sids:
        _destroy_keeper(sid)


def get_pool_status() -> dict:
    """Return current keeper pool state for monitoring/debugging."""
    with _pool_lock:
        keepers = [{
            'session_id': sid[:24],
            'inner_pid': info['inner_pid'],
            'alive': _keeper_alive(info),
            'created_at': info['created_at'],
            'last_used': info['last_used'],
            'workspace': info.get('workspace'),
            'hostname': info.get('hostname'),
        } for sid, info in _keepers.items()]
    return {'pool_size': len(keepers), 'idle_timeout': SANDBOX_IDLE_TIMEOUT, 'keepers': keepers}


def _read_child_pid(fd: int, proc, timeout: float = _KEEPER_SPAWN_TIMEOUT):
    """Read JSON lines from bwrap's --json-status-fd until {"child-pid": N} appears.

    Returns the host PID of the sandboxed command, or None on
    timeout/EOF/bwrap death.
    """
    deadline = time.time() + timeout
    buf = b''
    while time.time() < deadline:
        try:
            rlist, _, _ = select.select([fd], [], [], 0.5)
        except (ValueError, OSError):
            return None
        if not rlist:
            if proc is not None and proc.poll() is not None:
                return None            # bwrap died before reporting
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return None
        if chunk == b'':
            return None                # EOF without a child-pid line
        buf += chunk
        for line in buf.split(b'\n'):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if 'child-pid' in obj:
                return int(obj['child-pid'])
    return None


def _nsenter_argv(inner_pid: int, workdir: str, inner: list, ulimit_v_kb: int = 0) -> list:
    """Build the command that joins the keeper's namespaces and runs *inner*.

    util-linux 2.37 (Ubuntu 22.04) has no `nsenter --wd`, and entering the
    mount namespace resets the cwd to /, so the workdir is set by a bash
    trampoline. nsenter -p makes the forked child enter the PID namespace;
    zombies inside the sandbox are reaped by bwrap's init.
    """
    # /tmp is a fresh tmpfs at sandbox start, so the scratchpad workdir may not
    # exist yet — create it before cd (harmless for /workspace, which exists).
    setup = f'mkdir -p {shlex.quote(workdir)} 2>/dev/null; cd {shlex.quote(workdir)} || exit 127'
    if ulimit_v_kb > 0:
        setup += f'; ulimit -v {ulimit_v_kb} 2>/dev/null'
    return ['nsenter', '--preserve-credentials', '-U', '-m', '-u', '-i', '-p',
            '-t', str(inner_pid), '--',
            'bash', '-c', f'{setup}; exec "$@"', 'evonic-exec'] + inner


_KEEPER_GONE_PHRASES = ('no such process', 'cannot open /proc')


def _is_keeper_gone(result: dict) -> bool:
    """Detect nsenter failures caused by the keeper having died."""
    if 'error' not in result and result.get('exit_code', 0) == 0:
        return False
    combined = (result.get('stderr', '') + result.get('stdout', '')
                + result.get('error', '')).lower()
    return 'nsenter:' in combined and any(p in combined for p in _KEEPER_GONE_PHRASES)


class BwrapBackend(LocalBackend):
    """Executes bash/python inside a persistent bubblewrap namespace sandbox.

    Subclasses LocalBackend (with run_as_user=None) to reuse its
    _poll_proc / _run_bash_streaming machinery and its host-side (non-sudo)
    file I/O — the workspace is a plain host directory, so file operations
    only need the sandbox-view → host path reverse mapping in _to_host().
    """

    def __init__(self, session_id: str = '', workspace: str = None,
                 agent_id: str = '', agent_name: str = '', is_subagent: bool = False,
                 is_explorer: bool = False):
        super().__init__(session_id=session_id, workspace=workspace, run_as_user=None)
        self._agent_id = agent_id
        self._hostname = _sanitize_hostname(agent_name or agent_id)
        self._is_subagent = is_subagent
        self._is_explorer = is_explorer
        self._dirs_ready = False

    # ------------------------------------------------------------------
    # Sandbox construction
    # ------------------------------------------------------------------

    def _ensure_dirs(self):
        if self._dirs_ready:
            return
        ws = self._cwd()
        os.makedirs(os.path.join(ws, _HOME_SUBDIR), exist_ok=True)
        # The scratchpad lives in the sandbox's tmpfs /tmp (created lazily by the
        # nsenter cd trampoline), not in the bound workspace — nothing to mkdir here.
        self._dirs_ready = True

    def _bwrap_argv(self, workspace_fd: int = None, home_fd: int = None) -> list:
        ws = self._cwd()
        helpers_dir = _stage_helpers()
        argv = [
            'bwrap',
            '--ro-bind', '/usr', '/usr',
            '--ro-bind', '/etc', '/etc',
            '--ro-bind-try', '/opt', '/opt',
        ]
        # Merged-usr distros (Debian/Ubuntu) have /bin -> usr/bin symlinks;
        # recreate them as symlinks, fall back to ro-binds on split-usr hosts.
        for d in ('/bin', '/sbin', '/lib', '/lib64'):
            if os.path.islink(d):
                argv += ['--symlink', 'usr' + d, d]
            elif os.path.isdir(d):
                argv += ['--ro-bind', d, d]
        # On systemd-resolved/resolvconf hosts /etc/resolv.conf is a symlink
        # into /run, which is not mounted — bind the real file at its target
        # path so DNS resolution works inside the sandbox.
        resolv = os.path.realpath('/etc/resolv.conf')
        if resolv != '/etc/resolv.conf':
            argv += ['--ro-bind-try', resolv, resolv]
        workspace_bind = (['--bind-fd', str(workspace_fd), '/workspace'] if workspace_fd is not None
                          else ['--bind', ws, '/workspace'])
        home_bind = (['--bind-fd', str(home_fd), _HOME_MOUNT] if home_fd is not None
                     else ['--bind', os.path.join(ws, _HOME_SUBDIR), _HOME_MOUNT])
        # Bind the agent's host artifact registry into the sandbox at the same
        # relative path the sandbox-visible path convention uses, so that
        # /workspace/shared/agents/<id>/artifacts/ always points at the SAME
        # directory the web UI / list_artifacts / fetch_artifact serve.
        # Without this, agents whose workspace differs from BASE_DIR would
        # silently append to a sandbox copy the UI never reads (artifact
        # divergence bug).  The destination dir is created inside the sandbox
        # (bwrap creates missing mount points); the host source is ensured by
        # _ensure_dirs on the host side.
        artifacts_binds = []
        if self._agent_id and _ARTIFACTS_ROOT:
            registry_dir = os.path.join(_ARTIFACTS_ROOT, self._agent_id, 'artifacts')
            # Skip when the workspace bind already exposes the registry at the
            # same sandbox path (workspace == BASE_DIR): binding a directory
            # onto itself is redundant.
            ws_relative = os.path.join(ws, 'shared', 'agents',
                                       self._agent_id, 'artifacts')
            if os.path.realpath(ws_relative) != os.path.realpath(registry_dir):
                os.makedirs(registry_dir, exist_ok=True)
                artifacts_binds = [
                    '--bind', registry_dir, f'/workspace/shared/agents/{self._agent_id}/artifacts',
                ]
        argv += [
            '--proc', '/proc',
            '--dev', '/dev',
            '--tmpfs', '/tmp',
            *workspace_bind,
            *artifacts_binds,
            *home_bind,
            '--ro-bind', helpers_dir, _HELPERS_MOUNT,
            '--unshare-pid', '--unshare-uts', '--unshare-ipc', '--unshare-user',
            '--hostname', self._hostname,
            '--die-with-parent',
        ]
        if SANDBOX_NETWORK == 'none':
            argv += ['--unshare-net']
        return argv

    def _base_env(self, env: dict) -> dict:
        # Deliberately NOT inherited from os.environ — the sandbox must not
        # see server secrets. Minimal explicit environment only.
        run_env = {
            'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': _HOME_MOUNT,
            'USER': 'agent',
            'LOGNAME': 'agent',
            'HOSTNAME': self._hostname,
            'TMPDIR': '/tmp',
            'LANG': 'C.UTF-8',
            'TERM': 'dumb',
            'SCRATCH': scratch_dir(self._agent_id),
        }
        run_env.update(env)
        return run_env

    @staticmethod
    def _bash_prefix() -> str:
        prefix = _PATH_PREFIX
        if SANDBOX_BWRAP_ULIMIT_V_MB > 0:
            prefix = f'ulimit -v {SANDBOX_BWRAP_ULIMIT_V_MB * 1024} 2>/dev/null\n' + prefix
        return prefix

    def _workdir(self) -> str:
        # Normal sub-agents run with cwd = their scratchpad so their relative-path
        # writes stay out of the project root.  Explorer sub-agents have their own
        # explicit workspace and must NOT be redirected.  The scratchpad is created
        # by the nsenter cd trampoline (tmpfs /tmp is empty at sandbox start).
        return scratch_dir(self._agent_id) if (self._is_subagent and not self._is_explorer) else '/workspace'

    def _subprocess_identity_kwargs(self) -> dict:
        if os.geteuid() != 0:
            return {}
        workspace_stat = os.stat(self._cwd())
        return {'user': workspace_stat.st_uid, 'group': workspace_stat.st_gid,
                'extra_groups': ()}

    # ------------------------------------------------------------------
    # Keeper lifecycle
    # ------------------------------------------------------------------

    def _spawn_keeper(self):
        """Start `bwrap … --json-status-fd W sleep infinity`.

        Returns (info_dict, None) on success or (None, error_message).
        """
        self._ensure_dirs()
        ws = self._cwd()
        bind_fds = []
        try:
            bind_fds = [
                os.open(ws, os.O_RDONLY | os.O_DIRECTORY),
                os.open(os.path.join(ws, _HOME_SUBDIR), os.O_RDONLY | os.O_DIRECTORY),
            ]
        except OSError as e:
            for fd in bind_fds:
                os.close(fd)
            return None, f'Failed to open bwrap bind source: {e}'
        r_fd, w_fd = os.pipe()
        cmd = self._bwrap_argv(*bind_fds) + ['--json-status-fd', str(w_fd), 'sleep', 'infinity']
        # A descriptor bypasses pathname traversal, but bwrap still opens
        # /proc/self/fd/N after entering its user namespace. If Evonic runs as
        # root and the private workspace belongs to another user, that owner is
        # mapped to nobody and the reopened directory remains inaccessible.
        # Launch bwrap as the workspace owner so its user namespace maps the
        # bind source correctly and preserves normal workspace write access.
        popen_kwargs = self._subprocess_identity_kwargs()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True,
                env=self._base_env({}),
                pass_fds=(w_fd, *bind_fds), start_new_session=True,
                **popen_kwargs,
            )
        except OSError as e:
            os.close(r_fd)
            os.close(w_fd)
            return None, f'Failed to start bwrap keeper: {e}'
        finally:
            for fd in bind_fds:
                os.close(fd)
        os.close(w_fd)  # our copy; bwrap holds its own
        inner_pid = _read_child_pid(r_fd, proc)
        if inner_pid is None:
            stderr = _exited_process_stderr(proc)
            _destroy_probe(proc, r_fd)
            if stderr:
                return None, f'bwrap keeper failed to start: {_bwrap_startup_failure_message(stderr)}'
            return None, 'bwrap keeper failed to start: no child-pid reported'
        probe, failure = _wait_for_nsenter_ready(inner_pid)
        readiness_error = _nsenter_failure_message(probe, failure)
        if readiness_error:
            # bwrap may report its child PID and then fail while mounting sources.
            # Its stderr is the primary cause; nsenter only sees the vanished child.
            stderr = _exited_process_stderr(proc)
            _destroy_probe(proc, r_fd)
            if stderr:
                return None, f'bwrap keeper failed to start: {_bwrap_startup_failure_message(stderr)}'
            return None, f'bwrap keeper failed readiness check: {readiness_error}'
        now = time.time()
        # Keep r_fd open in the pool entry (closed in _destroy_keeper) so
        # bwrap never hits a broken pipe when writing its exit-status line.
        return {'proc': proc, 'inner_pid': inner_pid, 'status_fd': r_fd,
                'created_at': now, 'last_used': now,
                'workspace': self._cwd(), 'hostname': self._hostname,
                'layout_version': _KEEPER_LAYOUT_VERSION}, None

    def _get_or_create_keeper(self):
        """Return (inner_pid, None) or (None, error_message)."""
        ws = self._cwd()
        stale = False
        with _pool_lock:
            info = _keepers.get(self._session_id)
            if info is not None:
                if info['workspace'] != ws:
                    logger.info(f'Workspace changed for session {self._session_id[:12]} — recreating keeper')
                    stale = True
                elif info.get('layout_version') != _KEEPER_LAYOUT_VERSION:
                    logger.info(f'Sandbox layout changed for session {self._session_id[:12]} — recreating keeper')
                    stale = True
                elif not _keeper_alive(info):
                    logger.warning(f'bwrap keeper vanished for session {self._session_id[:12]} — recreating')
                    stale = True
                else:
                    info['last_used'] = time.time()
                    return info['inner_pid'], None
        if stale:
            _destroy_keeper(self._session_id)
        with _pool_lock:
            info = _keepers.get(self._session_id)  # re-check after re-acquire
            if info is not None and _keeper_alive(info) and info['workspace'] == ws \
                    and info.get('layout_version') == _KEEPER_LAYOUT_VERSION:
                info['last_used'] = time.time()
                return info['inner_pid'], None
            new_info, err = self._spawn_keeper()
            if err:
                return None, err
            _keepers[self._session_id] = new_info
        _ensure_reaper_running()
        return new_info['inner_pid'], None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _exec(self, cmd: list, input_data: str, timeout: int, run_env: dict) -> dict:
        t0 = time.time()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self._cwd(), env=run_env,
            start_new_session=True,
            **self._subprocess_identity_kwargs(),
        )
        process_tracker.register(self._session_id, proc, proc.pid,
                                 kill_method='killpg')
        try:
            stdout, stderr, reason = self._poll_proc(proc, input_data, timeout, t0)
            if stdout is None:
                elapsed = round(time.time() - t0, 3)
                if reason == 'timeout':
                    return {
                        'error': f'Execution timed out after {timeout}s',
                        'exit_code': -1,
                        'execution_time': elapsed,
                    }
                was_user_stop = not process_tracker.is_registered(self._session_id)
                if was_user_stop:
                    return {
                        'error': 'Execution stopped by user',
                        'exit_code': -9,
                        'execution_time': elapsed,
                    }
                sig = -proc.returncode if proc.returncode else 'unknown'
                return {
                    'error': f'Process killed by signal {sig}. This may happen when a command requires interactive input that cannot be provided in this environment.',
                    'exit_code': proc.returncode or -9,
                    'execution_time': elapsed,
                }
        finally:
            process_tracker.unregister(self._session_id)
        elapsed = round(time.time() - t0, 3)
        if proc.returncode != 0 and 'bwrap:' in (stderr or ''):
            stderr += _USERNS_HINT
        return {
            'stdout': truncate(stdout, _MAX_OUTPUT_BYTES),
            'stderr': truncate(stderr, _MAX_OUTPUT_BYTES),
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    def _exec_in_keeper(self, inner: list, input_data: str, timeout: int,
                        run_env: dict, on_output=None, ulimit_v_kb: int = 0) -> dict:
        inner_pid, err = self._get_or_create_keeper()
        if err:
            return {'error': err, 'exit_code': -1, 'execution_time': 0}
        cmd = _nsenter_argv(inner_pid, self._workdir(), inner, ulimit_v_kb)
        if on_output is not None:
            return self._run_bash_streaming(cmd, input_data, timeout, run_env,
                                            time.time(), on_output)
        return self._exec(cmd, input_data, timeout, run_env)

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        err = _availability_error()
        if err:
            return {'error': err, 'exit_code': -1, 'execution_time': 0}
        self._ensure_dirs()
        run_env = self._base_env(env)
        prefixed = self._bash_prefix() + script
        result = self._exec_in_keeper(['bash', '-s'], prefixed, timeout, run_env, on_output)
        if _is_keeper_gone(result):
            # Keeper died mid-race: recreate once, retry once.
            _destroy_keeper(self._session_id)
            result = self._exec_in_keeper(['bash', '-s'], prefixed, timeout, run_env, on_output)
        return result

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        err = _availability_error()
        if err:
            return {'error': err, 'exit_code': -1, 'execution_time': 0}
        self._ensure_dirs()
        run_env = self._base_env(env)
        run_env['PYTHONPATH'] = _HELPERS_MOUNT_PARENT
        ulimit_v_kb = SANDBOX_BWRAP_ULIMIT_V_MB * 1024 if SANDBOX_BWRAP_ULIMIT_V_MB > 0 else 0
        result = self._exec_in_keeper(['python3', '-'], code, timeout, run_env,
                                      ulimit_v_kb=ulimit_v_kb)
        if _is_keeper_gone(result):
            _destroy_keeper(self._session_id)
            result = self._exec_in_keeper(['python3', '-'], code, timeout, run_env,
                                          ulimit_v_kb=ulimit_v_kb)
        return result

    # ------------------------------------------------------------------
    # Path resolution & file I/O
    # ------------------------------------------------------------------

    def resolve_path(self, path: str) -> str:
        """Convert a host filesystem path to the sandbox's /workspace view."""
        effective = self._cwd()
        if path.startswith(effective):
            return '/workspace' + path[len(effective):]
        # The host artifact registry is bind-mounted into the sandbox at
        # /workspace/shared/agents/<id>/artifacts; translate host registry
        # paths to that sandbox path (file tools resolve the sandbox path to
        # the host registry via resolve_workspace_path; _to_host maps back).
        if self._agent_id and _ARTIFACTS_ROOT:
            registry = os.path.join(_ARTIFACTS_ROOT, self._agent_id, 'artifacts')
            if path == registry or path.startswith(registry + os.sep):
                rel = path[len(registry):]
                return f'/workspace/shared/agents/{self._agent_id}/artifacts{rel}'
        return path

    def _to_host(self, path: str) -> str:
        """Reverse-map a sandbox-view path back to the host filesystem.

        File tools call resolve_path() first and hand us the sandbox view;
        the workspace and home are plain host directories, so file I/O runs
        host-side (LocalBackend non-sudo paths) on the mapped location.
        Other paths pass through as-is — note that /tmp, while persistent
        for the keeper's lifetime, is invisible to host-side file tools;
        agents should keep tool-visible files under /workspace or /home/agent.
        """
        ws = self._cwd()
        # The artifact registry is bind-mounted into the sandbox at
        # /workspace/shared/agents/<id>/artifacts; map sandbox-view paths
        # under that prefix back to the HOST registry, not a workspace copy.
        if self._agent_id and _ARTIFACTS_ROOT:
            artifacts_rel = f'/workspace/shared/agents/{self._agent_id}/artifacts'
            if path == artifacts_rel or path.startswith(artifacts_rel + '/'):
                registry = os.path.join(_ARTIFACTS_ROOT, self._agent_id, 'artifacts')
                return registry + path[len(artifacts_rel):]
        if path == '/workspace' or path.startswith('/workspace/'):
            return ws + path[len('/workspace'):]
        if path == _HOME_MOUNT or path.startswith(_HOME_MOUNT + '/'):
            return os.path.join(ws, _HOME_SUBDIR) + path[len(_HOME_MOUNT):]
        return path

    def _is_scratch_path(self, path: str) -> bool:
        root = scratch_dir(self._agent_id)
        return path == root or path.startswith(root + '/')

    def _scratch_python(self, code: str) -> dict:
        result = self.run_python(code, 30, {})
        if result.get('error') or result.get('exit_code', 0) != 0:
            return {'error': result.get('error') or result.get('stderr', 'sandbox file operation failed')}
        try:
            return json.loads(result.get('stdout', '{}'))
        except (TypeError, ValueError):
            return {'error': 'Invalid response from sandbox file operation'}

    @staticmethod
    def _scratch_operation(path: str, operation: str, **values) -> str:
        return (
            'import base64, json, os\n'
            f'p = {path!r}\n'
            f'op = {operation!r}\n'
            f'values = {values!r}\n'
            'try:\n'
            "    if op == 'exists': result = os.path.exists(p)\n"
            "    elif op == 'stat':\n"
            "        exists = os.path.isfile(p)\n"
            "        chunk = open(p, 'rb').read(8192) if exists else b''\n"
            "        result = {'exists': exists, 'size': os.path.getsize(p) if exists else 0, 'is_binary': b'\\x00' in chunk}\n"
            "    elif op == 'read':\n"
            "        with open(p, encoding='utf-8') as f: result = {'content': f.read()}\n"
            "    elif op == 'mkdir': os.makedirs(p, exist_ok=True); result = {'ok': True}\n"
            "    elif op == 'delete': os.remove(p); result = {'ok': True}\n"
            "    elif op == 'read_bytes':\n"
            "        with open(p, 'rb') as f: result = {'data': base64.b64encode(f.read()).decode('ascii')}\n"
            "    elif op == 'write_bytes':\n"
            "        if values['create_dirs']: os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "        with open(p, 'wb') as f: f.write(base64.b64decode(values['data']))\n"
            "        result = {'ok': True}\n"
            "    print(json.dumps(result))\n"
            'except Exception as e: print(json.dumps({\'error\': str(e)}))\n'
        )

    def file_exists(self, path: str) -> bool:
        if not self._is_scratch_path(path):
            return super().file_exists(self._to_host(path))
        return self._scratch_python(self._scratch_operation(path, 'exists')) is True

    def file_stat(self, path: str) -> dict:
        if not self._is_scratch_path(path):
            return super().file_stat(self._to_host(path))
        return self._scratch_python(self._scratch_operation(path, 'stat'))

    def read_file(self, path: str) -> dict:
        if not self._is_scratch_path(path):
            return super().read_file(self._to_host(path))
        return self._scratch_python(self._scratch_operation(path, 'read'))

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        if not self._is_scratch_path(path):
            return super().write_file(self._to_host(path), content, create_dirs)
        return self.write_file_bytes(path, content.encode('utf-8'), create_dirs)

    def make_dirs(self, path: str) -> dict:
        if not self._is_scratch_path(path):
            return super().make_dirs(self._to_host(path))
        return self._scratch_python(self._scratch_operation(path, 'mkdir'))

    def cat_file_bytes(self, path: str) -> dict:
        if not self._is_scratch_path(path):
            return super().cat_file_bytes(self._to_host(path))
        result = self._scratch_python(self._scratch_operation(path, 'read_bytes'))
        if 'data' in result:
            return {'bytes': base64.b64decode(result['data'])}
        return result

    def delete_file(self, path: str) -> dict:
        if not self._is_scratch_path(path):
            return super().delete_file(self._to_host(path))
        return self._scratch_python(self._scratch_operation(path, 'delete'))

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        if not self._is_scratch_path(path):
            return super().write_file_bytes(self._to_host(path), data, create_dirs)
        return self._scratch_python(self._scratch_operation(
            path, 'write_bytes', data=base64.b64encode(data).decode('ascii'), create_dirs=create_dirs,
        ))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def destroy(self) -> dict:
        return _destroy_keeper(self._session_id)

    def status(self) -> dict:
        err = _availability_error()
        info = {
            'backend': 'bwrap',
            'workspace': self._cwd(),
            'hostname': self._hostname,
            'available': err is None,
            'detail': ('persistent sandbox; background processes survive between commands; '
                       f'{_HOME_MOUNT} persists at <workspace>/{_HOME_SUBDIR}'),
        }
        with _pool_lock:
            keeper = _keepers.get(self._session_id)
        if keeper is not None:
            info['keeper'] = 'running' if _keeper_alive(keeper) else 'dead'
            info['inner_pid'] = keeper['inner_pid']
            info['uptime_s'] = round(time.time() - keeper['created_at'], 1)
        else:
            info['keeper'] = 'not_started'
        if err:
            info['error'] = err
        return info
