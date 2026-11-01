"""
PodmanBackend — runs bash and Python inside a Podman container.

Selected with ``SANDBOX_BACKEND=podman``. It is the daemonless counterpart of
:mod:`backend.tools.lib.backends.docker_backend`: same container image, same
mounts, same labels, same bash/runpy/file-I/O contract, but driven through the
``podman`` CLI, which usually runs rootless and needs no Docker daemon.

Implementation strategy
-----------------------
``DockerBackend`` subclassed, not forked: bash/python execution, file I/O,
path resolution, the process tracker integration and the output truncation are
inherited unchanged. What differs is rebound here:

* ``_docker`` / ``_docker_popen`` — point at the ``podman`` binary (the parent
  resolves these through module globals, so a subclass replaces them cleanly).
* ``_effective_image`` / ``_restart_policy`` — the parent's overridable hooks,
  so shared container-creation code picks the Podman image and a restart
  policy rootless Podman can honour.
* ``_sibling_runtime_names`` — keeps the inherited sweep/reconcile loops away
  from containers owned by another runtime loaded in the same process.
* ``_PODMAN_USERNS`` / ``_PODMAN_USER`` — the user-namespace mapping without
  which the sandbox user cannot write to the bind-mounted workspace (see
  *Rootless notes*).

Runtime exclusivity
-------------------
The container pool, the idle reaper, the startup sweep and the atexit cleanup
are process-global in the parent module. Mixing Docker and Podman in one process
would therefore let that shared cleanup delete containers it does not own, so
``_claim_runtime('podman')`` refuses to start when the Docker pool is already
active (and ``DockerBackend`` mirrors the check). The exception is containers
belonging to a sibling runtime that is merely *loaded*: those are never swept,
so an in-process simulation cannot delete the live containers.

Rootless notes
--------------
* ``--restart=unless-stopped`` requires a systemd cgroup manager and is refused
  by rootless Podman in most setups, so persistent containers are created with
  ``--restart=no``. They still survive ``evonic`` restarts: they are stopped (not
  removed) on shutdown and restarted by the startup sweep on the next boot.
* ``--security-opt label=disable`` is passed by default. On SELinux-enforcing
  hosts (Fedora, RHEL, ...) a bind mount keeps the host label of the source
  directory (``user_tmp_t`` for anything under ``/tmp``) and the container is
  denied write access to it even as root. Disabling the container label
  confinement for the sandbox is the same posture Docker uses on these hosts.
  Override with ``SANDBOX_PODMAN_SECURITY_OPT`` (empty value drops the flag,
  ``label=type:container_file_t`` re-enables labelling).
* The user namespace must be mapped explicitly. Rootless Podman maps the
  container to a *subordinate* uid by default, so the bind-mounted workspace
  appears owned by ``root`` inside the container and the sandbox user gets
  ``Permission denied`` on every write, which makes the sandbox unusable. The
  default here is ``--userns=keep-id:uid=<image uid>,gid=<image gid>``: it maps
  the image's own uid onto the host uid, which is exactly what the bind mount
  needs, and it does NOT force Podman to ID-map the image layers (fast start,
  no ``not enough unused IDs in user namespace`` failure). A plain
  ``--userns=keep-id`` is not used because it does replay the image through the
  ID-mapping machinery. Override with ``SANDBOX_PODMAN_USERNS`` (empty value
  drops the flag), and the ids with ``SANDBOX_PODMAN_USER`` (default ``1000``).
* ``--memory`` / ``--cpus`` need cgroups v2; when Podman cannot enforce them the
  container still starts (Podman only warns). Use a rootful Podman setup if hard
  limits are mandatory.
* ``--network=bridge`` maps to ``pasta`` / ``slirp4netns`` in rootless mode.
"""

import atexit
import logging
import os
import shutil
import subprocess
import threading
import time

from backend.tools.lib.backends import docker_backend
from backend.tools.lib.backends.docker_backend import (  # noqa: F401  (re-exported)
    _ARTIFACTS_ROOT,
    _MOUNT_LAYOUT_VERSION,
)
from backend.tools._workspace import scratch_dir

