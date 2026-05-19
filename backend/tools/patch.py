"""Backend implementation for the patch tool — applies unified diff patches to files.

Primary backend: system `patch` utility (used when available on PATH).
Fallback backend: pure-Python implementation that is reliable for all hunk types,
including insertion-only hunks with no surrounding context.
"""

import os
import re

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.tools._workspace import resolve_workspace_path

SEARCH_WINDOW = 50


def _unescape_llm(s: str) -> str:
    """Remove common LLM double-escaping from a string."""
    return s.replace('\\"', '"').replace("\\'", "'")


def _normalize_for_match(s: str) -> str:
    """Normalize a string for fuzzy matching.

    Handles LLM double-escaping AND JSON unicode escapes (\\uXXXX → char),
    so that e.g. ``\\u2192`` in the file matches ``→`` in the patch.
    """
    s = _unescape_llm(s)
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)


# ---------------------------------------------------------------------------
# Patch parser
# ---------------------------------------------------------------------------

def parse_hunks(patch_text: str) -> list:
    """
    Parse a unified diff string into a list of hunk dicts.

    Each hunk:
        {
            'old_start': int,   # 1-based line number in original
            'old_count': int,
            'new_start': int,
            'new_count': int,
            'lines': list of (op, content, no_newline)
                op: ' ' context, '-' remove, '+' add
                content: str without prefix and without trailing newline
                no_newline: True if followed by \\ No newline marker
        }
    """
    hunks = []
    current_hunk = None

    for raw_line in patch_text.splitlines():
        line = raw_line.rstrip('\r\n')

        if re.match(r'^(diff --git|index |old mode|new mode|deleted file|new file)', line):
            continue

        if line.startswith('--- ') or line.startswith('+++ '):
            if current_hunk is not None:
                hunks.append(current_hunk)
                current_hunk = None
            continue

        hunk_match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if hunk_match:
            if current_hunk is not None:
                hunks.append(current_hunk)
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            current_hunk = {
                'old_start': old_start,
                'old_count': old_count,
                'new_start': new_start,
                'new_count': new_count,
                'lines': [],
            }
            continue

        if current_hunk is None:
            continue

        if line.startswith('\\ '):
            if current_hunk['lines']:
                op, content, _ = current_hunk['lines'][-1]
                current_hunk['lines'][-1] = (op, content, True)
            continue

        if line.startswith('-'):
            current_hunk['lines'].append(('-', line[1:], False))
        elif line.startswith('+'):
            current_hunk['lines'].append(('+', line[1:], False))
        elif line.startswith(' '):
            current_hunk['lines'].append((' ', line[1:], False))
        else:
            current_hunk['lines'].append((' ', line, False))

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks


# ---------------------------------------------------------------------------
# Pure-Python fallback helpers
# ---------------------------------------------------------------------------

def _find_first_anchor(lines: list, hunk_lines: list) -> int:
    """
    Scan the file for the first context/removal line from the hunk.
    Used to build helpful error hints.
    Returns 0-based index, or -1 if not found.
    `lines` may contain line endings (readlines output) or bare strings.
    """
    for op, txt, _ in hunk_lines:
        if op in (' ', '-'):
            needle = txt.rstrip()
            for i, line in enumerate(lines):
                if line.rstrip('\r\n').rstrip() == needle:
                    return i
            break  # only search for the very first context/removal line
    return -1


