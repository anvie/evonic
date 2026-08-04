"""
Tool: send_file — send a file as an attachment to the user via
the active messaging channel (Telegram, WhatsApp, etc.).

Agents can use this to deliver PDFs, images, documents, spreadsheets,
or any output file directly to the user through the active channel.

Usage:
  - file_path (required): absolute or relative path to the file
  - caption (optional): text caption to send alongside the file
  - mime_type (optional): MIME type; auto-detected if omitted
"""

import os
import re
import tempfile


_POLICY_ERROR = "File attachment is not permitted by the configured policy."


def _path_policy_pattern(agent: dict):
    return str((agent or {}).get("send_file_allowed_path_regex") or "").strip()


def _check_path_policy(agent: dict, canonical_path: str):
    """Apply the core per-agent regex and generic attachment policies.

    ``canonical_path`` must already be resolved by the relevant filesystem
    backend. No file metadata or content should be exposed before this check.
    """
    pattern = _path_policy_pattern(agent)
    if pattern:
        try:
            if not re.search(pattern, canonical_path):
                return {"error": _POLICY_ERROR}
        except (re.error, OSError, ValueError, TypeError):
            return {"error": _POLICY_ERROR}

    from backend.plugin_hooks import check_attachment_policies
    return check_attachment_policies(agent or {}, canonical_path)


def _check_self_request_policy(agent: dict, requested_path: str):
    """Honor policies that explicitly refer to the virtual ``/_self`` path.

    The configured regex has historically matched canonical filesystem paths.
    For virtual self paths, also enforce expressions that mention ``/_self``
    against the original request so a negative lookahead cannot be bypassed by
    canonicalization. Policies that do not mention the virtual prefix retain
    their canonical-path behavior in ``_check_path_policy``.
    """
    pattern = _path_policy_pattern(agent)
    if "/_self" not in pattern:
        return None
    try:
        if not re.search(pattern, requested_path):
            return {"error": _POLICY_ERROR}
    except (re.error, OSError, ValueError, TypeError):
        return {"error": _POLICY_ERROR}
    return None

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _get_workplace_backend(agent: dict, session_id: str):
    """Return the execution backend for an agent with workplace/sandbox, or None."""
    workplace_id = agent.get('workplace_id')
    sandbox_enabled = agent.get('sandbox_enabled', False)
    run_as_user = bool(((agent or {}).get('run_as_user') or '').strip())

    if not workplace_id and not sandbox_enabled and not run_as_user:
        return None

    if workplace_id:
        from backend.workplaces.manager import workplace_manager
        return workplace_manager.get_backend(workplace_id, sandbox_enabled=sandbox_enabled)
    elif sandbox_enabled:
        from backend.tools.lib.exec_backend import registry as exec_registry
        return exec_registry.get_backend(session_id, agent)
    else:
        from backend.tools.lib.backends.local_backend import LocalBackend
        return LocalBackend(session_id=session_id)


def execute(agent: dict, args: dict) -> dict:
    session_id = agent.get("session_id", "default")
    if not session_id or session_id == "default":
        return {"error": "No active session — cannot send file without a session"}

    file_path = (args.get("file_path") or "").strip()
    caption = args.get("caption") or None
    mime_type = args.get("mime_type") or None

    if not file_path:
        return {
            "error": 'The "file_path" parameter is required. '
                     'Provide the path to the file to send, '
                     'e.g. file_path="output/report.pdf"'
        }

    # /_self/ path: resolve to agent's local directory on the evonic server.
    # /_self/ paths are always local — they don't go through a workplace backend.
    agent_id = (agent or {}).get('id', '')
    is_self = False
    if agent_id:
        from backend.tools._workspace import is_self_path, resolve_self_path
        if is_self_path(file_path):
            is_self = True
            policy_error = _check_self_request_policy(agent, file_path)
            if policy_error:
                return policy_error
            resolved = resolve_self_path(agent_id, file_path)
            if not resolved:
                return {"error": f"File not found: \"{file_path}\" — path outside agent directory"}
            if not os.path.exists(resolved):
                return {"error": f'File not found: "{file_path}"'}
            if not os.path.isfile(resolved):
                return {"error": f'Path is not a file: "{file_path}"'}
            policy_error = _check_path_policy(agent, os.path.realpath(resolved))
            if policy_error:
                return policy_error
            try:
                file_size = os.path.getsize(resolved)
            except OSError:
                return {"error": "Unable to access the requested file."}
            file_path = resolved

    if not is_self:
        # Non-/self/ path: check for workplace/sandbox backend first
        backend = _get_workplace_backend(agent, session_id)

        if backend is not None:
            # Resolve path through the workplace/sandbox backend
            from backend.tools._workspace import resolve_workspace_path
            target_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
            target_path = backend.resolve_path(target_path) if hasattr(backend, 'resolve_path') else target_path

            # Check file exists on the remote filesystem
            st = backend.file_stat(target_path)
            if not st.get('exists'):
                return {"error": f'File not found: "{file_path}"'}
            if st.get('is_dir'):
                return {"error": f'Path is not a file: "{file_path}"'}

            file_size = st.get('size', 0)
            policy_error = _check_path_policy(agent, os.path.realpath(target_path))
            if policy_error:
                return policy_error

            # Fetch file bytes from remote and stage locally for channel delivery
            result = backend.cat_file_bytes(target_path)
            if 'error' in result:
                return {"error": f'Failed to read file from workplace: {result["error"]}'}

            ext = os.path.splitext(file_path)[1]
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            try:
                tmp.write(result['bytes'])
            finally:
                tmp.close()
            file_path = tmp.name
        else:
            # No workplace/sandbox: direct local filesystem access
            if not os.path.isabs(file_path):
                file_path = os.path.join(_WORKSPACE_ROOT, file_path)

            if not os.path.exists(file_path):
                return {"error": f'File not found: "{file_path}"'}
            if not os.path.isfile(file_path):
                return {"error": f'Path is not a file: "{file_path}"'}

            file_path = os.path.realpath(file_path)
            policy_error = _check_path_policy(agent, file_path)
            if policy_error:
                return policy_error
            try:
                file_size = os.path.getsize(file_path)
            except OSError as e:
                return {"error": f'Cannot access file "{file_path}": {e}'}

    # Send via channel — lazy import to avoid circular deps
    try:
        from backend.agent_runtime import agent_runtime

        success = agent_runtime.send_file_as_bot(
            session_id, file_path, caption, mime_type
        )
    except Exception as e:
        return {"error": f"Failed to send file: {e}"}

    if not success:
        return {"error": "Failed to send file — channel may be unavailable"}

    return {
        "result": "File sent successfully",
        "file_path": file_path,
        "file_name": os.path.basename(file_path),
        "file_size": file_size,
    }
