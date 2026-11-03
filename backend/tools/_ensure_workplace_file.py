"""
Ensure a host-local file path is accessible in the agent's workspace environment.

If the agent has a workplace_id (SSH/tunnel remote), this helper copies the file
from the host filesystem to the execution backend so the file can be accessed
by other tools (read_file, bash, runpy, etc.) that run inside the remote.

If there is no workplace, the path is returned as-is (it's already accessible).
"""

import hashlib
import os
import re

from backend.tools._workspace import scratch_dir


def _safe_component(value: str, fallback: str) -> str:
    """Return a filesystem-safe path component without changing useful IDs."""
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '')).strip('._')
    return value or fallback


def _staging_path(local_path: str, agent: dict) -> str:
    """Return a deterministic agent- and session-scoped backend staging path.

    The name is derived from the source path plus its size and mtime, so the same
    attachment always resolves to the same backend path.  A fresh random name per
    call would duplicate multi-MB payloads on every turn and make the path shown
    to the model differ from the one later reported by other tools.
    """
    agent_id = _safe_component(agent.get('id'), 'default')
    session_id = _safe_component(agent.get('session_id'), 'default')
    filename = _safe_component(os.path.basename(local_path), 'attachment')
    try:
        info = os.stat(local_path)
        fingerprint = f"{os.path.abspath(local_path)}|{info.st_size}|{info.st_mtime_ns}"
    except OSError:
        fingerprint = str(local_path)
    digest = hashlib.sha1(fingerprint.encode('utf-8', 'replace')).hexdigest()[:16]
    return os.path.join(
        scratch_dir(agent_id), 'attachments', session_id,
        f"{digest}_{filename}",
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
        The path as seen by the execution environment (a container path such as
        /workspace/... for Docker).  This is the value that must be handed to the
        model and to other tools — never the host path, which does not exist
        inside the sandbox.

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

    expected_size = os.path.getsize(local_path)

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
    # The name is content-derived, so repeated calls for the same attachment
    # reuse one path instead of stacking up duplicate copies.
    dest_path = _staging_path(local_path, agent)

    # Resolve path if the backend supports it
    resolved_dest = (
        backend.resolve_path(dest_path)
        if hasattr(backend, 'resolve_path')
        else dest_path
    )

    # Idempotent: skip the transfer when the backend already holds this exact file.
    # Avoids re-streaming multi-MB payloads through `docker exec` on every turn.
    try:
        existing = backend.file_stat(resolved_dest)
    except Exception:
        existing = None
    if isinstance(existing, dict) and existing.get('size', -1) == expected_size:
        return resolved_dest

    # Read file bytes from the host filesystem only when a transfer is needed
    with open(local_path, 'rb') as f:
        data = f.read()

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

    # Return the backend-visible path: the host path is meaningless inside the sandbox
    return resolved_dest