def _find_hunk_pos(lines: list, hunk_lines: list, stated_pos: int,
                   fuzzy: bool = True) -> tuple:
    """
    Find the 0-based position in `lines` where the hunk should be applied.

    For insertion-only hunks (no context or removal lines) the stated position
    is trusted directly — no search is needed.

    For hunks with context/removal lines, uses tiered matching:
      1. Exact match (trailing whitespace stripped) within ±SEARCH_WINDOW
      2. Indent-tolerant match (all whitespace stripped) within ±SEARCH_WINDOW
      3. Indent-tolerant match across the entire file
      4. Unescape-tolerant match (handles LLM double-escaping like \\" → ")

    `lines` may contain line endings or bare strings — both are handled.

    Returns (pos, None) on success, (pos, 'unescape') when matched via
    unescape-tolerant tier, or (-1, None) if no match found.
    """
    to_match = [(op, txt) for op, txt, _ in hunk_lines if op in (' ', '-')]

    # Insertion-only: no context to verify, trust the stated line number.
    if not to_match:
        pos = max(0, min(stated_pos, len(lines)))
        return (pos, None)

    match_len = len(to_match)
    window = SEARCH_WINDOW if fuzzy else 0

    # --- Tier 1: exact match (trailing whitespace stripped), ±window ---
    for delta in range(window + 1):
        for sign in ([0] if delta == 0 else [1, -1]):
            pos = stated_pos + sign * delta
            if pos < 0 or pos + match_len > len(lines):
                continue
            if all(
                lines[pos + i].rstrip('\r\n').rstrip() == to_match[i][1].rstrip()
                for i in range(match_len)
            ):
                return (pos, None)

    if not fuzzy:
        return (-1, None)

    # Pre-compute stripped versions for tier 2 and 3.
    match_stripped = [txt.strip() for _, txt in to_match]
    lines_stripped = [l.rstrip('\r\n').strip() for l in lines]

    # --- Tier 2: indent-tolerant match, ±window ---
    for delta in range(window + 1):
        for sign in ([0] if delta == 0 else [1, -1]):
            pos = stated_pos + sign * delta
            if pos < 0 or pos + match_len > len(lines_stripped):
                continue
            if all(
                lines_stripped[pos + i] == match_stripped[i]
                for i in range(match_len)
            ):
                return (pos, None)

    # --- Tier 3: indent-tolerant match, full-file scan ---
    for pos in range(len(lines_stripped) - match_len + 1):
        if all(
            lines_stripped[pos + i] == match_stripped[i]
            for i in range(match_len)
        ):
            return (pos, None)

    # --- Tier 4: normalize-tolerant match (LLM double-escaping + \uXXXX) ---
    # Handles: \" vs ", \' vs ', \u2192 vs → (either side may have either form)
    match_norm = [_normalize_for_match(txt).rstrip() for _, txt in to_match]
    lines_norm = [_normalize_for_match(l.rstrip('\r\n')).rstrip() for l in lines]
    if (match_norm != [txt.rstrip() for _, txt in to_match] or
            lines_norm != [l.rstrip('\r\n').rstrip() for l in lines]):
        for pos in range(len(lines_norm) - match_len + 1):
            if all(
                lines_norm[pos + i] == match_norm[i]
                for i in range(match_len)
            ):
                return (pos, 'unescape')

    return (-1, None)


def _apply_hunks_to_content(raw: str, patch_text: str) -> dict:
    """Pure-Python hunk application on a raw content string.

    Same logic as apply_hunks() but operates on an in-memory string
    instead of reading/writing a file. Used by the sandboxed code path
    where file I/O goes through the execution backend.
    Returns {'result': 'success', 'content': str, 'hunks_applied': int}
    or {'error': str}.
    """
    hunks = parse_hunks(patch_text)
    if not hunks:
        return {'error': 'No valid hunks found in patch. Make sure it contains @@ hunk headers. For simple edits, consider using str_replace instead.'}

    # Detect CRLF and work with LF-normalized content internally.
    crlf = '\r\n' in raw
    content = raw.replace('\r\n', '\n')

    # Split into lines WITHOUT endings, tracking whether file ends with \n.
    if content.endswith('\n'):
        lines = content[:-1].split('\n')
        trailing_newline = True
    elif content:
        lines = content.split('\n')
        trailing_newline = False
    else:
        lines = []
        trailing_newline = False

    offset = 0  # accumulated offset from previously applied hunks

    for hunk in hunks:
        hunk_lines = hunk['lines']

        # ── Insertion-only hunk ──
        if hunk['old_count'] == 0:
            insert_pos = hunk['new_start'] - 1 + offset
            insert_pos = max(0, min(insert_pos, len(lines)))
            new_lines = [txt for op, txt, _ in hunk_lines if op == '+']
            lines = lines[:insert_pos] + new_lines + lines[insert_pos:]
            offset += len(new_lines)
            if new_lines:
                trailing_newline = True
            continue

        # ── Context hunk: find matching position ──
        stated_pos = hunk['old_start'] - 1 + offset
        pos, match_hint = _find_hunk_pos(lines, hunk_lines, stated_pos, fuzzy=True)

        if pos == -1:
            anchor = _find_first_anchor(lines, hunk_lines)
            hint = f' (Hint: anchor found at line {anchor + 1})' if anchor >= 0 else ''
            read_offset = max(1, hunk['old_start'] - 20)
            read_hint = f' Use read_file with offset={read_offset} to view content around line {hunk["old_start"]}.'

            for op, txt, _ in hunk_lines:
                if op in (' ', '-') and txt.strip():
                    for line in lines:
                        if (line.rstrip() == txt.strip().rstrip() and
                                line.rstrip() != txt.rstrip()):
                            return {
                                'error': (
                                    f'Context not found at line {hunk["old_start"]} — '
                                    f'possible indentation/tabs/spaces mismatch{hint}. '
                                    'Action: call read_file() to get the current file content, '
                                    f'then reconstruct your patch from scratch.{read_hint}'
                                )
                            }
                    break

            return {
                'error': (
                    f'Context not found for hunk at line {hunk["old_start"]} '
                    f'(searched entire file{hint}). '
                    'Action: call read_file() to get the current file content, '
                    f'then reconstruct your patch from scratch.{read_hint}'
                )
            }

        # ── Apply the hunk ──
        needs_unescape = match_hint == 'unescape'
        result_lines = []
        file_idx = pos
        for op, txt, _ in hunk_lines:
            if op == ' ':
                result_lines.append(lines[file_idx])
                file_idx += 1
            elif op == '-':
                file_idx += 1
            elif op == '+':
                result_lines.append(_unescape_llm(txt) if needs_unescape else txt)

        consumed = sum(1 for op, _, _ in hunk_lines if op in (' ', '-'))
        produced = sum(1 for op, _, _ in hunk_lines if op in (' ', '+'))
        lines = lines[:pos] + result_lines + lines[pos + consumed:]
        offset += produced - consumed

    # Reconstruct file content.
    result = '\n'.join(lines)
    if trailing_newline and lines:
        result += '\n'
    if crlf:
        result = result.replace('\n', '\r\n')

    return {'result': 'success', 'content': result, 'hunks_applied': len(hunks)}


