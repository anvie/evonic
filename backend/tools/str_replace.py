"""Backend implementation for the str_replace tool — exact-string replacement in files."""

import os

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.tools._workspace import resolve_workspace_path


def str_replace(file_path: str, old_str: str, new_str: str, count: int = 1) -> dict:
    """
    Replace an exact string occurrence in a file.

    Unlike patch, this tool does not require line numbers — it finds the
    exact text in the file and replaces it. This makes it reliable even
    after other edits have shifted line numbers.

    Args:
        file_path: Path to the file to edit.
        old_str:   The exact text to find and replace. Must be unique in the
                   file (or match exactly `count` times). Include enough
                   surrounding context to make it unambiguous.
        new_str:   The replacement text. Use an empty string to delete old_str.
        count:     Number of replacements to make (default 1). If the file
                   contains a different number of occurrences than `count`,
                   an error is returned.

    Returns:
        dict with 'result' and 'replacements' on success, or 'error' on failure.
    """
    if not file_path:
        return {'error': "Missing required argument: 'file_path'"}
    if old_str is None:
        return {'error': "Missing required argument: 'old_str'"}
    if new_str is None:
        return {'error': "Missing required argument: 'new_str'"}
    if not old_str:
        return {'error': "'old_str' must not be empty"}

    if not os.path.exists(file_path):
        return {'error': f"File not found: {file_path}"}

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except PermissionError:
        return {'error': f"Permission denied reading: {file_path}"}
    except UnicodeDecodeError:
        return {'error': f"File is not valid UTF-8 text: {file_path}"}
    except OSError as e:
        return {'error': str(e)}

    occurrences = content.count(old_str)

    if occurrences == 0:
        return {
            'error': (
                f"'old_str' not found in {file_path}. "
                "Action: call read_file() to get the current file content "
                "and copy the exact text you want to replace."
            )
        }

    if occurrences != count:
        return {
            'error': (
                f"'old_str' found {occurrences} time(s) in {file_path}, "
                f"but count={count}. "
                "Make 'old_str' more specific by including more surrounding context, "
                f"or set count={occurrences} if you intend to replace all occurrences."
            )
        }

    new_content = content.replace(old_str, new_str, count)

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except PermissionError:
        return {'error': f"Permission denied writing: {file_path}"}
    except OSError as e:
        return {'error': str(e)}

    return {'result': 'success', 'replacements': count}


def execute(agent, args: dict) -> dict:
    file_path = args.get('file_path')
    display_path = file_path  # keep original path for error messages
    old_str = args.get('old_str')
    new_str = args.get('new_str')
    count = args.get('count', 1)

    # Heuristic safety check: block access to .ssh directory
    if not (agent or {}).get('_skip_safety') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_ssh_path
        ssh_check = check_ssh_path(file_path, agent)
        if ssh_check["blocked"]:
            return {"error": ssh_check["error"]}

    # Heuristic safety check: require approval for sensitive system paths
    if not (agent or {}).get('_skip_safety') and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_sensitive_path
        path_check = check_sensitive_path(file_path, agent)
        if path_check["blocked"]:
            return {
                "error": path_check["error"],
                "level": "requires_approval",
                "reasons": [path_check["reason"]],
                "approval_info": {
                    "risk_level": "medium",
                    "description": "Modifying sensitive system paths may compromise system integrity.",
                    "file_path": file_path,
                },
            }

    # Heuristic safety check: require approval for .env files
    if not (agent or {}).get('_skip_safety') and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_env_path
        env_check = check_env_path(file_path, agent)
        if env_check["blocked"]:
            return {
                "error": env_check["error"],
                "level": "requires_approval",
                "reasons": [env_check["reason"]],
                "approval_info": {
                    "risk_level": "high",
                    "description": "Modifying environment files may expose or corrupt secrets, API keys, or passwords.",
                    "file_path": file_path,
                },
            }

    if isinstance(count, str):
        try:
            count = int(count)
        except ValueError:
            return {'error': f"'count' must be an integer, got: {count!r}"}

    if not file_path:
        return {'error': "Missing required argument: 'file_path'"}
    if old_str is None:
        return {'error': "Missing required argument: 'old_str'"}
    if new_str is None:
        return {'error': "Missing required argument: 'new_str'"}
    if not old_str:
        return {'error': "'old_str' must not be empty"}

    # Normalise smart quotes in replacement content before applying
    from backend.normalizer import normalize_code_quotes
    new_str = normalize_code_quotes(new_str)

    # /_self/ path: always route to the agent's local directory on the evonic server.
    from backend.tools._workspace import is_self_path, resolve_self_path
    agent_id = (agent or {}).get('id')
    if agent_id and is_self_path(file_path):
        local_path = resolve_self_path(agent_id, file_path)
        if not local_path:
            return {'error': "Access denied — path escapes agent directory."}
        result = str_replace(local_path, old_str, new_str, count=count)
        if 'error' in result and display_path != local_path:
            result['error'] = result['error'].replace(local_path, display_path)
        return result

    # /_portal/ path: route through a virtual path mapping to local/SSH/evonet.
    from backend.tools._portal import is_portal_path, resolve_portal_path
    if agent_id and is_portal_path(file_path):
        backend, real_path = resolve_portal_path(agent_id, file_path)
        if backend is None:
            return {'error': real_path}  # error message

        if not backend.file_exists(real_path):
            return {'error': f"File not found: {display_path}"}

        read_result = backend.read_file(real_path)
        if 'error' in read_result:
            return {'error': read_result['error']}

        content = read_result['content']
        occurrences = content.count(old_str)

        if occurrences == 0:
            return {
                'error': (
                    f"'old_str' not found in {display_path}. "
                    "Action: call read_file() to get the current file content "
                    "and copy the exact text you want to replace."
                )
            }

        if occurrences != count:
            return {
                'error': (
                    f"'old_str' found {occurrences} time(s) in {display_path}, "
                    f"but count={count}. "
                    "Make 'old_str' more specific by including more surrounding context, "
                    f"or set count={occurrences} if you intend to replace all occurrences."
                )
            }

        new_content = content.replace(old_str, new_str, count)
        wr = backend.write_file(real_path, new_content)
        if 'error' in wr:
            return {'error': wr['error']}

        return {'result': 'success', 'replacements': count}

    # When sandbox is enabled, route file I/O through the execution backend.
    sandbox_enabled = (agent or {}).get('sandbox_enabled', 1)
    if sandbox_enabled:
        from backend.tools.lib.exec_backend import registry
        session_id = (agent or {}).get('session_id') or 'default'
        backend = registry.get_backend(session_id, agent)

        # Resolve the file path relative to the agent's workspace before
        # sending it to the execution backend.
        target_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
        # Convert host path to the backend's view (e.g. /workspace for Docker)
        target_path = backend.resolve_path(target_path)

        if not backend.file_exists(target_path):
            return {'error': f"File not found: {display_path}"}

        result = backend.read_file(target_path)
        if 'error' in result:
            return {'error': result['error']}

        content = result['content']
        occurrences = content.count(old_str)

        if occurrences == 0:
            return {
                'error': (
                    f"'old_str' not found in {display_path}. "
                    "Action: call read_file() to get the current file content "
                    "and copy the exact text you want to replace."
                )
            }

        if occurrences != count:
            return {
                'error': (
                    f"'old_str' found {occurrences} time(s) in {display_path}, "
                    f"but count={count}. "
                    "Make 'old_str' more specific by including more surrounding context, "
                    f"or set count={occurrences} if you intend to replace all occurrences."
                )
            }

        new_content = content.replace(old_str, new_str, count)
        wr = backend.write_file(target_path, new_content)
        if 'error' in wr:
            return {'error': wr['error']}

        return {'result': 'success', 'replacements': count}

    # No sandbox — direct host filesystem access (original behavior)
    file_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
    result = str_replace(file_path, old_str, new_str, count=count)

    # Replace resolved absolute path with the original display path in error messages
    if 'error' in result and display_path and display_path != file_path:
        result['error'] = result['error'].replace(file_path, display_path)

    return result


