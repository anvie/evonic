"""
exec_backend — execution backend abstraction for bash and runpy tools.

New execution environments (Docker, local, SSH, E2B, …) implement
ExecutionBackend and register themselves via the module-level `registry`.

Usage from bash.py / runpy.py:
    from backend.tools.lib.exec_backend import registry
    backend = registry.get_backend(session_id, agent_context)
    return backend.run_bash(script, timeout, env)
"""

import re
import threading
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# Shared utilities (used by all backends)
# ---------------------------------------------------------------------------

_ENV_KEY_PATTERN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def truncate(text: str, max_bytes: int) -> str:
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', errors='replace') + '\n[truncated]'


def validate_env_keys(env: dict) -> tuple:
    """Return (clean_env, error) where error is None if all keys are valid."""
    for key in env:
        if not _ENV_KEY_PATTERN_RE.match(key):
            return {}, f'Invalid environment variable key: {key!r}. Only [A-Za-z_][A-Za-z0-9_]* is allowed.'
    return env, None


def file_stat_code(path: str) -> str:
    """Return a Python code snippet that prints exists/size/is_binary for *path*.

    Used by SSH-based backends (SSHBackend, RemoteWorkplaceBackend,
    TunnelWorkplaceBackend) to query file metadata on a remote host.
    The snippet prints ``exists=0|1 size=<int> is_binary=0|1`` on a
    single line, parseable by :func:`parse_file_stat_output`.
    """
    return f"""
import os
p = {repr(path)}
if not os.path.exists(p):
    print('exists=0 size=0 is_binary=0')
else:
    size = os.path.getsize(p)
    try:
        chunk = open(p, 'rb').read(8192)
        is_binary = b'\\x00' in chunk
    except Exception:
        is_binary = True
    print(f'exists=1 size={{size}} is_binary={{1 if is_binary else 0}}')
"""


def parse_file_stat_output(output: str) -> dict:
    """Parse the ``exists=… size=… is_binary=…`` output produced by :func:`file_stat_code`.

    Returns ``{'exists': bool, 'size': int, 'is_binary': bool}``.
    On parse failure, returns ``{'exists': False, 'size': 0, 'is_binary': False}``.
    """
    try:
        parts = dict(kv.split('=') for kv in output.strip().split())
        return {
            'exists': parts.get('exists') == '1',
            'size': int(parts.get('size', 0)),
            'is_binary': parts.get('is_binary') == '1',
        }
    except Exception:
        return {'exists': False, 'size': 0, 'is_binary': False}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ExecutionBackend(ABC):
    """Base class for all execution backends."""

    @abstractmethod
    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        """Execute a bash script. Returns {stdout, stderr, exit_code, execution_time}.

        on_output: optional Callable[[str], None] invoked with incremental
        output chunks as they arrive (live streaming). The return value is
        unchanged — the full buffered output is still returned for logging and
        backward compatibility. Backends that cannot stream ignore it and
        return batched output as before.
        """

    @abstractmethod
    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        """Execute Python code. Returns {stdout, stderr, exit_code, execution_time}."""

    @abstractmethod
    def destroy(self) -> dict:
        """Tear down the backend (stop container, close connection, etc.)."""

    @abstractmethod
    def status(self) -> dict:
        """Return backend type and status info."""

    # ------------------------------------------------------------------
    # File I/O — used by write_file, read_file, str_replace, patch tools
    # to ensure file operations target the execution environment (Docker
    # container, SSH remote, etc.) instead of the host filesystem.
    # ------------------------------------------------------------------

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """Check whether a path exists in the execution environment."""

    @abstractmethod
    def file_stat(self, path: str) -> dict:
        """Return {'exists': bool, 'size': int, 'is_binary': bool} for a path."""

    @abstractmethod
    def read_file(self, path: str) -> dict:
        """Read a text file. Returns {'content': str} or {'error': str}."""

    @abstractmethod
    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        """Write string content to a file. Returns {'ok': True} or {'error': str}."""

    @abstractmethod
    def make_dirs(self, path: str) -> dict:
        """Create directories recursively. Returns {'ok': True} or {'error': str}."""

    @abstractmethod
    def cat_file_bytes(self, path: str) -> dict:
        """Read a file as raw bytes from the execution environment.

        Used by save_artifact to stream file contents from sandbox (tmpfs),
        SSH remote, or tunnel — places where shutil.copy2 / docker cp cannot reach.

        Returns {'bytes': b'...'} or {'error': str}.
        """

    @abstractmethod
    def delete_file(self, path: str) -> dict:
        """Delete a file from the execution environment.

        Used by save_artifact to remove the source file after a verified
        move into the artifacts directory.

        Returns {'ok': True} on success or {'error': str} on failure.
        """

    @abstractmethod
    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        """Write raw bytes to a file in the execution environment.

        Used by fetch_artifact to stream artifact file bytes from the host
        into the sandbox/SSH/tunnel execution environment.

        Returns {'ok': True} on success or {'error': str} on failure.
        """

    # ------------------------------------------------------------------
    # Path resolution — converts a host filesystem path into the
    # path the backend's execution environment can access.
    # Default: identity (the path is used as-is).
    # ------------------------------------------------------------------

    def resolve_path(self, path: str) -> str:
        """Convert a host filesystem path to the backend's view of that path.

        The default is identity.  DockerBackend overrides this to translate
        host-mount paths to their /workspace container counterpart.
        """
        return path


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