def apply_hunks(file_path: str, patch_text: str) -> dict:
    """
    Pure-Python patch application. Used as fallback when the system `patch`
    binary is unavailable.

    Handles:
    - Insertion-only hunks (@@ -N,0 +N,M @@) — inserts at stated position
      without requiring any surrounding context.
    - Context hunks — matched with ±50-line drift tolerance, trailing-whitespace-fuzzy.
    - CRLF line endings — detected and preserved.
    - Files without trailing newline.
    """
    hunks = parse_hunks(patch_text)
    if not hunks:
        return {'error': 'No valid hunks found in patch. Make sure it contains @@ hunk headers. For simple edits, consider using str_replace instead.'}

    creating_new = all(h['old_start'] == 0 and h['old_count'] == 0 for h in hunks)

    if not os.path.exists(file_path):
        if not creating_new:
            return {'error': f'File not found: {file_path}'}
        parent = os.path.dirname(os.path.abspath(file_path))
        os.makedirs(parent, exist_ok=True)
        open(file_path, 'w').close()

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace', newline='') as f:
            raw = f.read()
    except OSError as e:
        return {'error': str(e)}

    # Detect CRLF and work with LF-normalized content internally.
    crlf = '\r\n' in raw
    content = raw.replace('\r\n', '\n')

    # Split into lines WITHOUT endings, tracking whether file ends with \n.
    if content.endswith('\n'):
        lines = content[:-1].split('\n')
        trailing_newline = True
    elif content:
        lines = content.split('\n')
        trailing_newline = False
    else:
        lines = []
        trailing_newline = False

    offset = 0  # accumulated offset from previously applied hunks

    for hunk in hunks:
        hunk_lines = hunk['lines']

        # ── Insertion-only hunk ────────────────────────────────────────────
        if hunk['old_count'] == 0:
            insert_pos = hunk['new_start'] - 1 + offset
            insert_pos = max(0, min(insert_pos, len(lines)))
            new_lines = [txt for op, txt, _ in hunk_lines if op == '+']
            lines = lines[:insert_pos] + new_lines + lines[insert_pos:]
            offset += len(new_lines)
            if new_lines:
                trailing_newline = True
            continue

        # ── Context hunk: find matching position ──────────────────────────
        stated_pos = hunk['old_start'] - 1 + offset
        pos, match_hint = _find_hunk_pos(lines, hunk_lines, stated_pos, fuzzy=True)

        if pos == -1:
            # Build a helpful error message.
            anchor = _find_first_anchor(lines, hunk_lines)
            hint = f' (Hint: anchor found at line {anchor + 1})' if anchor >= 0 else ''
            read_offset = max(1, hunk['old_start'] - 20)
            read_hint = f' Use read_file with offset={read_offset} to view content around line {hunk["old_start"]}.'

            # Detect indentation mismatch specifically.
            for op, txt, _ in hunk_lines:
                if op in (' ', '-') and txt.strip():
                    for line in lines:
                        if (line.rstrip() == txt.strip().rstrip() and
                                line.rstrip() != txt.rstrip()):
                            return {
                                'error': (
                                    f'Context not found at line {hunk["old_start"]} — '
                                    f'possible indentation/tabs/spaces mismatch{hint}. '
                                    'Action: call read_file() to get the current file content, '
                                    f'then reconstruct your patch from scratch.{read_hint}'
                                )
                            }
                    break

            return {
                'error': (
                    f'Context not found for hunk at line {hunk["old_start"]} '
                    f'(searched entire file{hint}). '
                    'Action: call read_file() to get the current file content, '
                    f'then reconstruct your patch from scratch.{read_hint}'
                )
            }

        # ── Apply the hunk ─────────────────────────────────────────────────
        needs_unescape = match_hint == 'unescape'
        result_lines = []
        file_idx = pos
        for op, txt, _ in hunk_lines:
            if op == ' ':
                result_lines.append(lines[file_idx])
                file_idx += 1
            elif op == '-':
                file_idx += 1
            elif op == '+':
                result_lines.append(_unescape_llm(txt) if needs_unescape else txt)

        consumed = sum(1 for op, _, _ in hunk_lines if op in (' ', '-'))
        produced = sum(1 for op, _, _ in hunk_lines if op in (' ', '+'))
        lines = lines[:pos] + result_lines + lines[pos + consumed:]
        offset += produced - consumed

    # Reconstruct file content.
    result = '\n'.join(lines)
    if trailing_newline and lines:
        result += '\n'
    if crlf:
        result = result.replace('\n', '\r\n')

    try:
        with open(file_path, 'w', encoding='utf-8', newline='') as f:
            f.write(result)
    except OSError as e:
        return {'error': str(e)}

    return {'result': 'success', 'hunks_applied': len(hunks)}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_patch(file_path: str, patch_text: str) -> dict:
    """Apply a unified diff patch to a file using the pure-Python implementation."""
    try:
        hunks = parse_hunks(patch_text)
    except Exception as e:
        return {'error': f'Failed to parse patch: {e}'}

    if not hunks:
        return {'error': 'No valid hunks found in patch. Make sure it contains @@ hunk headers. For simple edits, consider using str_replace instead.'}

    return apply_hunks(file_path, patch_text)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

