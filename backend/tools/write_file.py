"""Backend implementation for the write_file tool — writes full content to a file."""

import os

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.tools._workspace import resolve_workspace_path
from backend.tools.lib.safety_pipeline import should_skip_safety

_STR_REPLACE_STEPS = """1. First, call read_file() to see the current content.
2. Then call {tool} with old_str set to the exact lines you want to change (copy them from read_file's output).
3. Set new_str to your replacement text.
Retrying write_file will be refused again."""

_PATCH_STEPS = """1. First, call read_file() to see the current content.
2. Then call patch with a unified diff containing your changes (use read_file's output as reference).
3. Ensure the patch has proper @@ hunk headers.
Retrying write_file will be refused again."""

_EDIT_RECIPE_STR_REPLACE = {
    'tool': 'str_replace',
    'old_str': '<READ the file first with read_file, then paste the exact text to replace here>',
    'new_str': '<your replacement text here>',
}

_EDIT_RECIPE_PATCH = {
    'tool': 'patch',
    'patch': '<READ the file first with read_file, then construct a unified diff patch here>',
}


def _get_edit_suggestion(agent):
    """Determine which edit tool to suggest based on agent's assigned tools.

    Returns (tool_name_str, edit_recipe_dict).
    """
    assigned = set((agent or {}).get('assigned_tool_ids', []))
    has_str_replace = 'str_replace' in assigned
    has_patch = 'patch' in assigned

    if has_str_replace and has_patch:
        return ('str_replace or patch', _EDIT_RECIPE_STR_REPLACE)
    elif has_patch:
        return ('patch', _EDIT_RECIPE_PATCH)
    else:
        # Default to str_replace (fallback when only str_replace or none assigned)
        return ('str_replace', _EDIT_RECIPE_STR_REPLACE)


def write_file(
    file_path: str,
    content: str,
    overwrite: bool = True,
    create_dirs: bool = True,
    edit_suggestion: tuple = None,
) -> dict:
    """
    Write content to a file.

    Args:
        file_path:   Target file path (absolute or relative).
        content:     Full content to write. Written exactly as provided.
        overwrite:   If False, refuse to write if the file already exists.
        create_dirs: If True, create missing parent directories automatically.
        edit_suggestion: Optional (tool_name, recipe_dict) from _get_edit_suggestion().

    Returns:
        dict with 'result', 'created' on success,
        or 'error' on failure.
    """
    if not file_path:
        return {'error': "Missing required argument: 'file_path'"}
    if content is None:
        return {'error': "Missing required argument: 'content'"}

    abs_path = os.path.abspath(file_path)
    already_exists = os.path.exists(abs_path)

    # Resolve edit suggestion: use provided, or derive from what the agent has,
    # or fall back to str_replace.
    if edit_suggestion is not None:
        edit_tool_name, edit_recipe = edit_suggestion
    else:
        edit_tool_name, edit_recipe = _get_edit_suggestion(None)

    if edit_tool_name == 'patch':
        steps = _PATCH_STEPS
    else:
        steps = _STR_REPLACE_STEPS

    # Write-vs-Edit guard: Write tool is for NEW files only.
    # Existing files MUST be modified via str_replace/patch (surgical edits),
    # never by overwriting the entire file. This invariant forces the
    # model to make precise, targeted changes instead of lazy wholesale
    # rewrites — the single highest-impact mechanism from little-coder.
    if already_exists:
        error_msg = (
            f"File already exists: {file_path}. "
            "The write_file tool is for creating NEW files only — "
            "it does NOT overwrite existing files. "
            f"To modify an existing file, use {edit_tool_name} instead.\n"
            f"{steps}"
        )
        recipe = dict(edit_recipe)
        recipe['file_path'] = file_path
        return {
            'error': error_msg,
            'isError': True,
            'edit_recipe': recipe,
        }

    # Create parent directories
    parent = os.path.dirname(abs_path)
    if parent:
        if not os.path.exists(parent):
            if create_dirs:
                try:
                    os.makedirs(parent, exist_ok=True)
                except PermissionError:
                    return {'error': f"Permission denied creating directories: {parent}"}
                except Exception as e:
                    return {'error': f"Failed to create directories: {e}"}
            else:
                return {
                    'error': (
                        f"Parent directory does not exist: {parent}. "
                        "Set create_dirs=true to create it automatically."
                    )
                }

    # Write
    try:
        encoded = content.encode('utf-8')
        with open(abs_path, 'wb') as f:
            f.write(encoded)
    except PermissionError:
        return {'error': f"Permission denied writing: {file_path}"}
    except IsADirectoryError:
        return {'error': f"Path is a directory, not a file: {file_path}"}
    except Exception as e:
        return {'error': f"Error writing file: {e}"}

    return {
        'result': 'success',
        'bytes_written': len(encoded),
        'created': not already_exists,
    }