logger = logging.getLogger(__name__)

try:
    from config import (
        SANDBOX_WORKSPACE,
        SANDBOX_MEMORY_LIMIT,
        SANDBOX_CPU_LIMIT,
        SANDBOX_NETWORK,
        SANDBOX_MAX_CONTAINERS,
        SANDBOX_PODMAN_IMAGE,
    )
except ImportError:
    SANDBOX_WORKSPACE = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
    SANDBOX_MEMORY_LIMIT = '512m'
    SANDBOX_CPU_LIMIT = '1'
    SANDBOX_NETWORK = 'bridge'
    SANDBOX_MAX_CONTAINERS = 10
    SANDBOX_PODMAN_IMAGE = 'localhost/evonic-sandbox:latest'

# Runtime identity. ``_RUNTIME_NAME`` is read by the shared pool bookkeeping in
# the parent module; ``_RUNTIME_CLI`` is the binary this backend shells out to.
_RUNTIME_NAME = 'podman'
_WORKSPACE_MOUNT = '/workspace'
_RUNTIME_CLI = os.getenv('SANDBOX_PODMAN_CLI', 'podman').strip() or 'podman'
PODMAN_CLI = _RUNTIME_CLI

# Rootless Podman cannot use ``--restart=unless-stopped`` (it needs a systemd
# cgroup manager). Persistent containers are stopped on shutdown and restarted by
# the startup sweep instead, so installed state still survives restarts.
_PODMAN_RESTART_POLICY = 'no'

# SELinux: a bind mount keeps the source's host label, so a container cannot
# write to the mounted workspace on enforcing hosts. Disabling label separation
# for the sandbox mirrors what Docker does there. Empty value drops the flag.
_PODMAN_SECURITY_OPT = os.getenv('SANDBOX_PODMAN_SECURITY_OPT', 'label=disable').strip()

# User-namespace mode. The default is the *mapped* keep-id form built from
# ``_PODMAN_USER``: it maps the image user onto the host uid without replaying
# the image layers, so the workspace bind mount stays writable. Set
# SANDBOX_PODMAN_USERNS to override it, or to an empty value to fall back to
# Podman's (broken for bind mounts) subuid mapping.
_PODMAN_USERNS_TEMPLATE = 'keep-id:uid={uid},gid={gid}'
_PODMAN_USERNS = os.getenv('SANDBOX_PODMAN_USERNS', '').strip()
_PODMAN_USER = os.getenv('SANDBOX_PODMAN_USER', '1000').strip() or '1000'

_ALREADY_EXISTS_MARKERS = (
    'already exists',
    'already in use',
    'name is already in use',
    'container name',
)

# Errors that mean "this container is gone" and a retry should recreate it.
_CONTAINER_GONE_PHRASES = tuple(docker_backend._CONTAINER_GONE_PHRASES) + (
    'no such container',
    'container not found',
)


# ---------------------------------------------------------------------------
# Module-level container pool (Podman-only; independent from the Docker pool)
# ---------------------------------------------------------------------------

_containers: dict = {}
_pool_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CLI helpers (rebound into the parent module by bind())
# ---------------------------------------------------------------------------