def execute(agent, args: dict) -> dict:
    file_path = args.get('file_path')
    patch_text = args.get('patch')

    if not file_path:
        return {'error': "Missing required argument: 'file_path'"}
    if patch_text is None:
        return {'error': "Missing required argument: 'patch'"}
    if not isinstance(patch_text, str):
        return {'error': "'patch' must be a string containing unified diff content"}

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
                    "description": "Patching sensitive system paths may compromise system integrity.",
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
                    "description": "Patching environment files may expose or corrupt secrets, API keys, or passwords.",
                    "file_path": file_path,
                },
            }

    # Normalise smart quotes in patch content before applying
    from backend.normalizer import normalize_code_quotes
    patch_text = normalize_code_quotes(patch_text)

    # /_self/ path: always route to the agent's local directory on the evonic server.
    from backend.tools._workspace import is_self_path, resolve_self_path
    agent_id = (agent or {}).get('id')
    if agent_id and is_self_path(file_path):
        local_path = resolve_self_path(agent_id, file_path)
        if not local_path:
            return {'error': "Access denied — path escapes agent directory."}
        return apply_patch(local_path, patch_text)

    # /_portal/ path: route through a virtual path mapping to local/SSH/evonet.
    from backend.tools._portal import is_portal_path, resolve_portal_path
    if agent_id and is_portal_path(file_path):
        backend, real_path = resolve_portal_path(agent_id, file_path)
        if backend is None:
            return {'error': real_path}  # error message

        # Parse hunks to check if this is creating a new file
        creating_new = False
        try:
            hunks = parse_hunks(patch_text)
            creating_new = all(h['old_start'] == 0 and h['old_count'] == 0 for h in hunks)
        except Exception:
            pass

        if not backend.file_exists(real_path):
            if not creating_new:
                return {'error': f'File not found: {file_path}'}
            parent = os.path.dirname(real_path)
            if parent:
                backend.make_dirs(parent)

        if creating_new and not backend.file_exists(real_path):
            backend.write_file(real_path, '')

        read_result = backend.read_file(real_path)
        if 'error' in read_result:
            return {'error': read_result['error']}

        result = _apply_hunks_to_content(read_result['content'], patch_text)
        if 'error' in result:
            return result

        wr = backend.write_file(real_path, result['content'])
        if 'error' in wr:
            return {'error': wr['error']}

        return {'result': 'success', 'hunks_applied': result.get('hunks_applied', 0)}

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
        creating_new = False
        try:
            hunks = parse_hunks(patch_text)
            creating_new = all(h['old_start'] == 0 and h['old_count'] == 0 for h in hunks)
        except Exception:
            pass

        if not backend.file_exists(target_path):
            if not creating_new:
                return {'error': f'File not found: {file_path}'}
            parent = os.path.dirname(target_path)
            if parent:
                backend.make_dirs(parent)

        if creating_new and not backend.file_exists(target_path):
            backend.write_file(target_path, '')

        read_result = backend.read_file(target_path)
        if 'error' in read_result:
            return {'error': read_result['error']}

        result = _apply_hunks_to_content(read_result['content'], patch_text)
        if 'error' in result:
            return result

        wr = backend.write_file(target_path, result['content'])
        if 'error' in wr:
            return {'error': wr['error']}

        return {'result': 'success', 'hunks_applied': result.get('hunks_applied', 0)}

    # No sandbox — direct host filesystem access (original behavior)
    workspace_root = None
    if file_path and (file_path.startswith('/workspace') or not os.path.isabs(file_path)):
        agent_workspace = (agent or {}).get('workspace')
        if file_path.startswith('/workspace') or agent_workspace:
            from config import SANDBOX_WORKSPACE as _ws
            fallback = _ws
            resolved = resolve_workspace_path(agent, file_path, fallback)
            if resolved != file_path:
                workspace_root = os.path.abspath(agent_workspace or fallback)
                file_path = resolved

    result = apply_patch(file_path, patch_text)

    # Replace absolute host paths in error messages with /workspace-relative paths
    # so agents running inside a container see paths they understand.
    if workspace_root and 'error' in result:
        result['error'] = result['error'].replace(workspace_root, '/workspace')

    return result


