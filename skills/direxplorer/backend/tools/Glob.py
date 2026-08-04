"""
Glob — find files matching a glob pattern.

Returns a list of matching file paths. Supports ** for recursive matching.

Results are capped (file count AND serialized JSON size) so the output
always fits inside the backend's 64KB stdout cap. Without the cap, a
recursive glob over a large workspace matches tens of thousands of files,
the backend truncates the stdout mid-JSON, and parsing fails with a
misleading "backend returned invalid path metadata" error.
"""
import os
from ._utils import _prepare_path, _run_python_json

# Keep output well under the backend's 64KB stdout cap (exec_backend.truncate).
# 48KB of JSON leaves headroom; the generated code also trims until the
# serialized payload fits, so long/escaped paths cannot blow the budget.
_MAX_FILES = 1000
_MAX_JSON_BYTES = 48 * 1024


def execute(agent: dict, args: dict) -> dict:
    pattern = args.get('pattern', '')
    if not pattern:
        return {'error': 'pattern is required'}

    requested = args.get('path', '.')
    try:
        backend, base_path, info = _prepare_path(agent, requested, want_dir=True)
    except PermissionError as exc:
        return {'error': str(exc)}
    except RuntimeError as exc:
        return {'error': f'cannot inspect path: {exc}'}
    if not info['exists']:
        return {'error': f'path not found: {base_path}'}
    if not info['is_dir']:
        return {'error': f'path is not a directory: {base_path}'}

    code = f'''import glob, json, os
base, pattern = {base_path!r}, {pattern!r}
skip = {{'.git', 'node_modules', '__pycache__', 'venv', '.venv', 'vendor', 'target', 'build', 'dist'}}
MAX_FILES = {_MAX_FILES}
MAX_JSON_BYTES = {_MAX_JSON_BYTES}
files = []
total = 0
for path in sorted(glob.glob(os.path.join(base, pattern), recursive=True)):
    rel = os.path.relpath(path, base)
    if not os.path.isfile(path) or any(part in skip for part in rel.split(os.sep)):
        continue
    total += 1
    if len(files) < MAX_FILES:
        files.append(rel)
payload = dict(files=files, count=total, truncated=total > len(files))
out = json.dumps(payload)
while files and len(out.encode('utf-8')) > MAX_JSON_BYTES:
    files.pop()
    payload['truncated'] = True
    out = json.dumps(payload)
print(out)'''
    try:
        results = _run_python_json(backend, code)
    except RuntimeError as exc:
        return {'error': f'glob failed: {exc}'}

    if isinstance(results, dict):
        files = results.get('files', [])
        count = results.get('count', len(files))
        truncated = results.get('truncated', False)
    else:  # defensive: tolerate a legacy plain-list payload
        files = results or []
        count = len(files)
        truncated = False
    return {
        'files': files,
        'count': count,
        'base': os.path.abspath(base_path),
        'truncated': truncated,
    }