def _runtime(*args, input_data: str = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a podman subcommand (mirror of ``docker_backend._docker``)."""
    return subprocess.run(
        [_RUNTIME_CLI] + list(args),
        input=input_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _runtime_popen(*args) -> subprocess.Popen:
    """Popen variant of :func:`_runtime` for interruptible execution."""
    return subprocess.Popen(
        [_RUNTIME_CLI] + list(args),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def cli_available() -> bool:
    """Return True when the podman binary is on PATH."""
    return shutil.which(_RUNTIME_CLI) is not None


def _userns_arg() -> str:
    """Return the ``--userns`` value for new sandbox containers.

    Defaults to ``keep-id:uid=<_PODMAN_USER>,gid=<_PODMAN_USER>`` so that the
    image user maps onto the host uid and the bind-mounted workspace is
    writable. ``SANDBOX_PODMAN_USERNS`` overrides it; an explicit empty value
    disables the flag.
    """
    if _PODMAN_USERNS:
        return _PODMAN_USERNS
    return _PODMAN_USERNS_TEMPLATE.format(uid=_PODMAN_USER, gid=_PODMAN_USER)


def workspace_writable(container_id: str, path: str = None, timeout: int = 20) -> tuple:
    """Verify the sandbox user can actually write to the mounted workspace.

    A rootless user-namespace mismatch shows up as a read-only workspace, not as
    a failed ``podman run``, so the backend checks it once per container and
    fails loudly instead of handing an unusable sandbox to the agent.
    """
    path = path or _WORKSPACE_MOUNT
    probe = path + '/.evonic-write-probe'
    try:
        r = _runtime('exec', container_id, 'sh', '-c',
                     f': > {probe} && rm -f {probe}', timeout=timeout)
    except Exception as exc:  # binary missing, timeout, ...
        return False, str(exc)
    if r.returncode == 0:
        return True, 'ok'
    return False, ((r.stderr or r.stdout or '').strip()
                   or f'sandbox user cannot write to {path}')


def runtime_reachable(timeout: int = 10) -> tuple:
    """Return ``(ok, detail)`` for the local Podman engine.

    ``podman info`` is the cheapest call that proves the engine really works
    (rootless storage initialised, no broken socket/config).
    """
    if not cli_available():
        return False, f'{_RUNTIME_CLI!r} binary not found on PATH'
    try:
        r = subprocess.run([_RUNTIME_CLI, 'info', '--format', '{{.Host.OCIRuntime.Name}}'],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f'{_RUNTIME_CLI} info timed out after {timeout}s'
    except OSError as exc:
        return False, f'cannot execute {_RUNTIME_CLI!r}: {exc}'
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f'{_RUNTIME_CLI} info failed'
    return True, (r.stdout.strip() or 'ok')


def _image_available(image: str = None, timeout: int = 30) -> tuple:
    """Return ``(present, detail)`` for the sandbox image in local storage."""
    image = image or SANDBOX_PODMAN_IMAGE
    try:
        r = _runtime('image', 'exists', image, timeout=timeout)
    except Exception as exc:  # binary missing, timeout, ...
        return False, str(exc)
    if r.returncode != 0:
        return False, f'image {image!r} not present in local Podman storage'
    return True, 'ok'


def _container_states() -> dict:
    """Map container name -> state for evonic-managed Podman containers."""
    try:
        r = _runtime('ps', '-a',
                     '--filter', 'label=evonic.managed=1',
                     '--format', '{{.Names}} {{.State}}')
    except Exception:
        logger.debug('podman ps failed', exc_info=True)
        return {}
    if r.returncode != 0:
        return {}
    states = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            states[parts[0]] = parts[1]
    return states


def _is_already_exists_error(stderr: str) -> bool:
    lowered = (stderr or '').lower()
    return any(marker in lowered for marker in _ALREADY_EXISTS_MARKERS)


def is_container_gone(result: dict) -> bool:
    """True when an exec/file-op result says the container no longer exists.

    Podman phrases differ from Docker's, so this hook is rebound into the parent
    module (see :func:`bind`) and drives the inherited recreate-on-retry path.
    """
    if 'error' not in result and result.get('exit_code', 0) == 0:
        return False
    combined = (result.get('stderr', '') + result.get('error', '')).lower()
    return any(p in combined for p in _CONTAINER_GONE_PHRASES)


def get_pool_status() -> dict:
    """Pool snapshot for this backend's own pool (overrides the parent's)."""
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
            'backend': _RUNTIME_NAME,
            'pool_size': len(_containers),
            'max_containers': SANDBOX_MAX_CONTAINERS,
            'idle_timeout': docker_backend.SANDBOX_IDLE_TIMEOUT,
            'persistent_count': persistent_count,
            'persistent_enabled': docker_backend.SANDBOX_PERSISTENT_CONTAINER_ENABLED,
            'containers': containers,
        }


# ---------------------------------------------------------------------------
# Parent-module hooks (image, restart policy, sibling runtime visibility)
# ---------------------------------------------------------------------------

def _effective_image() -> str:
    """Image used for new sandbox containers (Podman naming scheme)."""
    return SANDBOX_PODMAN_IMAGE


def _restart_policy(persistent: bool) -> str:
    """Rootless Podman refuses ``unless-stopped``; the startup sweep restarts."""
    return _PODMAN_RESTART_POLICY


def _container_create_args() -> list:
    """Extra ``podman run`` arguments for new sandbox containers.

    The inherited ``DockerBackend`` creation path builds its own argv (it never
    calls :func:`build_container_command`), and that argv is the one every
    ``bash``/``runpy`` call actually goes through. Rootless Podman needs two
    flags the Docker argv does not carry; without them the container starts
    fine but the bind-mounted workspace is owned by ``root`` and is therefore
    read-only for the sandbox user.

    * ``--userns`` maps the image user onto the host uid (see the module
      docstring, *Rootless notes*).
    * ``--security-opt`` drops the SELinux label so a ``:rw`` bind mount is not
      blocked by a label mismatch on enforcing hosts.
    """
    args = []
    userns = _userns_arg()
    if userns:
        args.append(f'--userns={userns}')
    if _PODMAN_SECURITY_OPT:
        args.append(f'--security-opt={_PODMAN_SECURITY_OPT}')
    return args


def _get_or_create_container_hook(session_id: str, agent_id: str = '',
                                  workspace: str = None, persistent: bool = False,
                                  artifacts_root: str = None) -> tuple:
    """Entry point the *parent* module uses while this runtime is bound.

    ``DockerBackend`` builds its argv inline, so its creation path only knows
    about Docker's flags. Binding this module into the parent (``bind(True)``)
    points the inherited ``_get_or_create_container`` global at this function,
    which routes every creation - including the ones triggered by
    ``run_bash``/``run_python`` - through the Podman builder.
    """
    docker_backend._claim_runtime(_RUNTIME_NAME)
    ensure_hooks_bound()
    return _get_or_create_container(
        session_id, agent_id=agent_id, workspace=workspace,
        persistent=persistent, artifacts_root=artifacts_root,
    )


def _sibling_runtime_names() -> set:
    """Names of containers owned by a *loaded* sibling container runtime.

    The parent keeps one process-global pool per runtime and its sweep/reconcile
    loops destroy evonic-labelled containers that are missing from that pool.
    When both backends are imported in one process (an in-process simulation, or
    a Docker deployment experimenting with Podman), each sweep must leave the
    other runtime's containers alone.

    Returns an empty set while this module is the one bound into the parent
    (normal Podman-only process, there is no sibling), and the Podman pool's
    names while the parent is bound to a different runtime (Docker) so that its
    sweep/reconcile loops leave Podman-owned containers alone.
    """
    if docker_backend.__dict__.get('_sibling_container_names') is globals()[
            '_sibling_runtime_names']:
        return set()
    with _pool_lock:
        names = {info.get('container_name', '') for info in _containers.values()}
    return {n for n in names if n}


# ---------------------------------------------------------------------------
# Container creation
# ---------------------------------------------------------------------------

def build_container_command(
    name: str,
    workspace: str,
    scratch: str,
    artifacts_mounts=None,
    persistent: bool = False,
    created_at: float = 0.0,
    image: str = None,
) -> list:
    """Return the ``podman run -d`` argv used to create a sandbox container.

    Kept as a pure function so it can be unit-tested on hosts without Podman.
    The argv mirrors ``docker_backend._get_or_create_container`` one-for-one,
    except for the image/restart policy and the extra ``evonic.runtime`` label.
    """
    artifacts_mounts = artifacts_mounts or []
    image = image or SANDBOX_PODMAN_IMAGE
    return [
        'run', '-d',
        *((('--rm',)) if not persistent else ()),
        f'--restart={_PODMAN_RESTART_POLICY}',
        '--name', name,
        f'--memory={SANDBOX_MEMORY_LIMIT}',
        f'--cpus={SANDBOX_CPU_LIMIT}',
        f'--network={SANDBOX_NETWORK}',
        '--pids-limit=256',
        *_container_create_args(),
        '--tmpfs', '/tmp:rw,exec,size=3000m',
        '--tmpfs', '/root:rw,size=16m',
        '--label', 'evonic.managed=1',
        '--label', f'evonic.runtime={_RUNTIME_NAME}',
        '--label', f'evonic.pid={os.getpid()}',
        '--label', f'evonic.created_at={created_at:.0f}',
        '--label', f'evonic.persistent={1 if persistent else 0}',
        '-v', f'{workspace}:{_WORKSPACE_MOUNT}:rw',
        *artifacts_mounts,
        '-v', f'{docker_backend._HELPERS_DIR}:{docker_backend._HELPERS_MOUNT}:ro',
        '-w', _WORKSPACE_MOUNT,
        '-e', f'SCRATCH={scratch}',
        # The image reference must be the first non-flag token; a missing image
        # makes Podman read `-v <workspace>:/workspace:rw` as image/volume names.
        image,
        'sh', '-c', f'mkdir -p {scratch} && exec sleep infinity',
    ]


def _evict_lru() -> None:
    """Evict the least recently used container from this backend's pool."""
    with _pool_lock:
        if not _containers:
            return
        non_persistent = [s for s, i in _containers.items() if not i.get('persistent')]
        if non_persistent:
            lru_sid = min(non_persistent, key=lambda s: _containers[s]['last_used'])
            is_persistent = False
        else:
            lru_sid = min(_containers, key=lambda s: _containers[s]['last_used'])
            is_persistent = True
        logger.warning('Max containers reached - evicting LRU session %s (%s)',
                       lru_sid[:12], 'persistent' if is_persistent else 'non-persistent')
    _destroy_container(lru_sid)


