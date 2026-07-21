"""
Ensure a host-local file path is accessible in the agent's workspace environment.

If the agent has a workplace_id (SSH/tunnel remote), this helper copies the file
from the host filesystem to the execution backend so the file can be accessed
by other tools (read_file, bash, runpy, etc.) that run inside the remote.

If there is no workplace, the path is returned as-is (it's already accessible).
"""

import os
import re
import uuid

from backend.tools._workspace import scratch_dir


def _safe_component(value: str, fallback: str) -> str:
    """Return a filesystem-safe path component without changing useful IDs."""
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '')).strip('._')
    return value or fallback


def _staging_path(local_path: str, agent: dict) -> str:
    """Return an agent- and session-scoped backend staging path."""
    agent_id = _safe_component(agent.get('id'), 'default')
    session_id = _safe_component(agent.get('session_id'), 'default')
    filename = _safe_component(os.path.basename(local_path), 'attachment')
    return os.path.join(
        scratch_dir(agent_id), 'attachments', session_id,
        f"{uuid.uuid4().hex}_{filename}",
    )


def ensure_workplace_file(local_path: str, agent: dict) -> str:
    """Return a path that is accessible from the agent's execution environment.

    For local agents (no workplace, no sandbox), the host path is returned directly.
    For agents with a workplace_id or sandbox, the file is copied to the execution
    backend and the remote-accessible path is returned.

    Args:
        local_path: Absolute or resolved path on the host filesystem.
        agent: Agent context dict (must contain at least 'id').

    Returns:
        The accessible path string (may differ from local_path for remote agents).

    Raises:
        RuntimeError: If the file cannot be transferred to the remote backend.
    """
    agent = agent or {}
    workplace_id = agent.get('workplace_id')
    sandbox_enabled = agent.get('sandbox_enabled', False)

    # No workplace and no sandbox ─ path is already accessible
    if not workplace_id and not sandbox_enabled:
        return local_path

    # Path must exist locally before we can transfer it
    if not os.path.isfile(local_path):
        raise RuntimeError(f"File not found on host: {local_path}")

    # Read file bytes from the host filesystem
    with open(local_path, 'rb') as f:
        data = f.read()

    # Resolve the execution backend
    if workplace_id:
        from backend.workplaces.manager import workplace_manager
        try:
            backend = workplace_manager.get_backend(
                workplace_id, sandbox_enabled=sandbox_enabled
            )
        except RuntimeError as e:
            raise RuntimeError(f"Workplace error: {e}")
    else:
        # Sandbox with no workplace: resolve through execution registry
        from backend.tools.lib.exec_backend import registry as exec_registry
        session_id = agent.get('session_id', 'default')
        backend = exec_registry.get_backend(session_id, agent)

    # Stage the copy in the execution environment's per-agent scratchpad.
    # A unique name prevents collisions when different attachments share a basename.
    dest_path = _staging_path(local_path, agent)

    # Resolve path if the backend supports it
    resolved_dest = (
        backend.resolve_path(dest_path)
        if hasattr(backend, 'resolve_path')
        else dest_path
    )

    # Write file to the execution backend
    write_result = backend.write_file_bytes(resolved_dest, data, create_dirs=True)
    if 'error' in write_result:
        raise RuntimeError(
            f"Failed to write file to execution backend: {write_result['error']}"
        )

    # Verify file was written correctly
    file_stat = backend.file_stat(resolved_dest)
    if file_stat.get('size', -1) != len(data):
        try:
            backend.delete_file(resolved_dest)
        except Exception:
            pass
        raise RuntimeError(
            f"Size mismatch after transferring {os.path.basename(local_path)}: "
            f"expected {len(data)} bytes, got {file_stat.get('size', -1)} bytes"
        )

    return dest_path