# ---------------------------------------------------------------------------
# Self-tests (run with: python backend/tools/patch.py)
# ---------------------------------------------------------------------------

def test_execute():
    import tempfile

    def make_file(content):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        f.write(content)
        f.close()
        return f.name

    def read_file(path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    passed = 0

    print('Test 1: Replace a line')
    tmp = make_file('line one\nline two\nline three\n')
    r = apply_patch(tmp, '@@ -1,3 +1,3 @@\n line one\n-line two\n+line TWO\n line three\n')
    assert r == {'result': 'success', 'hunks_applied': 1}, r
    assert read_file(tmp) == 'line one\nline TWO\nline three\n'
    passed += 1

    print('Test 2: Insert lines')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('alpha\nbeta\ngamma\n')
    r = apply_patch(tmp, '@@ -1,2 +1,4 @@\n alpha\n+inserted1\n+inserted2\n beta\n')
    assert r['result'] == 'success', r
    assert read_file(tmp) == 'alpha\ninserted1\ninserted2\nbeta\ngamma\n'
    passed += 1

    print('Test 3: Insertion-only hunk (no context at all)')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('line1\nline2\nline3\n')
    r = apply_hunks(tmp, '@@ -2,0 +2,2 @@\n+new_a\n+new_b\n')
    assert r['result'] == 'success', r
    assert read_file(tmp) == 'line1\nnew_a\nnew_b\nline2\nline3\n'
    passed += 1

    print('Test 4: Create new file')
    new_path = tmp + '.new'
    if os.path.exists(new_path):
        os.unlink(new_path)
    r = apply_patch(new_path, '@@ -0,0 +1,3 @@\n+first line\n+second line\n+third line\n')
    assert r['result'] == 'success', r
    assert read_file(new_path) == 'first line\nsecond line\nthird line\n'
    os.unlink(new_path)
    passed += 1

    print('Test 5: Multiple hunks')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('a\nb\nc\nd\ne\nf\n')
    r = apply_patch(tmp, '@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -5,2 +5,2 @@\n e\n-f\n+F\n')
    assert r['result'] == 'success', r
    assert read_file(tmp) == 'a\nB\nc\nd\ne\nF\n'
    passed += 1

    print('Test 6: Context mismatch → error (Python fallback)')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('line1\nline2\nline3\n')
    r = apply_hunks(tmp, '@@ -1,2 +1,2 @@\n WRONG_CONTEXT\n-line2\n+LINE2\n')
    assert 'error' in r, r
    passed += 1

    print('Test 7: Git-style headers skipped')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('foo\nbar\n')
    patch = 'diff --git a/f b/f\nindex a..b 100644\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n foo\n-bar\n+BAR\n'
    r = apply_patch(tmp, patch)
    assert r['result'] == 'success', r
    assert read_file(tmp) == 'foo\nBAR\n'
    passed += 1

    print('Test 8: CRLF preserved (Python fallback)')
    p2 = tempfile.mktemp(suffix='.txt')
    with open(p2, 'w', encoding='utf-8', newline='') as f:
        f.write('line1\r\nline2\r\nline3\r\n')
    r = apply_hunks(p2, '@@ -2,1 +2,1 @@\n-line2\n+LINE2\n')
    assert r['result'] == 'success', r
    raw = open(p2, 'rb').read()
    assert b'\r\n' in raw, 'CRLF should be preserved'
    os.unlink(p2)
    passed += 1

    print('Test 9: File not found → error')
    r = apply_patch('/nonexistent/path/file.txt', '@@ -1,1 +1,1 @@\n-x\n+y\n')
    assert 'error' in r, r
    passed += 1

    print('Test 10: No hunks → error')
    r = apply_patch(tmp, 'not a patch')
    assert 'error' in r, r
    passed += 1

    print('Test 11: Implicit hunk count=1')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('only line\n')
    r = apply_patch(tmp, '@@ -1 +1 @@\n-only line\n+ONLY LINE\n')
    assert r['result'] == 'success', r
    assert read_file(tmp) == 'ONLY LINE\n'
    passed += 1

    print('Test 12: Python fallback drift tolerance (±50 lines)')
    lines = [f'filler_{i}\n' for i in range(44)]
    lines += ['target line\n', 'after target\n']
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    r = apply_hunks(tmp, '@@ -1,2 +1,2 @@\n target line\n-after target\n+REPLACED\n')
    assert r['result'] == 'success', r
    assert 'REPLACED' in read_file(tmp)
    passed += 1

    print('Test 13: LLM double-escaped quotes in context')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('def foo():\n    """A docstring."""\n    x = 1\n    return x\n')
    r = apply_hunks(tmp, '@@ -1,4 +1,4 @@\n def foo():\n     \\"\\"\\"A docstring.\\"\\"\\"\n-    x = 1\n+    x = 2\n     return x\n')
    assert r['result'] == 'success', r
    assert 'x = 2' in read_file(tmp)
    assert '"""A docstring."""' in read_file(tmp)  # docstring preserved from file
    passed += 1

    print('Test 14: LLM double-escaped quotes in added lines')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('def bar():\n    """Old doc."""\n    pass\n')
    r = apply_hunks(tmp, '@@ -1,3 +1,3 @@\n def bar():\n-    \\"\\"\\"Old doc.\\"\\"\\"\n+    \\"\\"\\"New doc.\\"\\"\\"\n     pass\n')
    assert r['result'] == 'success', r
    assert '"""New doc."""' in read_file(tmp)  # unescaped in output
    passed += 1

    print('Test 15: JSON \\uXXXX in file vs decoded char in patch')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('{\n  "label": "Model \\u2192 Mapping",\n  "value": 1\n}\n')
    r = apply_hunks(tmp, '@@ -1,4 +1,4 @@\n {\n   "label": "Model \u2192 Mapping",\n-  "value": 1\n+  "value": 2\n }\n')
    assert r['result'] == 'success', r
    content = read_file(tmp)
    assert '"value": 2' in content
    assert '\\u2192' in content  # file's original escape preserved
    passed += 1

    os.unlink(tmp)
    print(f'\nAll {passed} tests passed!')


if __name__ == '__main__':
    test_execute()