def _destroy_container(session_id: str) -> dict:
    """Remove the container registered under ``session_id`` in this pool.

    Mirrors the parent's implementation but always talks to Podman and only to
    this module's pool.
    """
    with _pool_lock:
        info = _containers.pop(session_id, None)

    if info is None:
        return {'result': 'no_container', 'detail': 'No active container for this session.'}

    container_id = info['container_id']
    result = _runtime('rm', '-f', container_id)
    if result.returncode == 0:
        return {'result': 'container_destroyed', 'container_id': container_id[:12]}
    logger.warning('podman rm failed for %s: %s - re-adding to pool',
                   container_id[:12], (result.stderr or '').strip())
    with _pool_lock:
        _containers[session_id] = info
    return {'error': f'podman rm failed: {(result.stderr or "").strip()}'}


def _artifacts_mounts(agent_id: str, workspace: str, artifacts_root: str) -> list:
    """Bind-mount the agent's host artifact registry, mirroring the parent.

    See ``docker_backend._get_or_create_container`` for why this mount exists:
    it keeps ``/workspace/shared/agents/<id>/artifacts`` pointing at the same
    directory the web UI and artifact tools read.
    """
    if not (agent_id and artifacts_root):
        return []
    registry_dir = os.path.join(artifacts_root, agent_id, 'artifacts')
    ws_relative = os.path.join(workspace, 'shared', 'agents', agent_id, 'artifacts')
    if os.path.realpath(ws_relative) == os.path.realpath(registry_dir):
        return []
    os.makedirs(registry_dir, exist_ok=True)
    return ['-v', f'{registry_dir}:/workspace/shared/agents/{agent_id}/artifacts:rw']


