"""
Grep — regex search across files in a directory using ripgrep.

Returns matching lines grouped by file with line numbers.
"""
import os
import json
import shlex
from ._utils import _prepare_path, _is_kb_vault_path

_MAX_MATCHES = 500
# Cap total output size (~50KB) to prevent context overflow when the
# explorer sub-agent returns a tool_trace to the delegating agent.
# A single Grep result with long lines can produce 1M+ chars through
# 500 matches x 2KB lines.  50KB keeps ~125 typical 400-char lines
# which is enough for the explorer to reason about.
_MAX_OUTPUT_CHARS = 50_000


def execute(agent: dict, args: dict) -> dict:
    pattern = args.get('pattern', '')
    if not pattern:
        return {'error': 'pattern is required'}

    search_path = args.get('path', '.')
    include = args.get('include', '')

    try:
        backend, search_path, info = _prepare_path(agent, search_path)
    except PermissionError as exc:
        return {'error': str(exc)}
    except RuntimeError as exc:
        return {'error': f'cannot inspect path: {exc}'}
    if not info['exists']:
        return {'error': f'path not found: {search_path}'}

    cmd = ['rg', '--json', '--no-heading', '--max-count', str(_MAX_MATCHES)]

    # An agent's KB vault lives under the gitignored agents/ tree; ripgrep honors
    # .gitignore by default and would skip every doc (0 matches). Search it in
    # full. Normal code workspaces keep respecting ignore files.
    if _is_kb_vault_path(search_path):
        cmd.append('--no-ignore')

    if include:
        cmd.extend(['--glob', include])

    cmd.append(pattern)
    cmd.append(search_path)

    # Use `command` to bypass any shell function or alias named `rg`.  Execution
    # backends may initialize shell helpers, and a helper that appends arguments
    # can widen this search beyond the confined workspace.
    result = backend.run_bash('command ' + ' '.join(shlex.quote(part) for part in cmd), 30, {})
    if result.get('error'):
        message = result['error']
        if 'timed out' in message.lower():
            return {'error': 'search timed out after 30 seconds'}
        return {'error': message}
    if result.get('exit_code') == 127:
        return {'error': 'ripgrep (rg) is not installed'}

    # Parse ripgrep JSON output
    matches_by_file: dict = {}
    total_matches = 0
    truncated = False

    for line in result.get('stdout', '').strip().split('\n'):
        if not line:
            continue
        try:
            entry = json.loads(line)
            typ = entry.get('type', '')

            if typ == 'match':
                match_data = entry.get('data', {})
                file_abs = match_data.get('path', {}).get('text', '')
                line_num = match_data.get('line_number', 0)
                line_text = match_data.get('lines', {}).get('text', '').rstrip('\n')

                if total_matches >= _MAX_MATCHES:
                    truncated = True
                    break

                if file_abs not in matches_by_file:
                    matches_by_file[file_abs] = []

                matches_by_file[file_abs].append({
                    'line': line_num,
                    'content': line_text,
                })
                total_matches += 1

        except (json.JSONDecodeError, KeyError):
            continue

    # Convert to sorted output
    matches = []
    for file_abs in sorted(matches_by_file.keys()):
        if info['is_dir']:
            rel = os.path.relpath(file_abs, search_path)
        else:
            rel = file_abs
        matches.append({'file': rel, 'matches': matches_by_file[file_abs]})

    result = {'matches': matches, 'total_matches': total_matches}
    if truncated:
        result['truncated'] = True
        result['note'] = f'Results truncated at {_MAX_MATCHES} matches. Narrow your search.'

    # --- Output size cap ---
    # Even with _MAX_MATCHES=500, long line content can produce 1M+ chars
    # of JSON.  Truncate progressively (drop ~20 % of matches per pass)
    # until the serialised output fits within _MAX_OUTPUT_CHARS.
    _result_str = json.dumps(result)
    if len(_result_str) > _MAX_OUTPUT_CHARS:
        _dropped = 0
        while len(_result_str) > _MAX_OUTPUT_CHARS and matches:
            _n_to_drop = max(1, len(matches) // 5)
            for _ in range(_n_to_drop):
                if not matches:
                    break
                matches.pop()
                _dropped += 1
            result['matches'] = matches
            result['total_matches'] = total_matches
            _result_str = json.dumps(result)
        if _dropped > 0:
            result['truncated'] = True
            taken = total_matches - _dropped
            result['note'] = (
                f'Output truncated to {_MAX_OUTPUT_CHARS} chars — '
                f'showing {taken}/{total_matches} matches across '
                f'{len(matches)} file(s). Narrow your search pattern or scope.'
            )

    return result
