"""
Tool: list_artifacts — list and search an agent's artifact files.

Works directly on the filesystem — reads shared/agents/<agent_id>/artifacts/
directory. Supports filename filter, content grep (text/document only),
category type filter, sorting, and result limiting.
"""

import os

from backend.tools._workspace import effective_agent_id
from backend.tools.lib.simulation_scope import shared_agents_dir

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEXT_DOCUMENT_EXTENSIONS = frozenset({
    '.md', '.pdf',
    '.txt', '.csv', '.json', '.yaml', '.yml', '.xml', '.log',
    '.py', '.c', '.rs', '.js', '.ts', '.jsx', '.tsx', '.cpp', '.cc', '.cxx',
    '.h', '.hpp', '.java', '.go', '.rb', '.php', '.cs', '.swift', '.kt',
    '.scala', '.r', '.m', '.sh', '.bash', '.zsh', '.ps1', '.sql',
    '.html', '.css', '.scss', '.less', '.toml', '.ini', '.cfg', '.conf',
    '.env', '.lock', '.diff', '.patch', '.Makefile', '.Dockerfile',
    '.vue', '.svelte', '.lua', '.pl', '.pm', '.gradle', '.groovy',
})


def _artifacts_dir(agent_id, artifacts_root=None):
    root = artifacts_root or os.path.join(BASE_DIR, 'shared', 'agents')
    return os.path.join(root, agent_id, 'artifacts')


def _get_file_category(fname):
    ext = os.path.splitext(fname)[1].lower()
    if ext in ('.md', '.pdf'):
        return 'document'
    if ext in TEXT_DOCUMENT_EXTENSIONS:
        return 'text'
    if ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico'):
        return 'image'
    if ext in ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma'):
        return 'sound'
    if ext in ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.m4v'):
        return 'video'
    return 'data'


def _is_text_or_document(fname):
    category = _get_file_category(fname)
    return category in ('text', 'document')


def _grep_file(filepath, needle):
    """Return True if needle (lowercase) found in file content."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            # Read in chunks to handle large files
            while True:
                chunk = f.read(8192)
                if not chunk:
                    return False
                if needle in chunk.lower():
                    return True
    except Exception:
        return False


def execute(agent, args):
    agent_id = effective_agent_id(agent)
    if not agent_id:
        return {'error': 'Agent ID not found in context'}

    artifacts_dir = _artifacts_dir(agent_id, shared_agents_dir(agent))
    if not os.path.isdir(artifacts_dir):
        return {'files': [], 'total': 0}

    filter_query = (args.get('filter', '') or '').strip().lower()
    grep_query = (args.get('grep', '') or '').strip().lower()
    type_filter = (args.get('type', '') or '').strip().lower()
    sort_param = (args.get('sort', '') or '').strip().lower() or 'newest'
    limit = args.get('limit', 50)

    # Validate and clamp limit
    if not isinstance(limit, int) or limit < 1:
        limit = 50
    if limit > 200:
        limit = 200

    # Validate sort
    if sort_param not in ('newest', 'updated', 'alpha', 'alpha_desc'):
        sort_param = 'newest'

    # Validate type
    valid_types = ('all', 'document', 'text', 'image', 'sound', 'video', 'data')
    if type_filter and type_filter not in valid_types:
        type_filter = ''

    files = []
    for fname in sorted(os.listdir(artifacts_dir)):
        fpath = os.path.join(artifacts_dir, fname)
        if not os.path.isfile(fpath):
            continue

        # --- filter: filename match ---
        if filter_query and filter_query not in fname.lower():
            continue

        # --- type filter ---
        cat = _get_file_category(fname)
        if type_filter and type_filter != 'all' and cat != type_filter:
            continue

        # --- grep: content search (text/document only) ---
        if grep_query:
            if _is_text_or_document(fname):
                if not _grep_file(fpath, grep_query):
                    continue
            else:
                # Non-text files cannot be grepped — skip them when grep is active
                continue

        stat = os.stat(fpath)
        files.append({
            'filename': fname,
            'size': stat.st_size,
            'modified': stat.st_mtime,
            'category': cat,
        })

    # --- sort ---
    if sort_param == 'updated':
        files.sort(key=lambda f: f['modified'], reverse=True)
    elif sort_param == 'alpha':
        files.sort(key=lambda f: f['filename'].lower())
    elif sort_param == 'alpha_desc':
        files.sort(key=lambda f: f['filename'].lower(), reverse=True)
    else:  # newest
        files.sort(key=lambda f: f['modified'], reverse=True)

    total = len(files)
    files = files[:limit]

    response = {
        'files': files,
        'total': total,
    }
    if filter_query:
        response['filter'] = filter_query
    if grep_query:
        response['grep'] = grep_query

    return response