def _get_or_create_container(
    session_id: str,
    agent_id: str = '',
    workspace: str = None,
    persistent: bool = False,
    artifacts_root: str = None,
) -> tuple:
    """Return ``(container_id, None)`` or ``(None, error_string)``.

    Same contract and pool keys as ``docker_backend._get_or_create_container``,
    so cross-session reuse, LRU eviction and idle reaping behave identically.
    The pool dict, lock, reaper and startup sweep are the parent's module-level
    objects; only the CLI that creates/removes containers changes.
    """
    effective_workspace = os.path.abspath(workspace if workspace else SANDBOX_WORKSPACE)
    effective_artifacts_root = artifacts_root or _ARTIFACTS_ROOT
    pool_key = (agent_id or session_id) if persistent else session_id

    needs_destroy = False
    with _pool_lock:
        info = _containers.get(pool_key)
        if info is not None:
            if (info.get('workspace') != effective_workspace
                    or info.get('mount_version') != _MOUNT_LAYOUT_VERSION
                    or info.get('artifacts_root', _ARTIFACTS_ROOT) != effective_artifacts_root):
                logger.info('Workspace/mount changed for %s%s — recreating container',
                            'persistent ' if persistent else '', pool_key[:12])
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

    name = docker_backend._container_name(pool_key, agent_id if persistent else '')
    scratch = scratch_dir(agent_id)
    created_at = time.time()
    mounts = _artifacts_mounts(agent_id, effective_workspace, effective_artifacts_root)
    cmd = build_container_command(
        name=name, workspace=effective_workspace, scratch=scratch,
        artifacts_mounts=mounts, persistent=persistent, created_at=created_at,
    )

    try:
        result = _runtime(*cmd)
    except FileNotFoundError:
        return None, (f'Podman CLI not found: {_RUNTIME_CLI!r} is not installed on '
                      f'this host. Install podman or set SANDBOX_BACKEND=docker.')
    except subprocess.TimeoutExpired:
        return None, f'podman run timed out after 30s (image {SANDBOX_PODMAN_IMAGE})'

    if result.returncode != 0 and _is_already_exists_error(result.stderr):
        logger.info('Stale container found for %s — removing and retrying', name)
        _destroy_named(name)
        try:
            result = _runtime(*cmd)
        except subprocess.TimeoutExpired:
            return None, f'podman run timed out after 30s (image {SANDBOX_PODMAN_IMAGE})'

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        if 'image' in detail.lower():
            detail = (f'{detail} (build it with: podman build -t evonic-sandbox:latest '
                      f'docker/tools/)')
        return None, f'Failed to start container: {detail}'

    container_id = result.stdout.strip()
    if not container_id:
        return None, 'Failed to start container: podman returned an empty container id'

    # A rootless user-namespace mismatch leaves the workspace bind mount owned by
    # a foreign uid: the container starts fine and every write fails. Verify once
    # here so the agent never receives a read-only sandbox.
    writable, why = workspace_writable(container_id, _WORKSPACE_MOUNT)
    if not writable:
        _destroy_named(container_id)
        logger.error('Sandbox workspace is not writable (%s): %s', container_id[:12], why)
        return None, (
            'Sandbox workspace is not writable inside the container: ' + str(why) +
            '. Check SANDBOX_PODMAN_USERNS (current: ' + (_userns_arg() or '<none>') +
            ') and SANDBOX_PODMAN_USER (current: ' + _PODMAN_USER + '); the mapping ' +
            'must make the host uid the owner of the mounted workspace.'
        )

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
            'artifacts_root': effective_artifacts_root,
            'persistent': persistent,
            'runtime': _RUNTIME_NAME,
            'pool_key': pool_key,
        }
    ensure_reaper_running()
    ensure_monitor_running()
    return container_id, None