def execute(agent, args: dict) -> dict:
    file_path = args.get('file_path')
    content = args.get('content')
    overwrite = args.get('overwrite', True)
    create_dirs = args.get('create_dirs', True)

    # Compute dynamic edit tool suggestion based on agent's assigned tools
    edit_suggestion = _get_edit_suggestion(agent)

    # Heuristic safety check: block access to .ssh directory
    if not should_skip_safety(agent) and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_ssh_path
        ssh_check = check_ssh_path(file_path, agent)
        if ssh_check["blocked"]:
            return {"error": ssh_check["error"]}

    # Heuristic safety check: require approval for SQLite database access
    if not should_skip_safety(agent) and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_sqlite_path
        db_check = check_sqlite_path(file_path, agent)
        if db_check["blocked"]:
            return {
                "error": db_check["error"],
                "level": "requires_approval",
                "reasons": [db_check["reason"]],
                "approval_info": {
                    "risk_level": "medium",
                    "description": "Writing to SQLite database files may corrupt or expose sensitive data.",
                    "file_path": file_path,
                },
            }

    # Heuristic safety check: require approval for sensitive system paths
    if not should_skip_safety(agent) and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_sensitive_path
        path_check = check_sensitive_path(file_path, agent)
        if path_check["blocked"]:
            return {
                "error": path_check["error"],
                "level": "requires_approval",
                "reasons": [path_check["reason"]],
                "approval_info": {
                    "risk_level": "medium",
                    "description": "Writing to sensitive system paths may compromise system integrity.",
                    "file_path": file_path,
                },
            }

    # Heuristic safety check: require approval for .env files
    if not should_skip_safety(agent) and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_env_path
        env_check = check_env_path(file_path, agent)
        if env_check["blocked"]:
            return {
                "error": env_check["error"],
                "level": "requires_approval",
                "reasons": [env_check["reason"]],
                "approval_info": {
                    "risk_level": "high",
                    "description": "Writing to environment files may expose or corrupt secrets, API keys, or passwords.",
                    "file_path": file_path,
                },
            }

    # Normalize booleans in case they arrive as strings from the LLM
    if isinstance(overwrite, str):
        overwrite = overwrite.lower() not in ('false', '0', 'no')
    if isinstance(create_dirs, str):
        create_dirs = create_dirs.lower() not in ('false', '0', 'no')

    # Normalise smart quotes in code content before writing
    from backend.normalizer import normalize_code_quotes
    content = normalize_code_quotes(content)

    if file_path is None:
        return {'error': "Missing required argument: 'file_path'"}
    if content is None:
        return {'error': "Missing required argument: 'content'"}

    # /_self/ path: always route to the agent's local directory on the evonic server.
    from backend.tools._workspace import is_self_path, resolve_self_path
    agent_id = (agent or {}).get('id')
    if agent_id and is_self_path(file_path):
        local_path = resolve_self_path(agent_id, file_path)
        if not local_path:
            return {'error': "Access denied — path escapes agent directory."}
        return write_file(local_path, content, overwrite=overwrite, create_dirs=create_dirs, edit_suggestion=edit_suggestion)

    # /_portal/ path: route through a virtual path mapping to local/SSH/evonet.
    from backend.tools._portal import is_portal_path, resolve_portal_path
    if agent_id and is_portal_path(file_path):
        backend, real_path = resolve_portal_path(agent_id, file_path)
        if backend is None:
            return {'error': real_path}  # error message
        already_exists = backend.file_exists(real_path)
        if already_exists:
            edit_tool_name, edit_recipe = edit_suggestion
            if edit_tool_name == 'patch':
                steps = _PATCH_STEPS
            else:
                steps = _STR_REPLACE_STEPS
            error_msg = (
                f"File already exists: {file_path}. "
                "The write_file tool is for creating NEW files only — "
                "it does NOT overwrite existing files. "
                f"To modify an existing file, use {edit_tool_name} instead.\n"
                f"{steps}"
            )
            recipe = dict(edit_recipe)
            recipe['file_path'] = file_path
            return {
                'error': error_msg,
                'isError': True,
                'edit_recipe': recipe,
            }
        parent = real_path.rsplit("/", 1)[0] if "/" in real_path else ""
        if parent and create_dirs and not backend.file_exists(parent):
            result = backend.make_dirs(parent)
            if 'error' in result:
                return result
        result = backend.write_file(real_path, content, create_dirs=False)
        if 'error' in result:
            return result
        return {
            'result': 'success',
            'bytes_written': len(content.encode('utf-8')),
            'created': not already_exists,
        }

    # When sandbox is enabled, route file I/O through the execution backend
    # (Docker container, SSH remote, etc.) instead of the host filesystem.
    sandbox_enabled = (agent or {}).get('sandbox_enabled', 1)
    if sandbox_enabled:
        from backend.tools.lib.exec_backend import registry
        session_id = (agent or {}).get('session_id') or 'default'
        backend = registry.get_backend(session_id, agent)

        # Resolve the file path relative to the agent's workspace before
        # sending it to the execution backend (container/SSH).  This ensures
        # that relative paths and bare file names are resolved against the
        # correct workspace directory.
        target_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
        # Convert host path to the backend's view (e.g. /workspace for Docker)
        target_path = backend.resolve_path(target_path)
        already_exists = backend.file_exists(target_path)

        # Write-vs-Edit guard: Write tool is for NEW files only.
        if already_exists:
            edit_tool_name, edit_recipe = edit_suggestion
            if edit_tool_name == 'patch':
                steps = _PATCH_STEPS
            else:
                steps = _STR_REPLACE_STEPS
            error_msg = (
                f"File already exists: {file_path}. "
                "The write_file tool is for creating NEW files only — "
                "it does NOT overwrite existing files. "
                f"To modify an existing file, use {edit_tool_name} instead.\n"
                f"{steps}"
            )
            recipe = dict(edit_recipe)
            recipe['file_path'] = file_path
            return {
                'error': error_msg,
                'isError': True,
                'edit_recipe': recipe,
            }

        # Create parent directories if needed
        if create_dirs:
            parent = os.path.dirname(target_path)
            if parent and parent != '/' and not backend.file_exists(parent):
                result = backend.make_dirs(parent)
                if 'error' in result:
                    return result

        result = backend.write_file(target_path, content, create_dirs=False)
        if 'error' in result:
            return result

        return {
            'result': 'success',
            'bytes_written': len(content.encode('utf-8')),
            'created': not already_exists,
        }

    # No sandbox — direct host filesystem access (original behavior)
    file_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
    return write_file(file_path, content, overwrite=overwrite, create_dirs=create_dirs, edit_suggestion=edit_suggestion)