class BackendRegistry:
    """
    Per-session backend registry.

    Each session starts with no explicit backend; get_backend() auto-creates
    a DockerBackend or LocalBackend on first call based on agent_context.
    Tools like sshc call set_backend() to override for a session.
    """

    def __init__(self):
        self._backends: dict[str, ExecutionBackend] = {}
        self._lock = threading.Lock()

    def get_backend(self, session_id: str, agent_context: dict) -> 'ExecutionBackend':
        """Return the active backend for a session, creating a default if needed."""
        with self._lock:
            if session_id in self._backends:
                return self._backends[session_id]

        # Extract sandbox setting from agent_context (used by both workplace and default paths)
        sandbox_enabled = bool((agent_context or {}).get('sandbox_enabled', 1))

        # If agent has a Workplace assigned, delegate to WorkplaceManager
        workplace_id = (agent_context or {}).get('workplace_id')
        if workplace_id:
            from backend.workplaces.manager import workplace_manager
            return workplace_manager.get_backend(workplace_id, sandbox_enabled=sandbox_enabled)

        # Create default backend based on agent_context
        workspace = (agent_context or {}).get('workspace') or None

        if sandbox_enabled:
            try:
                from config import SANDBOX_BACKEND
            except ImportError:
                SANDBOX_BACKEND = 'docker'
            agent_id = (agent_context or {}).get('agent_id', (agent_context or {}).get('id', ''))
            is_subagent = bool((agent_context or {}).get('is_subagent'))
            is_explorer = bool((agent_context or {}).get('is_explorer'))
            if SANDBOX_BACKEND == 'bwrap':
                from backend.tools.lib.backends.bwrap_backend import BwrapBackend
                agent_name = (agent_context or {}).get('agent_name') or (agent_context or {}).get('name') or ''
                backend = BwrapBackend(session_id, workspace=workspace, agent_id=agent_id,
                                       agent_name=agent_name, is_subagent=is_subagent,
                                       is_explorer=is_explorer)
            else:
                from backend.tools.lib.backends.docker_backend import DockerBackend
                from config import SANDBOX_PERSISTENT_CONTAINER_ENABLED
                parent_session_id = ((agent_context or {}).get('_sandbox_parent_session_id')
                                     if is_explorer else None)
                parent_workspace = ((agent_context or {}).get('_sandbox_parent_workspace')
                                    if parent_session_id else None)
                # Main agent (not a sub-agent, not an explorer) gets a persistent
                # container keyed by agent_id, so installed packages survive
                # across sessions and across `evonic` restarts. Sub-agents and
                # explorers share their parent agent's container.
                persistent = (
                    SANDBOX_PERSISTENT_CONTAINER_ENABLED
                    and not is_subagent
                    and not is_explorer
                )
                backend = DockerBackend(
                    session_id, agent_id=agent_id, workspace=workspace,
                    is_subagent=is_subagent, is_explorer=is_explorer,
                    container_session_id=parent_session_id,
                    container_workspace=parent_workspace,
                    persistent=persistent,
                )
        else:
            from backend.tools.lib.backends.local_backend import LocalBackend
            run_as_user = (agent_context or {}).get('run_as_user') or None
            backend = LocalBackend(session_id=session_id, workspace=workspace,
                                   run_as_user=run_as_user)

        # Don't store default backends — they're ephemeral and session-keyed
        # internally by DockerBackend itself. Only explicit overrides are stored.
        return backend

    def set_backend(self, session_id: str, backend: 'ExecutionBackend') -> None:
        """Override the backend for a session (called by sshc, e2b, etc.)."""
        with self._lock:
            old = self._backends.get(session_id)
            if old is not None:
                try:
                    old.destroy()
                except Exception:
                    pass
            self._backends[session_id] = backend

    def clear_backend(self, session_id: str) -> dict:
        """Remove backend override, reverting to default (Docker/local) on next call."""
        with self._lock:
            backend = self._backends.pop(session_id, None)
        if backend is None:
            return {'result': 'no_override', 'detail': 'No explicit backend was set for this session.'}
        try:
            result = backend.destroy()
        except Exception as e:
            result = {'error': str(e)}
        return result

    def get_status(self, session_id: str) -> dict:
        """Return status of the active backend override, or 'default' if none."""
        with self._lock:
            backend = self._backends.get(session_id)
        if backend is None:
            return {'backend': 'default', 'detail': 'Using default Docker/local backend.'}
        return backend.status()


# Module-level singleton used by bash.py, runpy.py, and sshc.py
registry = BackendRegistry()