def _destroy_named(name: str) -> None:
    """Remove a container by name, tolerating an already-gone container."""
    result = _runtime('rm', '-f', name)
    if result.returncode == 0:
        return
    if 'no such container' in ((result.stderr or '') + (result.stdout or '')).lower():
        return
    logger.info('podman rm -f failed for %s, retrying stop+rm: %s', name,
                ((result.stderr or '') + (result.stdout or '')).strip())
    _runtime('stop', '-t', '2', name)
    _runtime('rm', '-f', name)


# ---------------------------------------------------------------------------
# Runtime binding into the parent module
# ---------------------------------------------------------------------------

_MISSING = object()

# Hooks installed into the parent module so its inherited code loads, saves and
# runs against *this* module's pool and CLI:
#   pool objects   -> _containers / _pool_lock / _RUNTIME_NAME
#   CLI            -> _docker / _docker_popen
#   argv hooks     -> _effective_image / _restart_policy
#   recovery hooks -> _is_container_gone / get_pool_status / _sibling_runtime_names
#
# Each entry is (parent attribute, provider in this module). The provider names
# differ from the parent names on purpose: importing the parent's names here
# would shadow the Podman implementations above.
_BOUND_ATTRS = (
    ('_containers', '_containers'),
    ('_pool_lock', '_pool_lock'),
    ('_RUNTIME_NAME', '_RUNTIME_NAME'),
    ('_docker', '_runtime'),
    ('_docker_popen', '_runtime_popen'),
    ('_effective_image', '_effective_image'),
    ('_restart_policy', '_restart_policy'),
    ('_get_or_create_container', '_get_or_create_container_hook'),
    ('_is_container_gone', 'is_container_gone'),
    ('get_pool_status', 'get_pool_status'),
    ('_sibling_container_names', '_sibling_runtime_names'),
)