# ---------------------------------------------------------------------------
# Self-tests (run with: python3 backend/tools/write_file.py)
# ---------------------------------------------------------------------------

def test_execute():
    import tempfile, shutil

    tmp_dir = tempfile.mkdtemp()
    passed = 0

    def path(*parts):
        return os.path.join(tmp_dir, *parts)

    # ------------------------------------------------------------------
    print('Test 1: Create a new file')
    p = path('hello.txt')
    r = write_file(p, 'hello world\n')
    assert r['result'] == 'success', r
    assert r['created'] is True, r
    assert r['bytes_written'] == len('hello world\n'.encode()), r
    assert open(p).read() == 'hello world\n'
    passed += 1

    # ------------------------------------------------------------------
    print('Test 2: Write-vs-Edit guard refuses existing file (overwrite default)')
    r = write_file(p, 'new content\n')
    assert 'error' in r, r
    assert r.get('isError') is True, r
    assert 'edit_recipe' in r, r
    assert r['edit_recipe']['tool'] == 'str_replace', r
    assert open(p).read() == 'hello world\n'  # unchanged
    passed += 1

    # ------------------------------------------------------------------
    print('Test 3: Write-vs-Edit guard refuses existing file (overwrite=False)')
    r = write_file(p, 'blocked', overwrite=False)
    assert 'error' in r, r
    assert r.get('isError') is True, r
    assert 'edit_recipe' in r, r
    assert open(p).read() == 'hello world\n'  # unchanged
    passed += 1

    # ------------------------------------------------------------------
    print('Test 4: Write creates a new file')
    p2 = path('brand_new.txt')
    r = write_file(p2, 'fresh', overwrite=False)
    assert r['result'] == 'success', r
    assert r['created'] is True, r
    passed += 1

    # ------------------------------------------------------------------
    print('Test 5: create_dirs=True auto-creates nested directories')
    deep = path('a', 'b', 'c', 'deep.txt')
    r = write_file(deep, 'deep content', create_dirs=True)
    assert r['result'] == 'success', r
    assert open(deep).read() == 'deep content'
    passed += 1

    # ------------------------------------------------------------------
    print('Test 6: create_dirs=False fails when parent missing')
    missing = path('nonexistent', 'file.txt')
    r = write_file(missing, 'data', create_dirs=False)
    assert 'error' in r, r
    passed += 1

    # ------------------------------------------------------------------
    print('Test 7: Content preserved exactly (no extra newline)')
    p3 = path('exact.txt')
    r = write_file(p3, 'no trailing newline')
    assert open(p3).read() == 'no trailing newline'
    passed += 1

    # ------------------------------------------------------------------
    print('Test 8: Unicode content written correctly')
    p4 = path('unicode.txt')
    text = 'Héllo wörld — 日本語 🎉\n'
    r = write_file(p4, text)
    assert r['result'] == 'success', r
    assert open(p4, encoding='utf-8').read() == text
    passed += 1

    # ------------------------------------------------------------------
    print('Test 9: Empty content is valid')
    p5 = path('empty.txt')
    r = write_file(p5, '')
    assert r['result'] == 'success', r
    assert r['bytes_written'] == 0, r
    assert open(p5).read() == ''
    passed += 1

    # ------------------------------------------------------------------
    print('Test 10: Missing file_path returns error')
    r = write_file('', 'data')
    assert 'error' in r, r
    passed += 1

    # ------------------------------------------------------------------
    print('Test 11: Missing content returns error')
    r = write_file(path('x.txt'), None)
    assert 'error' in r, r
    passed += 1

    # ------------------------------------------------------------------
    print('Test 12: String boolean args normalised (LLM may send strings)')
    p6 = path('strflag.txt')
    r = execute({'sandbox_enabled': 0}, {'file_path': p6, 'content': 'ok', 'overwrite': 'true', 'create_dirs': 'true'})
    assert r['result'] == 'success', r
    r2 = execute({'sandbox_enabled': 0}, {'file_path': p6, 'content': 'blocked', 'overwrite': 'false'})
    assert 'error' in r2, r2
    passed += 1

    # Cleanup
    shutil.rmtree(tmp_dir)
    print(f'\nAll {passed} tests passed!')


if __name__ == '__main__':
    test_execute()
