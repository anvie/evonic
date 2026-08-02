"""
Shared utilities for FastContext tools — Grep, Glob, Read.

Provides _auto_correct_path for hallucinated-path fallback resolution.
"""
import json
import os
import posixpath


def _backend(agent: dict):
    from backend.tools.lib.exec_backend import registry
    return registry.get_backend((agent or {}).get('session_id') or 'default', agent or {})


def _run_python_json(backend, code: str, timeout: int = 30):
    result = backend.run_python(code, timeout, {})
    if result.get('error'):
        raise RuntimeError(result['error'])
    if result.get('exit_code', 0) != 0:
        raise RuntimeError(result.get('stderr') or 'backend path operation failed')
    stdout = result.get('stdout') or ''
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    # Backends append '\n[truncated]' when stdout exceeds the 64KB cap.
    # JSON cut mid-stream can never be parsed; surface the real cause.
    if stdout.rstrip().endswith('[truncated]'):
        raise RuntimeError(
            'backend output exceeded the 64KB stdout limit and was truncated; '
            'narrow the pattern or reduce the result set and retry'
        )
    try:
        return json.loads(stdout.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f'backend returned unparseable output ({len(stdout)} bytes): {exc}'
        ) from exc


def _path_info(backend, path: str) -> dict:
    code = f'''import json, os
p = {path!r}
print(json.dumps({{"path": os.path.realpath(p), "exists": os.path.exists(p), "is_dir": os.path.isdir(p), "is_file": os.path.isfile(p), "size": os.path.getsize(p) if os.path.isfile(p) else 0}}))'''
    return _run_python_json(backend, code)


def _auto_correct_path(backend, requested_path: str, workspace: str, path_is_dir: bool = False) -> str:
    """Resolve a hallucinated path suffix within the execution backend."""
    code = f'''import json, os
requested, workspace, want_dir = {requested_path!r}, {workspace!r}, {path_is_dir!r}
result = requested
if not os.path.exists(requested) and os.path.isdir(workspace):
    name = os.path.basename(requested.rstrip(os.sep))
    matches = []
    for root, dirs, files in os.walk(workspace):
        candidates = dirs if want_dir else files
        if name in candidates:
            matches.append(os.path.join(root, name))
    if matches:
        result = sorted(matches)[0]
print(json.dumps(result))'''
    return _run_python_json(backend, code)


def _is_kb_vault_path(path: str) -> bool:
    """True if ``path`` points inside an agent's managed KB vault (agents/<id>/kb).

    KB vaults live under a gitignored ``agents/`` tree, so ripgrep — which respects
    .gitignore by default — would skip every doc and return zero matches. Callers
    use this to force a full search (``--no-ignore``) for the vault while normal
    code workspaces keep honoring ignore files.
    """
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    for i in range(len(parts) - 2):
        if parts[i] == 'agents' and parts[i + 2] == 'kb':
            return True
    return False


def _resolve_workspace(agent: dict, path: str) -> str:
    """Resolve paths lexically without consulting the host filesystem."""
    workspace = (agent or {}).get('workspace', '')
    if path == '/workspace' or path.startswith('/workspace/'):
        path = posixpath.join(workspace, path[len('/workspace'):].lstrip('/'))
    elif workspace and not posixpath.isabs(path):
        path = posixpath.join(workspace, path)
    return posixpath.abspath(path)


def _prepare_path(agent: dict, path: str, *, want_dir: bool | None = None) -> tuple:
    """Resolve and canonically confine a path inside the backend workspace."""
    backend = _backend(agent)
    resolved = backend.resolve_path(_resolve_workspace(agent, path))
    workspace = backend.resolve_path(_resolve_workspace(agent, '.'))
    lexical = posixpath.normpath(resolved)
    workspace_lexical = posixpath.normpath(workspace)
    if workspace and lexical != workspace_lexical and not lexical.startswith(workspace_lexical + '/'):
        raise PermissionError("Access denied: path escapes workspace")
    info = _path_info(backend, lexical)
    workspace_info = _path_info(backend, workspace_lexical)
    canonical, canonical_ws = info['path'], workspace_info['path']
    if canonical != canonical_ws and not canonical.startswith(canonical_ws.rstrip('/') + '/'):
        raise PermissionError("Access denied: path escapes workspace")
    if not info['exists'] and workspace:
        corrected = _auto_correct_path(backend, lexical, workspace_lexical, want_dir is True)
        if corrected != lexical:
            info = _path_info(backend, corrected)
            canonical = info['path']
            if canonical != canonical_ws and not canonical.startswith(canonical_ws.rstrip('/') + '/'):
                raise PermissionError("Access denied: path escapes workspace")
    return backend, canonical, info


def _validate_workspace_boundary(resolved_path: str, workspace: str) -> str:
    """Validate that resolved_path stays within the workspace boundary.

    Uses os.path.realpath to resolve all symlinks and canonicalize both paths,
    then checks whether the resolved path is equal to or a subpath of the
    workspace. This blocks three attack vectors:

    1. Relative path traversal (``../../etc/passwd``)
    2. Absolute path escape (``/etc/shadow``)
    3. Symlink attacks (symlink inside workspace pointing to outside)

    Returns the resolved canonical path on success. Raises PermissionError if
    the path escapes the workspace.

    This function is a no-op for agents without a workspace set.
    """
    workspace_real = os.path.realpath(workspace)
    path_real = os.path.realpath(resolved_path)
    if path_real == workspace_real or path_real.startswith(workspace_real + os.sep):
        return path_real
    raise PermissionError("Access denied: path escapes workspace")