# Runtime identity recorded at import time. ``bind(False)`` only restores the
# Podman hooks while the process is still claimed by Podman (see :func:`bind`),
# which keeps a Docker process that merely imported this module from having its
# pool and CLI reverted behind its back.
_IMPORT_CLAIM = docker_backend.get_active_runtime()

# The parent's own pool object, captured before anything is bound. Callers that
# resolved the pool at import time (other backends, long-lived backend instances)
# keep working because ``bind(False)`` hands this exact object back.
_PARENT_POOL = docker_backend._containers

# Snapshot of the parent's own attributes, recorded before anything is bound.
# It is the restore target for ``bind(False)``: a hook that was absent on the
# parent is simply removed again.
_PARENT_ORIGINALS = {
    attr: docker_backend.__dict__.get(attr, _MISSING) for attr, _ in _BOUND_ATTRS
}

# The pool the parent currently sees while this module is bound. It is
# ``_containers`` itself, never a copy: the parent's cleanup must see the
# containers Podman created.
_bound_pool = _containers


def _is_our_claim() -> bool:
    """True while Podman owns (or may still take) the process-global pool."""
    active = docker_backend.get_active_runtime()
    if active == _RUNTIME_NAME:
        return True
    # An unclaimed, empty pool is the pristine state of a process whose first act
    # was importing this module; nothing has been created yet, so nothing can be
    # taken from another runtime either.
    return not active and not _containers and not _PARENT_POOL


def bind(bind: bool = True) -> None:
    """Bind (or release) the parent module's runtime, pool and hooks.

    The parent resolves its pool and container CLI through module globals, which
    is what lets a subclass swap the runtime without forking the pool
    implementation.

    Binding is only taken when no other runtime owns the pool: a Docker process,
    a shared deployment or a test module that merely imports this module must
    keep its own pool and CLI. Releasing is only taken while Podman still owns
    the pool, and each hook is restored only if it still holds this module's
    implementation, so a later ``monkeypatch`` is never reverted behind its back.

    Releasing hands the parent its original pool object back (filled with the
    containers Podman created so its cleanup can do its job), which leaves both
    backends usable side by side in one process.
    """
    active = docker_backend.get_active_runtime()
    if bind:
        if active and active != _RUNTIME_NAME:
            return
        for attr, provider in _BOUND_ATTRS:
            value = globals().get(provider, _MISSING)
            if value is _MISSING:
                continue
            setattr(docker_backend, attr, value)
        return

    if not _is_our_claim():
        return
    for attr, provider in _BOUND_ATTRS:
        value = globals().get(provider, _MISSING)
        if value is _MISSING:
            continue
        if docker_backend.__dict__.get(attr) is not value:
            continue
        original = _PARENT_ORIGINALS.get(attr, _MISSING)
        if original is _MISSING:
            docker_backend.__dict__.pop(attr, None)
        else:
            setattr(docker_backend, attr, original)
    # The containers Podman created belong to this module; they are not handed to the
    # parent. Only the parent's own pool object is restored, with its own
    # contents, so a released module never leaves containers behind that another
    # runtime would sweep or remove.
    docker_backend.__dict__['_containers'] = _PARENT_POOL


def is_bound() -> bool:
    """True when this module is the one currently driving the parent module."""
    return docker_backend.__dict__.get('_containers') is _containers


def ensure_hooks_bound() -> None:
    """Bind the Podman pool and hooks if this backend is not the active one."""
    if not is_bound():
        bind(True)