# ---------------------------------------------------------------------------
# Self-tests (run with: python backend/tools/str_replace.py)
# ---------------------------------------------------------------------------

def test_execute():
    import tempfile

    def make_file(content):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        f.write(content)
        f.close()
        return f.name

    def read(path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    passed = 0

    print('Test 1: Basic replacement')
    p = make_file('hello world\n')
    r = str_replace(p, 'world', 'Python')
    assert r == {'result': 'success', 'replacements': 1}, r
    assert read(p) == 'hello Python\n'
    passed += 1

    print('Test 2: Multiline replacement')
    p = make_file('line one\nline two\nline three\n')
    r = str_replace(p, 'line one\nline two', 'replaced one\nreplaced two')
    assert r['result'] == 'success', r
    assert read(p) == 'replaced one\nreplaced two\nline three\n'
    passed += 1

    print('Test 3: Delete by replacing with empty string')
    p = make_file('keep this\ndelete this\nkeep this too\n')
    r = str_replace(p, 'delete this\n', '')
    assert r['result'] == 'success', r
    assert read(p) == 'keep this\nkeep this too\n'
    passed += 1

    print('Test 4: old_str not found → error with action hint')
    p = make_file('some content\n')
    r = str_replace(p, 'nonexistent text', 'replacement')
    assert 'error' in r, r
    assert 'read_file' in r['error'], r
    passed += 1

    print('Test 5: Ambiguous match (2 occurrences, count=1) → error')
    p = make_file('foo\nfoo\nbar\n')
    r = str_replace(p, 'foo', 'baz')
    assert 'error' in r, r
    assert '2 time(s)' in r['error'], r
    passed += 1

    print('Test 6: count=2 matches exactly 2 occurrences')
    p = make_file('foo\nfoo\nbar\n')
    r = str_replace(p, 'foo', 'baz', count=2)
    assert r['result'] == 'success', r
    assert read(p) == 'baz\nbaz\nbar\n'
    passed += 1

    print('Test 7: File not found → error')
    r = str_replace('/nonexistent/path.txt', 'x', 'y')
    assert 'error' in r, r
    passed += 1

    print('Test 8: Empty old_str → error')
    p = make_file('content\n')
    r = str_replace(p, '', 'something')
    assert 'error' in r, r
    passed += 1

    print('Test 9: Unicode content')
    p = make_file('Héllo wörld\n')
    r = str_replace(p, 'wörld', 'Python')
    assert r['result'] == 'success', r
    assert read(p) == 'Héllo Python\n'
    passed += 1

    print('Test 10: execute() with /workspace path mapping')
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    p = make_file('test content\n')
    r = execute({'workspace': os.path.dirname(p), 'sandbox_enabled': 0},
                {'file_path': p, 'old_str': 'test content', 'new_str': 'replaced'})
    assert r['result'] == 'success', r
    passed += 1

    print(f'\nAll {passed} tests passed!')


if __name__ == '__main__':
    test_execute()
