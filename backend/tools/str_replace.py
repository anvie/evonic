"""Backend implementation for the str_replace tool — exact-string replacement in files."""

import logging
import os

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

logger = logging.getLogger(__name__)

_SMART_QUOTES = '\u2018\u2019\u201c\u201d'

from backend.tools._workspace import resolve_workspace_path
try:
    from backend.tools.lib.safety_pipeline import should_skip_safety
except ImportError:
    logger.warning("safety_pipeline unavailable — safety checks disabled for str_replace tool")
    should_skip_safety = lambda agent: True

def _match_with_unicode_fallback(content, old_str, new_str):
    """Try to match old_str in content, falling back to \\uXXXX re-encoded form.

    Returns (effective_old_str, effective_new_str, occurrences).
    When the re-encoded form matches, new_str is also re-encoded to preserve
    the file's literal-escape convention.
    """
    occurrences = content.count(old_str)
    if occurrences > 0:
        return old_str, new_str, occurrences

    from backend.normalizer import reencode_unicode_escapes
    reencoded_old = reencode_unicode_escapes(old_str)
    if reencoded_old != old_str:
        occurrences = content.count(reencoded_old)
        if occurrences > 0:
            reencoded_new = reencode_unicode_escapes(new_str)
            return reencoded_old, reencoded_new, occurrences

    # Tier 3: quote normalization fallback
    # LLM may receive smart-quotified read_file output (from normalize_llm_text)
    # and copy it into old_str, but the file has straight quotes.
    from backend.normalizer import normalize_code_quotes
    norm_old = normalize_code_quotes(old_str)
    if norm_old != old_str:
        occurrences = content.count(norm_old)
        if occurrences > 0:
            norm_new = normalize_code_quotes(new_str)
            return norm_old, norm_new, occurrences

    # Tier 4: bidirectional quote normalization fallback
    # The file may contain smart quotes while the agent provides straight quotes,
    # or the reverse.  Normalize both sides and locate the match position in the
    # original content so we can extract the actual (smart-quoted) substring.
    norm_content = normalize_code_quotes(content)
    if norm_content != content or norm_old != old_str:
        if norm_old in norm_content:
            # locate the first occurrence in normalized content
            idx = norm_content.index(norm_old)
            effective_old = content[idx:idx + len(norm_old)]
            occurrences = content.count(effective_old)
            if occurrences > 0:
                effective_new = normalize_code_quotes(new_str)
                return effective_old, effective_new, occurrences

    return old_str, new_str, 0