def ensure_reaper_running() -> None:
    """Start the shared idle reaper (it reads whichever pool is bound)."""
    docker_backend._ensure_reaper_running()


def ensure_monitor_running() -> None:
    """Start the shared pool monitor (it reads whichever pool is bound)."""
    docker_backend._ensure_monitor_running()


def _atexit_release() -> None:
    """Release the parent's hooks before the interpreter runs its own cleanup.

    Without this the parent's atexit handler would keep the rebound Podman CLI
    and run ``podman rm`` against containers it never created. Restoring first
    turns it back into a no-op for a Docker process (emptied pool, Docker CLI).
    """
    try:
        bind(False)
    except Exception:  # interpreter shutdown: never raise
        pass


atexit.register(_atexit_release)


# ---------------------------------------------------------------------------
# PodmanBackend
# ---------------------------------------------------------------------------

class PodmanBackend(docker_backend.DockerBackend):
    """Executes bash/python inside a Podman container.

    Behaviourally identical to :class:`DockerBackend` — the parent's bash,
    python and file-I/O paths call the rebound ``_docker``/``_docker_popen``
    helpers, so they run against the Podman CLI. Only the runtime identity,
    creation argv and availability checks are Podman-specific.
    """

    runtime_name = 'podman'

    def __init__(self, session_id: str, agent_id: str = '', workspace: str = None,
                 is_subagent: bool = False, is_explorer: bool = False,
                 container_session_id: str = None, container_workspace: str = None,
                 persistent: bool = False, artifacts_root: str = None):
        ensure_hooks_bound()
        # Raises a descriptive error when a *different* container runtime
        # already owns the process-global pool.
        docker_backend._claim_runtime(self.runtime_name)
        # The claim is what authorises the hooks; re-check after winning it in
        # case the pool was claimed elsewhere between bind and claim.
        if not is_bound():
            ensure_hooks_bound()
        super().__init__(
            session_id, agent_id=agent_id, workspace=workspace,
            is_subagent=is_subagent, is_explorer=is_explorer,
            container_session_id=container_session_id,
            container_workspace=container_workspace,
            persistent=persistent,
            artifacts_root=artifacts_root,
        )

    @property
    def image(self) -> str:
        """Image this backend launches (override of the Docker default)."""
        return SANDBOX_PODMAN_IMAGE

    def status(self) -> dict:
        ok, detail = runtime_reachable()
        with _pool_lock:
            info = _containers.get(self._container_session_id)
        base = {'backend': 'podman', 'runtime': _RUNTIME_CLI,
                'image': SANDBOX_PODMAN_IMAGE}
        if not ok:
            base.update({
                'runtime_available': False,
                'detail': detail,
                'container_id': (info or {}).get('container_id', None),
            })
            return base
        base['runtime_available'] = True
        if info:
            base.update({
                'container_id': info['container_id'][:12],
                'workspace': info.get('workspace'),
                'created_at': info.get('created_at'),
                'last_used': info.get('last_used'),
                'persistent': bool(info.get('persistent')),
            })
            return base
        base.update({'container_id': None,
                     'detail': 'No container yet (will be created on first use).'})
        return base

    def health_check(self) -> dict:
        """Deeper diagnostics: CLI, engine, image and pool state."""
        ok, detail = runtime_reachable()
        image_ok, image_detail = _image_available() if ok else (False, 'runtime unavailable')
        return {
            'cli': _RUNTIME_CLI,
            'cli_found': cli_available(),
            'runtime_reachable': ok,
            'runtime_detail': detail,
            'image': SANDBOX_PODMAN_IMAGE,
            'image_present': image_ok,
            'image_detail': image_detail,
            'containers': _container_states(),
            'pool': get_pool_status(),
        }


# Names re-exported for tests and for callers that treat container backends
# generically.
__all__ = [
    'PodmanBackend',
    'PODMAN_CLI',
    'bind',
    'build_container_command',
    'cli_available',
    'ensure_hooks_bound',
    'runtime_reachable',
    '_get_or_create_container',
    '_destroy_container',
    '_container_states',
]