def _close_match_hint(content, old_str):
    """Return a suggestion string if a quote-normalized version would match."""
    from backend.normalizer import normalize_code_quotes
    norm = normalize_code_quotes(old_str)
    if norm != old_str and norm in content:
        return f" Did you mean: {norm!r}? (quotes were normalized from smart \u2192 straight)"
    # Reverse case: file has smart quotes, agent provided straight quotes
    norm_content = normalize_code_quotes(content)
    norm_old = normalize_code_quotes(old_str)
    if norm_content != content and norm_old == old_str and norm_old in norm_content:
        return f" The file contains smart quotes matching your input. Try copying the exact text from read_file output."
    return ""


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

    old_str, new_str, occurrences = _match_with_unicode_fallback(content, old_str, new_str)

    if occurrences == 0:
        hint = _close_match_hint(content, old_str)
        return {
            'error': (
                f"'old_str' not found in {file_path}. "
                "Action: call read_file() to get the current file content "
                "and copy the exact text you want to replace."
                f"{hint}"
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
    if not should_skip_safety(agent) and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_ssh_path
        ssh_check = check_ssh_path(file_path, agent)
        if ssh_check["blocked"]:
            return {"error": ssh_check["error"]}

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
                    "description": "Modifying sensitive system paths may compromise system integrity.",
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

    # Normalise smart quotes in replacement content before applying, except the
    # ones old_str itself carries: those are copied from the file, and straightening
    # them would silently reword the surrounding text.
    from backend.normalizer import normalize_code_quotes
    kept = {ch for ch in _SMART_QUOTES if ch in old_str}
    new_str = ''.join(ch if ch in kept else normalize_code_quotes(ch) for ch in new_str)

    # /_self/ path: always route to the agent's local directory on the evonic server.
    # Sub-agents inherit their parent's directory — use effective agent ID.
    from backend.tools._workspace import is_self_path, resolve_self_path, _self_fuzzy_suggestion
    agent_id = ((agent or {}).get("parent_id") if (agent or {}).get("is_subagent")
                else (agent or {}).get("id", ""))
    if agent_id and is_self_path(file_path):
        local_path = resolve_self_path(agent_id, file_path)
        if not local_path:
            return {'error': "Access denied — path escapes agent directory."}
        # If the file doesn't exist, check for similar names (typos).
        if not os.path.exists(local_path):
            suggestion = _self_fuzzy_suggestion(agent_id, file_path)
            if suggestion:
                return {'error': f"File not found: {display_path}. Did you mean: {suggestion}"}
            return {'error': f"File not found: {display_path}."}
        result = str_replace(local_path, old_str, new_str, count=count)
        if 'error' in result and display_path != local_path:
            result['error'] = result['error'].replace(local_path, display_path)
        if '/kb/' in local_path and result.get('result') == 'success':
            try:
                from backend.agent_runtime.evomem_writer import mark_dirty
                mark_dirty(agent_id)
                logger.info("str_replace[%s]: kb edit detected, evomem sync scheduled", agent_id)
            except Exception as e:
                logger.warning("str_replace[%s]: failed to schedule evomem sync: %s", agent_id, e)
            if local_path.endswith('.md'):
                try:
                    from backend.agent_runtime.context import kb_frontmatter_warning
                    _warn = kb_frontmatter_warning(local_path)
                    if _warn:
                        result['warning'] = _warn
                except Exception:
                    pass
        return result

    # Hint when path starts with _self/ but missing leading slash
    if agent_id and file_path and (file_path.startswith('_self/') or file_path == '_self'):
        return {"error": f"File not found: {file_path}. If you meant to access an agent directory, use the prefix `/_self/` (with a leading slash)."}
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
        old_str, new_str, occurrences = _match_with_unicode_fallback(content, old_str, new_str)

        if occurrences == 0:
            hint = _close_match_hint(content, old_str)
            return {
                'error': (
                    f"'old_str' not found in {display_path}. "
                    "Action: call read_file() to get the current file content "
                    "and copy the exact text you want to replace."
                    f"{hint}"
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

    # When sandbox is enabled, the agent has a workplace, or run-as-user is
    # set, route file I/O through the execution backend (Docker container,
    # SSH remote, sudo -u <user>, etc.).
    sandbox_enabled = (agent or {}).get('sandbox_enabled', 1)
    has_workplace = bool((agent or {}).get('workplace_id'))
    run_as_user = bool(((agent or {}).get('run_as_user') or '').strip())
    if sandbox_enabled or has_workplace or run_as_user:
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
        old_str, new_str, occurrences = _match_with_unicode_fallback(content, old_str, new_str)

        if occurrences == 0:
            hint = _close_match_hint(content, old_str)
            return {
                'error': (
                    f"'old_str' not found in {display_path}. "
                    "Action: call read_file() to get the current file content "
                    "and copy the exact text you want to replace."
                    f"{hint}"
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

    # --- Unicode escape fallback tests ---

    print('Test 11: Unicode escape mismatch fallback')
    p = make_file('items: [\\u2022 first, \\u2022 second]\n')
    r = str_replace(p, '\u2022 first', '\u2022 replaced')
    assert r['result'] == 'success', r
    assert read(p) == 'items: [\\u2022 replaced, \\u2022 second]\n'
    passed += 1

    print('Test 12: Direct Unicode match (no fallback needed)')
    p = make_file('bullet: \u2022 item\n')  # actual bullet char in file
    r = str_replace(p, '\u2022 item', '- item')
    assert r['result'] == 'success', r
    assert read(p) == 'bullet: - item\n'
    passed += 1

    print('Test 13: Multiple unicode escapes in old_str')
    p = make_file('pair: \\u00e9\\u00e8\n')
    r = str_replace(p, '\u00e9\u00e8', 'ee')
    assert r['result'] == 'success', r
    assert read(p) == 'pair: ee\n'
    passed += 1

    print('Test 14: new_str re-encoded when fallback triggers')
    p = make_file('old: \\u2022\nnext line\n')
    r = str_replace(p, '\u2022', '\u2023')
    assert r['result'] == 'success', r
    assert read(p) == 'old: \\u2023\nnext line\n'
    passed += 1

    print(f'\nAll {passed} tests passed!')


if __name__ == '__main__':
    test_execute()
