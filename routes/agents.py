"""
Agent Management Blueprint — CRUD for agents, KB files, tools, and channels.
"""

import os
import re
import json
import uuid
import queue
from typing import Dict, Any, List, Optional
from flask import Blueprint, render_template, jsonify, request, Response, stream_with_context
from models.db import db
from models.chatlog import chatlog_manager, _DISPLAY_TYPES
from backend.tools import tool_registry

agents_bp = Blueprint('agents', __name__)

_SENSITIVE_AGENT_KEYS = frozenset({'workspace'})

_NOTES_MD_TEMPLATE = """# Notes.md -- User Preferences & Instructions

This file stores your user's personal preferences, tastes, language
preferences, and communication style instructions.

## What to store here

- User's preferred language (e.g. "User prefers Bahasa Indonesia")
- Communication style preferences (e.g. "User likes concise answers",
  "User dislikes emoji")
- Personal instructions (e.g. "Call the user 'Pak'")
- Tastes and preferences (e.g. "User prefers bullet points over paragraphs")

## What NOT to store here (use `remember` instead)

- Factual/memorization data: addresses, phone numbers, email, birthday
- Secret/sensitive data: passwords, tokens, PINs, secret codes, bank accounts

## Usage

- Read this file: read("notes.md")
- Update via write_file with path /_self/kb/notes.md
- Update immediately when the user gives a new preference
- Prioritize notes.md over `remember` for non-factual preference information
"""


def _sanitize_agent(agent: Dict[str, Any]) -> Dict[str, Any]:
    """Strip sensitive fields (workspace) from an agent dict before API response."""
    for key in _SENSITIVE_AGENT_KEYS:
        agent.pop(key, None)
    return agent


def _sanitize_agents(agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for a in agents:
        _sanitize_agent(a)
    return agents


def _apply_sandbox_workplace_policy(agent_data: dict, workplace_id: Optional[str]) -> None:
    """Docker sandbox is only supported on local workplaces."""
    if not workplace_id:
        return
    workplace = db.get_workplace(workplace_id)
    if workplace and workplace.get('type') in ('remote', 'cloud'):
        agent_data['sandbox_enabled'] = 0

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(BASE_DIR, 'agents')
WORKSPACE_DIR = os.path.join(BASE_DIR, 'shared', 'agents')

SLUG_RE = re.compile(r'^[a-z0-9_]+$')
USER_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\.@]{1,128}$')


def _validate_user_id(user_id: str) -> str:
    """Validate and normalize a user_id parameter.

    Rejects empty/whitespace-only, excessively long, or unsafe user_id values.
    Returns the normalized string on success.
    """
    user_id = (user_id or '').strip()
    if not user_id:
        raise ValueError('user_id must not be empty')
    if len(user_id) > 128:
        raise ValueError('user_id must not exceed 128 characters')
    if not USER_ID_RE.match(user_id):
        raise ValueError(
            'user_id contains invalid characters; '
            'allowed: alphanumeric, underscore, hyphen, dot, @'
        )
    return user_id


def _kb_dir(agent_id: str) -> str:
    return os.path.join(AGENTS_DIR, agent_id, 'kb')


def _ensure_kb_dir(agent_id: str) -> str:
    d = _kb_dir(agent_id)
    os.makedirs(d, exist_ok=True)
    return d


def _system_prompt_path(agent_id: str) -> str:
    return os.path.join(AGENTS_DIR, agent_id, 'SYSTEM.md')


def _read_system_prompt(agent_id: str, fallback: str = '') -> str:
    path = _system_prompt_path(agent_id)
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            pass
    return fallback


def _write_system_prompt(agent_id: str, content: str):
    path = _system_prompt_path(agent_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _migrate_system_prompts():
    """One-time migration: write DB system_prompt to SYSTEM.md for agents that lack the file."""
    try:
        agents = db.get_agents()
        for agent in agents:
            agent_id = agent.get('id', '')
            if not agent_id:
                continue
            path = _system_prompt_path(agent_id)
            if not os.path.isfile(path):
                sp = agent.get('system_prompt', '') or ''
                _write_system_prompt(agent_id, sp)
    except Exception as e:
        print(f"[agents] system_prompt migration error (non-fatal): {e}")


_migrate_system_prompts()


# ==================== Pages ====================

@agents_bp.route('/agents')
def agents_list():
    return render_template('agents.html')


@agents_bp.route('/agents/<agent_id>')
def agent_detail(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return "Agent not found", 404
    agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
    from backend.agent_runtime import DEFAULT_SUMMARIZE_PROMPT
    # Check if workspace directory exists and is valid
    workspace_invalid = False
    ws = agent.get('workspace', '').strip() if agent.get('workspace') else ''
    if not ws:
        workspace_invalid = True
    elif not os.path.isdir(ws):
        workspace_invalid = True
    return render_template('agent_detail.html', agent=agent,
                           DEFAULT_SUMMARIZE_PROMPT=DEFAULT_SUMMARIZE_PROMPT,
                           workspace_invalid=workspace_invalid,
                           workspace_path=ws if ws else '(not set)')


# ==================== Agent CRUD API ====================

@agents_bp.route('/api/agents', methods=['GET'])
def api_list_agents():
    agents = db.get_agents()
    return jsonify({'agents': _sanitize_agents(agents)})


@agents_bp.route('/api/agents/<agent_id>', methods=['GET'])
def api_get_agent(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
    agent['tools'] = db.get_agent_tools(agent_id)
    agent['channels'] = db.get_channels(agent_id)
    # Detect orphaned tools (skill uninstalled)
    known_ids = set()
    for td in tool_registry.get_all_tool_defs():
        known_ids.add(td.get('function', {}).get('name') or td.get('id', ''))
        if td.get('id'):
            known_ids.add(td['id'])
    agent['missing_tools'] = [t for t in agent['tools'] if t not in known_ids]
    return jsonify(_sanitize_agent(agent))


@agents_bp.route('/api/agents', methods=['POST'])
def api_create_agent():
    if not db.has_super_agent():
        return jsonify({'error': 'Super agent must be set up before creating other agents.', 'setup_required': True}), 400
    data = request.get_json()
    agent_id = data.get('id', '').strip().lower()
    if not agent_id or not SLUG_RE.match(agent_id):
        return jsonify({'error': 'Invalid ID. Use only lowercase alphanumeric characters and underscores (snake_case).'}), 400
    if db.get_agent(agent_id):
        return jsonify({'error': 'Agent ID already exists.'}), 400
    if len(data.get('name', '')) > 200:
        return jsonify({'error': 'Name too long (max 200 characters).'}), 400
    if len(data.get('description', '')) > 2000:
        return jsonify({'error': 'Description too long (max 2000 characters).'}), 400
    if len(data.get('system_prompt', '')) > 102400:
        return jsonify({'error': 'System prompt too long (max 100 KB).'}), 400
    _apply_sandbox_workplace_policy(data, data.get('workplace_id'))
    try:
        _ensure_kb_dir(agent_id)
        # Set default workspace for regular agents to shared/agents/[agent-id]
        if 'workspace' not in data or not data.get('workspace'):
            data['workspace'] = os.path.join(WORKSPACE_DIR, agent_id)
        db.create_agent(data)
        # Create workspace directory if it does not already exist
        os.makedirs(data['workspace'], exist_ok=True)
        _write_system_prompt(agent_id, data.get('system_prompt', ''))
        # Create artifacts directory
        _artifacts_dir(agent_id)
        # Inject artifacts instructions into SYSTEM.md if enabled
        artifacts_enabled = data.get('artifacts_enabled')
        if artifacts_enabled is None or artifacts_enabled:
            _ensure_artifacts_prompt(agent_id, True)
            db.add_agent_tool(agent_id, 'save_artifact')
        # Create notes.md template if it does not already exist
        _notes_md = os.path.join(_kb_dir(agent_id), 'notes.md')
        if not os.path.isfile(_notes_md):
            with open(_notes_md, 'w', encoding='utf-8') as _f:
                _f.write(_NOTES_MD_TEMPLATE)
        agent = db.get_agent(agent_id)
        agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
        return jsonify({'success': True, 'agent': _sanitize_agent(agent)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/agents/<agent_id>', methods=['PUT'])
def api_update_agent(agent_id):
    existing = db.get_agent(agent_id)
    if not existing:
        return jsonify({'error': 'Agent not found'}), 404
    data = request.get_json()
    # Super agent cannot be disabled
    if existing.get('is_super') and data.get('enabled') is False:
        return jsonify({'error': 'Super agent cannot be disabled.'}), 403
    target_workplace_id = data.get('workplace_id', existing.get('workplace_id'))
    _apply_sandbox_workplace_policy(data, target_workplace_id)
    if 'system_prompt' in data:
        _write_system_prompt(agent_id, data['system_prompt'])
    # Handle artifacts_enabled toggle: inject/remove SYSTEM.md instructions
    if 'artifacts_enabled' in data:
        old_artifacts = existing.get('artifacts_enabled', True) if existing.get('artifacts_enabled') is not None else True
        new_artifacts = bool(data['artifacts_enabled'])
        if new_artifacts != old_artifacts:
            _ensure_artifacts_prompt(agent_id, new_artifacts)
            if new_artifacts:
                db.add_agent_tool(agent_id, 'save_artifact')
            else:
                db.remove_agent_tool(agent_id, 'save_artifact')
    db.update_agent(agent_id, data)
    agent = db.get_agent(agent_id)
    agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
    return jsonify({'success': True, 'agent': _sanitize_agent(agent)})


@agents_bp.route('/api/agents/<agent_id>', methods=['DELETE'])
def api_delete_agent(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    if agent.get('is_super'):
        return jsonify({'error': 'Super agent cannot be deleted.'}), 403
    try:
        db.delete_agent(agent_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 403
    import shutil
    agent_dir = os.path.join(AGENTS_DIR, agent_id)
    if os.path.isdir(agent_dir):
        shutil.rmtree(agent_dir)
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/clone', methods=['POST'])
def api_clone_agent(agent_id):
    """Clone an existing agent: copy all settings, tools, skills, variables, and KB files."""
    source = db.get_agent(agent_id)
    if not source:
        return jsonify({'error': 'Agent not found'}), 404
    if source.get('is_super'):
        return jsonify({'error': 'Super agent cannot be cloned.'}), 403

    data = request.get_json() or {}
    new_id = data.get('id', '').strip().lower()
    new_name = data.get('name', '').strip()
    new_desc = data.get('description', '').strip()

    if not new_id or not SLUG_RE.match(new_id):
        return jsonify({
            'error': 'Invalid ID. Use only lowercase alphanumeric characters and underscores (snake_case).'
        }), 400
    if not new_name:
        new_name = f"{source.get('name', agent_id)} (Clone)"

    try:
        cloned_id = db.clone_agent(agent_id, new_id, new_name, new_desc)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Copy SYSTEM.md
    src_sp = _system_prompt_path(agent_id)
    if os.path.isfile(src_sp):
        _write_system_prompt(cloned_id, _read_system_prompt(agent_id))

    # Copy KB files
    src_kb = _kb_dir(agent_id)
    dst_kb = _ensure_kb_dir(cloned_id)
    if os.path.isdir(src_kb):
        for fname in os.listdir(src_kb):
            src_path = os.path.join(src_kb, fname)
            dst_path = os.path.join(dst_kb, fname)
            if os.path.isfile(src_path):
                import shutil
                shutil.copy2(src_path, dst_path)

    # Create workspace directory for the clone
    clone_ws = os.path.join(WORKSPACE_DIR, cloned_id)
    os.makedirs(clone_ws, exist_ok=True)

    agent = db.get_agent(cloned_id)
    agent['system_prompt'] = _read_system_prompt(cloned_id, fallback=agent.get('system_prompt', ''))
    return jsonify({'success': True, 'agent': _sanitize_agent(agent)})


# ==================== Agent Tools API ====================

@agents_bp.route('/api/agents/<agent_id>/tools', methods=['GET'])
def api_get_agent_tools(agent_id):
    tool_ids = db.get_agent_tools(agent_id)
    return jsonify({'tools': tool_ids})


@agents_bp.route('/api/agents/<agent_id>/tools', methods=['PUT'])
def api_set_agent_tools(agent_id):
    data = request.get_json()
    tool_ids = data.get('tools', [])
    db.set_agent_tools(agent_id, tool_ids)
    return jsonify({'success': True, 'tools': tool_ids})


# ==================== Agent Skills API ====================

@agents_bp.route('/api/agents/<agent_id>/skills', methods=['GET'])
def api_get_agent_skills(agent_id):
    skill_ids = db.get_agent_skills(agent_id)
    return jsonify({'skills': skill_ids})


@agents_bp.route('/api/agents/<agent_id>/skills', methods=['PUT'])
def api_set_agent_skills(agent_id):
    data = request.get_json()
    skill_ids = data.get('skills', [])
    db.set_agent_skills(agent_id, skill_ids)
    return jsonify({'success': True, 'skills': skill_ids})


# ==================== Agent Variables API ====================

@agents_bp.route('/api/agents/<agent_id>/variables', methods=['GET'])
def api_get_agent_variables(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    variables = db.get_agent_variables(agent_id)
    # Mask secret values in GET response
    for v in variables:
        if v.get('is_secret') and v.get('value'):
            v['value'] = '••••••••'
    return jsonify({'variables': variables})


@agents_bp.route('/api/agents/<agent_id>/variables', methods=['PUT'])
def api_set_agent_variables(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    data = request.get_json()
    variables = data.get('variables', [])
    # For secret fields, if the value is the mask placeholder, keep the existing value
    existing = {v['key']: v for v in db.get_agent_variables(agent_id)}
    for var in variables:
        if var.get('is_secret') and var.get('value') == '••••••••':
            old = existing.get(var['key'])
            if old:
                var['value'] = old['value']
    db.set_agent_variables_bulk(agent_id, variables)
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/variables/<key>', methods=['DELETE'])
def api_delete_agent_variable(agent_id, key):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    db.delete_agent_variable(agent_id, key)
    return jsonify({'success': True})


# ==================== Knowledge Base API ====================

@agents_bp.route('/api/agents/<agent_id>/kb', methods=['GET'])
def api_list_kb(agent_id):
    kb = _kb_dir(agent_id)
    if not os.path.isdir(kb):
        return jsonify({'files': []})
    files = []
    for fname in sorted(os.listdir(kb)):
        fpath = os.path.join(kb, fname)
        if os.path.isfile(fpath):
            stat = os.stat(fpath)
            files.append({
                'filename': fname,
                'size': stat.st_size,
                'modified': stat.st_mtime
            })
    return jsonify({'files': files})


@agents_bp.route('/api/agents/<agent_id>/kb/<filename>', methods=['GET'])
def api_get_kb_file(agent_id, filename):
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_kb_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    with open(fpath, 'r', encoding='utf-8') as f:
        content = f.read()
    return jsonify({'filename': filename, 'content': content})


@agents_bp.route('/api/agents/<agent_id>/kb', methods=['POST'])
def api_upload_kb(agent_id):
    kb = _ensure_kb_dir(agent_id)

    # Support both multipart file upload and JSON body
    if request.content_type and 'multipart' in request.content_type:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        f = request.files['file']
        fname = f.filename or 'untitled.md'
        if '/' in fname or '\\' in fname or '..' in fname:
            return jsonify({'error': 'Invalid filename'}), 400
        fpath = os.path.join(kb, fname)
        f.save(fpath)
        return jsonify({'success': True, 'filename': fname})
    else:
        data = request.get_json()
        fname = data.get('filename', '').strip()
        content = data.get('content', '')
        if not fname:
            return jsonify({'error': 'filename is required'}), 400
        if '/' in fname or '\\' in fname or '..' in fname:
            return jsonify({'error': 'Invalid filename'}), 400
        fpath = os.path.join(kb, fname)
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True, 'filename': fname})


@agents_bp.route('/api/agents/<agent_id>/kb/<filename>', methods=['PUT'])
def api_update_kb_file(agent_id, filename):
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_kb_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    data = request.get_json()
    content = data.get('content', '')
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({'success': True, 'filename': filename})


@agents_bp.route('/api/agents/<agent_id>/kb/<filename>', methods=['DELETE'])
def api_delete_kb_file(agent_id, filename):
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_kb_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    os.remove(fpath)
    return jsonify({'success': True})


def _artifacts_dir(agent_id: str) -> str:
    d = os.path.join(WORKSPACE_DIR, agent_id, 'artifacts')
    os.makedirs(d, exist_ok=True)
    return d


_ARTIFACT_PROMPT_TEMPLATE = """
## Artifacts Feature

You have an **Artifacts** feature that allows you to save files you produce during your work. Files are stored in your dedicated artifacts directory and are accessible via the web UI.

### Using save_artifact Tool

Use the **save_artifact** tool to save files:
- `filename`: the name of the file (e.g. 'report.md', 'analysis.txt', 'output.json')
- `content`: the text content of the file (or base64-encoded content for binary files)
- `mime_type`: optional MIME type hint
- `mode`: set to 'text' (default) for text files, or 'base64' for binary files (PDFs, images, etc.)

When to use this tool:
- After completing analysis or research, save the findings as a report
- After generating code, configuration, or any output, save it as an artifact
- After creating images, PDFs, or markdown documents
- Any time you produce a file that the user or other agents may want to reference later
- For binary files (PDFs, images), set `mode: "base64"` and provide base64-encoded content

### Alternative: Using write_file or bash/runpy

You can also save files directly to your artifacts directory using:
- `write_file` with path starting with `/workspace/shared/agents/<YOUR_AGENT_ID>/artifacts/<filename>`
- bash/runpy by writing files to the same directory path

This is particularly useful for binary files (PDFs, images) that you generate via Python scripts.

The files are stored in your dedicated artifacts directory and can be browsed and downloaded from the agent detail page in the Artifacts tab.
"""


def _ensure_artifacts_prompt(agent_id: str, enabled: bool):
    """Inject or remove the Artifacts instructions from the agent's SYSTEM.md."""
    path = _system_prompt_path(agent_id)
    prompt_text = _ARTIFACT_PROMPT_TEMPLATE.strip()

    if not os.path.isfile(path):
        return

    with open(path, 'r', encoding='utf-8') as f:
        sp = f.read()

    if enabled:
        # Inject if not already present
        if prompt_text not in sp:
            sp = sp.rstrip() + '\n\n' + prompt_text + '\n'
            _write_system_prompt(agent_id, sp)
    else:
        # Remove if present
        if prompt_text in sp:
            sp = sp.replace(prompt_text, '').strip()
            _write_system_prompt(agent_id, sp)


# ==================== Agent Artifacts API ====================


@agents_bp.route('/api/agents/<agent_id>/artifacts', methods=['GET'])
def api_list_artifacts(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    artifacts_dir = _artifacts_dir(agent_id)
    if not os.path.isdir(artifacts_dir):
        return jsonify({'files': []})
    
    sort_param = request.args.get('sort', 'newest')
    query = (request.args.get('q', '') or '').strip().lower()
    type_filter = (request.args.get('type', '') or '').strip().lower()
    
    # File type category detection
    def _get_file_category(fname):
        ext = os.path.splitext(fname)[1].lower()
        if ext in ('.md', '.pdf'):
            return 'document'
        if ext in ('.txt', '.csv', '.json', '.yaml', '.yml', '.xml', '.log',
                   '.py', '.c', '.rs', '.js', '.ts', '.jsx', '.tsx', '.cpp', '.cc', '.cxx',
                   '.h', '.hpp', '.java', '.go', '.rb', '.php', '.cs', '.swift', '.kt',
                   '.scala', '.r', '.m', '.sh', '.bash', '.zsh', '.ps1', '.sql',
                   '.html', '.css', '.scss', '.less', '.toml', '.ini', '.cfg', '.conf',
                   '.env', '.lock', '.diff', '.patch', '.Makefile', '.Dockerfile',
                   '.vue', '.svelte', '.lua', '.pl', '.pm', '.gradle', '.groovy'):
            return 'text'
        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico'):
            return 'image'
        if ext in ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma'):
            return 'sound'
        if ext in ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.m4v'):
            return 'video'
        return 'data'
    
    files = []
    for fname in sorted(os.listdir(artifacts_dir)):
        fpath = os.path.join(artifacts_dir, fname)
        if not os.path.isfile(fpath):
            continue
        
        # Apply search filter
        if query and query not in fname.lower():
            continue
        
        # Apply type filter
        cat = _get_file_category(fname)
        if type_filter and type_filter != 'all' and cat != type_filter:
            continue
        
        stat = os.stat(fpath)
        files.append({
            'filename': fname,
            'size': stat.st_size,
            'modified': stat.st_mtime,
            'category': cat,
        })
    
    # Sort
    if sort_param == 'updated':
        files.sort(key=lambda f: f['modified'], reverse=True)
    elif sort_param == 'alpha':
        files.sort(key=lambda f: f['filename'].lower())
    elif sort_param == 'alpha_desc':
        files.sort(key=lambda f: f['filename'].lower(), reverse=True)
    else:  # newest
        files.sort(key=lambda f: f['modified'], reverse=True)
    
    return jsonify({'files': files})


@agents_bp.route('/api/agents/<agent_id>/artifacts/<path:filename>', methods=['GET'])
def api_get_artifact(agent_id, filename):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_artifacts_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    from flask import send_file
    import mimetypes
    mime, _ = mimetypes.guess_type(filename)
    if mime is None:
        mime = 'application/octet-stream'
    return send_file(fpath, mimetype=mime, as_attachment=False)


@agents_bp.route('/api/agents/<agent_id>/artifacts/<path:filename>', methods=['DELETE'])
def api_delete_artifact(agent_id, filename):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_artifacts_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    os.remove(fpath)
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/artifacts', methods=['POST'])
def api_create_artifact(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    data = request.get_json()
    filename = data.get('filename', '').strip()
    content = data.get('content', '')
    if not filename:
        return jsonify({'error': 'filename is required'}), 400
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    artifacts_dir = _artifacts_dir(agent_id)
    fpath = os.path.join(artifacts_dir, filename)
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== Agent Avatar API ====================

AVATAR_DIR = os.path.join(BASE_DIR, 'shared', 'avatars')

def _avatar_dir(agent_id: str) -> str:
    d = os.path.join(AVATAR_DIR, agent_id)
    os.makedirs(d, exist_ok=True)
    return d


@agents_bp.route('/api/agents/<agent_id>/avatar', methods=['GET'])
def api_get_avatar(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    avatar_path = agent.get('avatar_path', '')
    if avatar_path and os.path.isfile(avatar_path):
        import mimetypes
        mime, _ = mimetypes.guess_type(avatar_path)
        if mime is None:
            mime = 'application/octet-stream'
        from flask import send_file
        return send_file(avatar_path, mimetype=mime)
    # Return default SVG avatar
    default_svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" fill="none">
  <rect width="40" height="40" rx="20" fill="#e0e7ff"/>
  <path d="M20 8a5 5 0 100 10 5 5 0 000-10zm-8 18.5a8 8 0 0116 0" fill="#4f46e5"/>
</svg>'''
    from flask import Response
    return Response(default_svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=3600'})


@agents_bp.route('/api/agents/<agent_id>/avatar', methods=['POST'])
def api_upload_avatar(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    # Validate image type
    allowed_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed_exts:
        return jsonify({'error': f'Invalid image type. Allowed: {", ".join(sorted(allowed_exts))}'}), 400
    avatar_dir = _avatar_dir(agent_id)
    # Remove old avatar file if exists
    old_path = agent.get('avatar_path', '')
    if old_path and os.path.isfile(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    # Save new avatar
    fname = f'avatar{ext}'
    fpath = os.path.join(avatar_dir, fname)
    f.save(fpath)
    db.update_agent(agent_id, {'avatar_path': fpath})
    return jsonify({'success': True, 'avatar_path': f'/api/agents/{agent_id}/avatar'})


@agents_bp.route('/api/agents/<agent_id>/avatar', methods=['DELETE'])
def api_delete_avatar(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    old_path = agent.get('avatar_path', '')
    if old_path and os.path.isfile(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    db.update_agent(agent_id, {'avatar_path': ''})
    return jsonify({'success': True})


# ==================== Channels API ====================

@agents_bp.route('/api/agents/<agent_id>/channels', methods=['GET'])
def api_list_channels(agent_id):
    from backend.channels.registry import channel_manager
    channels = db.get_channels(agent_id)
    primary_cid = db.get_primary_channel_id(agent_id)
    for ch in channels:
        ch['running'] = channel_manager.is_running(ch['id'])
        ch['is_primary'] = ch['id'] == primary_cid
    return jsonify({'channels': channels})


@agents_bp.route('/api/agents/<agent_id>/channels', methods=['POST'])
def api_create_channel(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    data = request.get_json()
    data['agent_id'] = agent_id
    if not data.get('type'):
        return jsonify({'error': 'Channel type is required'}), 400
    try:
        chan_id = db.create_channel(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    # Auto-start the channel after creation
    from backend.channels.registry import channel_manager
    try:
        channel_manager.start_channel(chan_id)
    except Exception as e:
        print(f"[ChannelManager] Auto-start failed for {chan_id}: {e}")
    channel = db.get_channel(chan_id)
    channel['running'] = channel_manager.is_running(chan_id)
    return jsonify({'success': True, 'channel': channel})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>', methods=['PUT'])
def api_update_channel(agent_id, channel_id):
    data = request.get_json()
    try:
        db.update_channel(channel_id, data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 409

    # Sync running state with enabled flag if it was changed
    if 'enabled' in data:
        from backend.channels.registry import channel_manager
        if data['enabled']:
            try:
                channel_manager.start_channel(channel_id)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error("Failed to start channel %s: %s", channel_id, e)
        else:
            channel_manager.stop_channel(channel_id)

    return jsonify({'success': True, 'channel': db.get_channel(channel_id)})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>', methods=['DELETE'])
def api_delete_channel(agent_id, channel_id):
    # Stop channel if running
    from backend.channels.registry import channel_manager
    channel_manager.stop_channel(channel_id)
    db.delete_channel(channel_id)
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/start', methods=['POST'])
def api_start_channel(agent_id, channel_id):
    from backend.channels.registry import channel_manager
    db.update_channel(channel_id, {'enabled': True})
    try:
        channel_manager.start_channel(channel_id)
        return jsonify({'success': True, 'running': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/stop', methods=['POST'])
def api_stop_channel(agent_id, channel_id):
    from backend.channels.registry import channel_manager
    db.update_channel(channel_id, {'enabled': False})
    channel_manager.stop_channel(channel_id)
    return jsonify({'success': True, 'running': False})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/set-primary', methods=['POST'])
def api_set_primary_channel(agent_id, channel_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    channel = db.get_channel(channel_id)
    if not channel or channel['agent_id'] != agent_id:
        return jsonify({'error': 'Channel not found for this agent'}), 404
    db.set_primary_channel(agent_id, channel_id)
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/unset-primary', methods=['POST'])
def api_unset_primary_channel(agent_id, channel_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    primary_cid = db.get_primary_channel_id(agent_id)
    if primary_cid != channel_id:
        return jsonify({'error': 'This channel is not the primary channel'}), 400
    db.unset_primary_channel(agent_id)
    return jsonify({'success': True})


# ==================== Pending Approvals API ====================


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/pending-approvals', methods=['GET'])
def api_list_pending_approvals(agent_id, channel_id):
    """Return non-expired pending approvals for a channel."""
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    channel = db.get_channel(channel_id)
    if not channel or channel['agent_id'] != agent_id:
        return jsonify({'error': 'Channel not found for this agent'}), 404
    approvals = db.get_pending_approvals(channel_id)
    return jsonify({'pending_approvals': approvals})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/pending-approvals/<pending_id>/approve', methods=['POST'])
def api_approve_pending(agent_id, channel_id, pending_id):
    """Approve a pending approval: add user to allowed_users and remove the pending record."""
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    channel = db.get_channel(channel_id)
    if not channel or channel['agent_id'] != agent_id:
        return jsonify({'error': 'Channel not found for this agent'}), 404
    success = db.approve_pending(pending_id)
    if not success:
        return jsonify({'error': 'Pending approval not found or already processed'}), 404
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/pending-approvals/<pending_id>/reject', methods=['POST'])
def api_reject_pending(agent_id, channel_id, pending_id):
    """Reject a pending approval: remove the pending record."""
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    channel = db.get_channel(channel_id)
    if not channel or channel['agent_id'] != agent_id:
        return jsonify({'error': 'Channel not found for this agent'}), 404
    success = db.reject_pending(pending_id)
    if not success:
        return jsonify({'error': 'Pending approval not found or already processed'}), 404
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/generate-pair-code', methods=['POST'])
def api_generate_pair_code(agent_id, channel_id):
    """Generate a new pairing code for a channel (admin-initiated).

    Creates a pending approval for a user_id specified in the request body,
    or returns a standalone code that the admin can hand out.
    """
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    channel = db.get_channel(channel_id)
    if not channel or channel['agent_id'] != agent_id:
        return jsonify({'error': 'Channel not found for this agent'}), 404

    from backend.channels.pairing import generate_pair_code, format_pair_code
    from datetime import datetime, timedelta

    data = request.get_json(silent=True) or {}
    external_user_id = (data.get('user_id') or '').strip()

    raw_code = generate_pair_code()
    formatted = format_pair_code(raw_code)
    expires_at = (datetime.utcnow() + timedelta(minutes=5)).isoformat()

    pending_id = db.create_pending_approval(
        channel_id=channel_id,
        external_user_id=external_user_id or '',
        user_name=data.get('user_name'),
        pair_code=raw_code,
        expires_at=expires_at,
    )

    return jsonify({'success': True, 'pair_code': formatted, 'raw_code': raw_code,
                    'expires_at': expires_at, 'pending_id': pending_id})


# ==================== WhatsApp Bridge API ====================

@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/qr', methods=['GET'])
def api_whatsapp_qr(agent_id, channel_id):
    """Return QR code data for WhatsApp channel auth."""
    from backend.channels.registry import channel_manager
    instance = channel_manager.get_channel_instance(channel_id)
    if not instance or instance.get_channel_type() != 'whatsapp':
        return jsonify({'error': 'WhatsApp channel not running'}), 404
    return jsonify(instance.get_qr())


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/bridge-status', methods=['GET'])
def api_whatsapp_bridge_status(agent_id, channel_id):
    """Return Baileys bridge connection status."""
    from backend.channels.registry import channel_manager
    instance = channel_manager.get_channel_instance(channel_id)
    if not instance or instance.get_channel_type() != 'whatsapp':
        return jsonify({'status': 'not_running'})
    return jsonify(instance.get_bridge_status())


@agents_bp.route('/api/channels/whatsapp-bridge/<channel_id>/callback', methods=['POST'])
def api_whatsapp_callback(channel_id):
    """Receive incoming WhatsApp messages from the Baileys sidecar."""
    import hmac
    from backend.channels.registry import channel_manager
    import threading
    instance = channel_manager.get_channel_instance(channel_id)
    if not instance or instance.get_channel_type() != 'whatsapp':
        return jsonify({'error': 'Channel not found'}), 404
    # Validate Bearer token set by the sidecar at startup
    auth_header = request.headers.get('Authorization', '')
    expected = f'Bearer {instance._callback_secret}'
    if not hmac.compare_digest(auth_header, expected):
        return jsonify({'error': 'Unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    threading.Thread(target=instance.handle_callback, args=(payload,), daemon=True).start()
    return jsonify({'ok': True})


# ==================== Compiled Prompt API ====================

@agents_bp.route('/api/agents/<agent_id>/compiled-prompt', methods=['GET'])
def api_compiled_prompt(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    user_id = request.args.get('user_id', 'anonymous')
    from backend.agent_runtime import agent_runtime
    context = agent_runtime.get_compiled_context(agent_id, user_id=user_id)
    return jsonify(context)


@agents_bp.route('/api/agents/<agent_id>/chat/llm-preview', methods=['GET'])
def api_llm_preview(agent_id):
    """Preview the actual messages array that would be sent to the LLM."""
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    user_id = request.args.get('user_id', 'anonymous')
    session_id = db.get_or_create_session(agent_id, user_id)

    from backend.agent_runtime import agent_runtime
    from backend.agent_runtime.context import build_system_prompt
    system_prompt = build_system_prompt(agent)
    messages = [{"role": "system", "content": system_prompt}]

    summary_record = db.get_summary(session_id, agent_id=agent_id)
    if summary_record:
        messages.append({
            "role": "system",
            "content": f"## Prior conversation summary\n{summary_record['summary']}"
        })
        raw_tail = db.get_messages_after(session_id, summary_record['last_message_id'],
                                          agent_id=agent_id)
        for msg in raw_tail:
            messages.append(agent_runtime._build_message_entry(msg, agent))
    else:
        history = db.get_session_messages(session_id, limit=50, agent_id=agent_id)
        for msg in history:
            messages.append(agent_runtime._build_message_entry(msg, agent))

    return jsonify({'messages': messages, 'has_summary': summary_record is not None})


# ==================== Chat API ====================

@agents_bp.route('/api/agents/<agent_id>/chat', methods=['POST'])
def api_chat(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    data = request.get_json()
    message = data.get('message', '').strip()
    user_id = data.get('user_id', 'anonymous')
    if not message:
        return jsonify({'error': 'Message is required'}), 400

    from backend.agent_runtime import agent_runtime
    try:
        result = agent_runtime.handle_message(agent_id, user_id, message)
        if result.get('buffered'):
            return jsonify({'success': True, 'buffered': True})
        if result.get('injected'):
            return jsonify({'success': True, 'injected': True})
        resp = {
            'success': True,
            'response': result['response'],
            'tool_trace': result.get('tool_trace', []),
            'timeline': result.get('timeline', []),
            'slash_command': result.get('slash_command', False),
            'clear_ui': result.get('clear_ui', False),
        }
        if result.get('error'):
            resp['error'] = True
        return jsonify(resp)
    except Exception as e:
        print(f"[WebChat] Error processing message for agent {agent_id}: {e}")
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/agents/<agent_id>/chat', methods=['GET'])
def api_chat_jsonl(agent_id):
    """Paginated JSONL-based chat history endpoint.

    GET /api/agents/<agent_id>/chat?session_id=<sid>&to_ts=<epoch_ms>&limit=15
      Returns up to `limit` entries with ts < to_ts, ascending. Omit to_ts for the tail.

    GET /api/agents/<agent_id>/chat?session_id=<sid>&after_ts=<epoch_ms>&limit=50
      Returns entries with ts > after_ts, ascending (for forward polling).

    Response: {"entries": [...], "has_more": bool}
      has_more is true when exactly `limit` entries were returned.
    """
    user_id = request.args.get('user_id', 'anonymous')
    session_id = request.args.get('session_id')
    to_ts = request.args.get('to_ts', type=int)
    after_ts = request.args.get('after_ts', type=int)
    limit = min(request.args.get('limit', 30, type=int), 200)

    if not session_id:
        session_id = db.get_or_create_session(agent_id, user_id)

    chatlog = chatlog_manager.get(agent_id, session_id)

    if after_ts is not None:
        # Forward scan: entries newer than after_ts
        all_entries = chatlog.get_entries_after_ts(after_ts, types=_DISPLAY_TYPES)
        entries = all_entries[:limit]
        return jsonify({'entries': entries, 'has_more': len(all_entries) > limit})

    # Backward (tail) scan: entries older than to_ts, counted by logical messages
    entries, has_more = chatlog.tail_by_messages(limit=limit, to_ts=to_ts)
    return jsonify({'entries': entries, 'has_more': has_more})


@agents_bp.route('/api/agents/<agent_id>/chat/history', methods=['GET'])
def api_chat_history(agent_id):
    user_id = request.args.get('user_id', 'anonymous')
    session_id = db.get_or_create_session(agent_id, user_id)
    messages = db.get_session_messages(session_id, limit=50, agent_id=agent_id)
    filtered = []
    for m in messages:
        if m['role'] == 'user' and m.get('content'):
            entry = {'role': m['role'], 'content': m['content']}
            if m.get('metadata'):
                entry['metadata'] = m['metadata']
            filtered.append(entry)
        elif m['role'] == 'assistant' and m.get('content') and not m.get('tool_calls'):
            entry = {'role': m['role'], 'content': m['content']}
            if m.get('metadata'):
                entry['metadata'] = m['metadata']
                if m['metadata'].get('error'):
                    entry['error'] = True
            filtered.append(entry)
        elif m['role'] == 'system' and m.get('content'):
            meta = m.get('metadata') or {}
            if meta.get('agent_state'):
                continue
            entry = {'role': m['role'], 'content': m['content']}
            if m.get('metadata'):
                entry['metadata'] = m['metadata']
            filtered.append(entry)
    return jsonify({'messages': filtered})


@agents_bp.route('/api/agents/<agent_id>/chat/poll', methods=['GET'])
def api_chat_poll(agent_id):
    """Poll for new messages after a given message ID."""
    user_id = request.args.get('user_id', 'anonymous')
    after_id = request.args.get('after', 0, type=int)
    session_id = db.get_or_create_session(agent_id, user_id)
    messages = db.get_messages_after(session_id, after_id, agent_id=agent_id)
    filtered = []
    for m in messages:
        if m['role'] == 'user' and m.get('content'):
            entry = {'id': m['id'], 'role': m['role'], 'content': m['content']}
            if m.get('metadata'):
                entry['metadata'] = m['metadata']
            filtered.append(entry)
        elif m['role'] == 'assistant' and m.get('content') and not m.get('tool_calls'):
            # Only include final assistant responses (skip intermediate tool call messages)
            entry = {'id': m['id'], 'role': m['role'], 'content': m['content']}
            if m.get('metadata'):
                entry['metadata'] = m['metadata']
                if m['metadata'].get('error'):
                    entry['error'] = True
            filtered.append(entry)
        elif m['role'] == 'system' and m.get('content'):
            meta = m.get('metadata') or {}
            if meta.get('agent_state'):
                continue
            entry = {'id': m['id'], 'role': m['role'], 'content': m['content']}
            if m.get('metadata'):
                entry['metadata'] = m['metadata']
            filtered.append(entry)
    return jsonify({'messages': filtered})


@agents_bp.route('/api/agents/<agent_id>/chat/summary', methods=['GET'])
def api_chat_summary(agent_id):
    user_id = request.args.get('user_id', 'anonymous')
    session_id = db.get_or_create_session(agent_id, user_id)
    summary = db.get_summary(session_id, agent_id=agent_id)
    if summary:
        return jsonify({'summary': summary['summary'],
                        'last_message_id': summary['last_message_id'],
                        'message_count': summary['message_count'],
                        'updated_at': summary.get('updated_at')})
    return jsonify({'summary': None})


@agents_bp.route('/api/agents/<agent_id>/chat/state', methods=['GET'])
def api_chat_agent_state(agent_id):
    """Return merged agent state (global + per-session fields).

    Global fields (focus, focus_reason) come from agent_state.
    Per-session fields (mode, tasks, plan_file, states, auto_trivial) come from
    session_state when ?session_id= is passed — matching how _restore_agent_state
    and _persist_agent_state_split work in the runtime.
    """
    from backend.agent_state import AgentState
    import json as _json

    agent_content = db.get_agent_state(agent_id=agent_id)
    agent_data = _json.loads(agent_content) if agent_content else {}

    session_id = request.args.get('session_id', '').strip()
    if session_id:
        session_content = db.get_session_state(session_id, agent_id=agent_id)
        session_data = _json.loads(session_content) if session_content else {}
        merged = {**agent_data, **session_data}
    else:
        merged = agent_data

    if merged:
        state = AgentState.deserialize(_json.dumps(merged))
        # Resolve active model badge
        active_model = None
        fb_id = agent_data.get('active_fallback_model_id')
        if fb_id:
            fb_model = db.get_model_by_id(fb_id)
            if fb_model:
                active_model = {
                    'name': fb_model.get('name', fb_id),
                    'model_name': fb_model.get('model_name', fb_id),
                    'is_fallback': True,
                    'id': fb_id,
                }
            else:
                active_model = {
                    'name': fb_id,
                    'model_name': fb_id,
                    'is_fallback': True,
                    'id': fb_id,
                }
        else:
            # Show primary model
            prim_model = db.get_agent_default_model(agent_id)
            if prim_model:
                active_model = {
                    'name': prim_model.get('name', 'unknown'),
                    'model_name': prim_model.get('model_name', 'unknown'),
                    'is_fallback': False,
                    'id': prim_model.get('id', ''),
                }
        return jsonify({
            'mode': state.mode,
            'tasks': state.tasks,
            'plan_file': state.plan_file,
            'states': state.states,
            'focus': state.focus,
            'focus_reason': state.focus_reason,
            'active_model': active_model,
        })
    return jsonify({'mode': None, 'active_model': None})


@agents_bp.route('/api/agents/<agent_id>/chat/clear', methods=['POST'])
def api_chat_clear(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404

    import os, datetime
    data = request.get_json()
    user_id = (data.get('user_id') or '').strip() or 'anonymous'

    from backend.agent_runtime import agent_runtime
    agent_runtime.clear_session(agent_id, user_id)

    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    # Truncate agent's llm.log (same as /clear slash command)
    log_path = os.path.join("logs", "agents", agent_id, "llm.log")
    if os.path.exists(log_path):
        with open(log_path, "w") as f:
            f.write(f"# LLM Log — Cleared on {now} UTC\n")

    # Truncate agent's sessrecap.log
    recap_path = os.path.join("logs", "agents", agent_id, "sessrecap.log")
    if os.path.exists(recap_path):
        with open(recap_path, "w") as f:
            f.write(f"# Session Recap Log — Cleared on {now} UTC\n")

    # Reset agent state to plan mode
    from backend.agent_state import AgentState
    fresh_state = AgentState()
    db.upsert_agent_state(fresh_state.serialize(), agent_id=agent_id)

    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/chat/session', methods=['GET'])
def api_chat_session(agent_id):
    """Return the session_id for a given agent + user, creating it if needed."""
    raw_user_id = request.args.get('user_id', 'anonymous')
    try:
        user_id = _validate_user_id(raw_user_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    session_id = db.get_or_create_session(agent_id, user_id)
    return jsonify({'session_id': session_id})


@agents_bp.route('/api/agents/<agent_id>/chat/stream', methods=['GET'])
def api_chat_stream(agent_id):
    """SSE endpoint — pushes live thinking/tool events for a session to the browser."""
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    from backend.event_stream import event_stream

    q = queue.Queue(maxsize=200)

    _SENTINEL = object()

    def _make_handler(sse_event_name, transform):
        def handler(data):
            if data.get('session_id') != session_id:
                return
            try:
                payload = transform(data)
                if payload is not None:
                    payload['seq'] = data.get('_seq')
                    q.put_nowait((sse_event_name, payload, data.get('_seq')))
            except queue.Full:
                pass
        return handler

    _TRANSFORMS = {
        'turn_begin':         ('turn_begin',       lambda d: {'ts': d.get('ts', 0)}),
        'llm_thinking':       ('thinking',         lambda d: {'content': d.get('thinking', '')}),
        'tool_call_started':  ('tool_call_started', lambda d: {
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'param_types': d.get('param_types', {}),
        }),
        'tool_executed':      ('tool_executed',    lambda d: {
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'result': d.get('tool_result', {}),
            'error': d.get('has_error', False),
        }),
        'llm_response_chunk': ('response_chunk',  lambda d: {
            'content': d.get('content', ''),
            'is_final': d.get('is_final', False),
            'send_as_message': d.get('send_as_message', False),
        }),
        'turn_complete':      ('done',             lambda d: {'thinking_duration': d.get('thinking_duration')}),
        'approval_required':  ('approval_required', lambda d: {
            'approval_id': d.get('approval_id', ''),
            'agent_id': d.get('agent_id', ''),
            'source_agent_id': d.get('source_agent_id', ''),
            'source_agent_name': d.get('source_agent_name', ''),
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'approval_info': d.get('approval_info', {}),
            'reasons': d.get('reasons', []),
            'score': d.get('score'),
        }),
        'approval_resolved':  ('approval_resolved', lambda d: {
            'approval_id': d.get('approval_id', ''),
            'decision': d.get('decision', ''),
            'timed_out': d.get('timed_out', False),
        }),
        'llm_retry': ('retry', lambda d: {
            'retry_count': d.get('retry_count', 0),
            'max_retries': d.get('max_retries', 0),
            'error_type': d.get('error_type', ''),
            'message': d.get('user_message', ''),
        }),
        'message_injected': ('message_injected', lambda d: {
            'message': d.get('message', ''),
        }),
        'message_injection_applied': ('message_injection_applied', lambda d: {
            'content': d.get('content', ''),
            'count': d.get('count', 1),
        }),
        'session_clear': ('session_clear', lambda d: {
            'session_id': d.get('session_id', ''),
            'agent_id': d.get('agent_id', ''),
        }),
        'turn_split': ('turn_split', lambda d: {}),
    }

    handlers = {
        event_name: _make_handler(sse_name, transform)
        for event_name, (sse_name, transform) in _TRANSFORMS.items()
    }

    # Client passes ?after=N when it has already replayed events 1..N via /chat/events.
    # We only pre-fill the gap (events N+1..M) that arrived between the client's replay
    # fetch and this SSE subscription, avoiding duplicate delivery of already-seen events.
    after_seq = request.args.get('after', 0, type=int)

    # Subscribe to live events BEFORE snapshotting the buffer. This ensures no events
    # are lost in the window between snapshot and subscribe. Overlap (events captured
    # by both snapshot and live handler) is safely deduplicated by seq on the client.
    for event_name, handler in handlers.items():
        event_stream.on(event_name, handler)

    event_stream.register_web_listener(session_id)

    # Snapshot buffered events after subscribing — any event emitted after this point
    # is caught by the live handler; events before are in the snapshot.
    buffered_raw = event_stream.get_session_events(session_id, after_seq)

    # Only pre-fill events from the current in-progress turn.
    # Treat turn_complete and session_clear as "boundary" events — discard everything
    # up to and including the last one so a fresh SSE connection never replays a
    # completed turn or a past session_clear that would wipe the UI.
    # IMPORTANT: Only strip on fresh connections (after_seq == 0). On reconnections
    # (after_seq > 0), the client hasn't seen these events yet and needs them —
    # especially turn_complete which finalizes the thinking bubble.
    if after_seq == 0:
        last_complete = -1
        for i, e in enumerate(buffered_raw):
            if e['event'] in ('turn_complete', 'session_clear'):
                last_complete = i
        if last_complete >= 0:
            buffered_raw = buffered_raw[last_complete + 1:]

    # Prune resolved approval cycles — if an approval_required has already been
    # followed by a matching approval_resolved, discard both. Only keep the most
    # recent unresolved approval (if any) so a reconnecting client never re-shows
    # an approval modal that was already handled.
    active_approvals = {}
    discard_set = set()
    for i, e in enumerate(buffered_raw):
        if e['event'] == 'approval_required':
            d = e.get('data', {})
            if isinstance(d, dict):
                aid = d.get('approval_id', '')
                if aid:
                    # Replace any previous unresolved approval with the same id
                    # (shouldn't happen, but guard against duplicates)
                    if aid in active_approvals:
                        discard_set.add(active_approvals[aid])
                    active_approvals[aid] = i
        elif e['event'] == 'approval_resolved':
            d = e.get('data', {})
            if isinstance(d, dict):
                aid = d.get('approval_id', '')
                if aid and aid in active_approvals:
                    discard_set.add(active_approvals[aid])
                    discard_set.add(i)
                    del active_approvals[aid]
    if discard_set:
        buffered_raw = [e for i, e in enumerate(buffered_raw) if i not in discard_set]

    # Pre-fill the queue with buffered events so a reconnecting client immediately
    # sees the in-progress reasoning trace without waiting for the next live event.
    for entry in buffered_raw:
        sse_name_transform = _TRANSFORMS.get(entry['event'])
        if sse_name_transform:
            sse_name, transform = sse_name_transform
            payload = transform(entry['data'])
            payload['seq'] = entry['seq']
            try:
                q.put_nowait((sse_name, payload, entry['seq']))
            except queue.Full:
                break

    def generate():
        try:
            while True:
                try:
                    item = q.get(timeout=30)
                except queue.Empty:
                    # No events for 30s — send a heartbeat comment to keep the connection alive.
                    # The client uses this to detect a live stream vs a stale one.
                    yield ": heartbeat\n\n"
                    continue
                sse_event, payload, seq = item
                id_line = f"id: {seq}\n" if seq is not None else ''
                yield f"{id_line}event: {sse_event}\ndata: {json.dumps(payload)}\n\n"
                if sse_event == 'done':
                    break
        finally:
            event_stream.unregister_web_listener(session_id)
            for event_name, handler in handlers.items():
                event_stream.off(event_name, handler)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@agents_bp.route('/api/approvals/stream', methods=['GET'])
def api_approvals_stream():
    """Global SSE endpoint — pushes ALL approval events (any agent, any session)
    to every connected client. Unlike the per-session chat stream, there is no
    session_id filtering — this is exactly the point: approval modals need to
    appear on Dashboard, Settings, Skills, and any other page, not just /agents/:id.
    """
    from backend.event_stream import event_stream

    q = queue.Queue(maxsize=200)

    _TRANSFORMS = {
        'approval_required': ('approval_required', lambda d: {
            'approval_id': d.get('approval_id', ''),
            'agent_id': d.get('agent_id', ''),
            'source_agent_id': d.get('source_agent_id', ''),
            'source_agent_name': d.get('source_agent_name', ''),
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'approval_info': d.get('approval_info', {}),
            'reasons': d.get('reasons', []),
            'score': d.get('score'),
        }),
        'approval_resolved': ('approval_resolved', lambda d: {
            'approval_id': d.get('approval_id', ''),
            'decision': d.get('decision', ''),
            'timed_out': d.get('timed_out', False),
        }),
    }

    def _make_handler(sse_event_name, transform):
        def handler(data):
            try:
                payload = transform(data)
                if payload is not None:
                    payload['seq'] = data.get('_seq')
                    q.put_nowait((sse_event_name, payload, data.get('_seq')))
            except queue.Full:
                pass
        return handler

    handlers = {}
    for event_name, (sse_name, transform) in _TRANSFORMS.items():
        h = _make_handler(sse_name, transform)
        handlers[event_name] = h
        event_stream.on(event_name, h)

    def generate():
        try:
            while True:
                try:
                    item = q.get(timeout=30)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                sse_event, payload, seq = item
                id_line = f"id: {seq}\n" if seq is not None else ''
                yield f"{id_line}event: {sse_event}\ndata: {json.dumps(payload)}\n\n"
        finally:
            for event_name, handler in handlers.items():
                event_stream.off(event_name, handler)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@agents_bp.route('/api/agents/<agent_id>/chat/events', methods=['GET'])
def api_chat_events(agent_id):
    """Fetch missed SSE events by sequence range for gap-detection recovery."""
    session_id = request.args.get('session_id')
    after_seq = request.args.get('after', type=int)
    up_to_seq = request.args.get('up_to', type=int)
    if not session_id or after_seq is None:
        return jsonify({'error': 'session_id and after required'}), 400
    if up_to_seq is not None and up_to_seq - after_seq > 200:
        return jsonify({'error': 'range too large (max 200)'}), 400

    from backend.event_stream import event_stream

    _TRANSFORM_MAP = {
        'turn_begin':        ('turn_begin',      lambda d: {'ts': d.get('ts', 0)}),
        'llm_thinking':      ('thinking',        lambda d: {'content': d.get('thinking', '')}),
        'tool_call_started': ('tool_call_started', lambda d: {'tool': d.get('tool_name', ''), 'args': d.get('tool_args', {}), 'param_types': d.get('param_types', {})}),
        'tool_executed':     ('tool_executed',    lambda d: {'tool': d.get('tool_name', ''), 'args': d.get('tool_args', {}), 'result': d.get('tool_result', {}), 'error': d.get('has_error', False)}),
        'llm_response_chunk':('response_chunk',  lambda d: {'content': d.get('content', ''), 'is_final': d.get('is_final', False), 'send_as_message': d.get('send_as_message', False)}),
        'turn_complete':     ('done',             lambda d: {'thinking_duration': d.get('thinking_duration')}),
        'approval_required': ('approval_required', lambda d: {'approval_id': d.get('approval_id', ''), 'agent_id': d.get('agent_id', ''), 'source_agent_id': d.get('source_agent_id', ''), 'source_agent_name': d.get('source_agent_name', ''), 'tool': d.get('tool_name', ''), 'args': d.get('tool_args', {}), 'approval_info': d.get('approval_info', {}), 'reasons': d.get('reasons', []), 'score': d.get('score')}),
        'approval_resolved': ('approval_resolved', lambda d: {'approval_id': d.get('approval_id', ''), 'decision': d.get('decision', ''), 'timed_out': d.get('timed_out', False)}),
        'llm_retry':         ('retry',             lambda d: {'retry_count': d.get('retry_count', 0), 'max_retries': d.get('max_retries', 0), 'error_type': d.get('error_type', ''), 'message': d.get('user_message', '')}),
        'turn_split':        ('turn_split',        lambda d: {}),
    }

    if up_to_seq is None:
        raw = event_stream.get_session_events(session_id, after_seq)
    else:
        raw = event_stream.get_events_in_range(session_id, after_seq, up_to_seq)
    events = []
    for entry in raw:
        event_name = entry['event']
        if event_name in _TRANSFORM_MAP:
            sse_name, transform = _TRANSFORM_MAP[event_name]
            payload = transform(entry['data'])
            payload['seq'] = entry['seq']
            events.append({'event': sse_name, 'seq': entry['seq'], 'data': payload})

    return jsonify({'events': events})


@agents_bp.route('/api/agents/<agent_id>/chat/approve', methods=['POST'])
def api_chat_approve(agent_id):
    """Resolve a pending tool approval (approve or reject)."""
    from backend.agent_runtime.approval import approval_registry
    data = request.get_json() or {}
    approval_id = data.get('approval_id', '').strip()
    decision = data.get('decision', '').strip()

    if not approval_id or decision not in ('approve', 'reject'):
        return jsonify({'error': 'approval_id and decision (approve/reject) required'}), 400

    pending = approval_registry.get(approval_id)
    if not pending:
        return jsonify({'error': 'Approval not found or expired'}), 404
    if pending.agent_id != agent_id:
        return jsonify({'error': 'Approval does not belong to this agent'}), 403

    success = approval_registry.resolve(approval_id, decision)
    if not success:
        return jsonify({'error': 'Approval already resolved'}), 409

    return jsonify({'ok': True, 'decision': decision})


@agents_bp.route('/api/agents/busy', methods=['GET'])
def api_agents_busy():
    """Return all agents currently processing an LLM turn."""
    from backend.agent_runtime import agent_runtime
    return jsonify({'busy': agent_runtime.get_busy_agents()})


@agents_bp.route('/api/agents/<agent_id>/busy', methods=['GET'])
def api_agent_busy(agent_id):
    """Return whether a specific agent is currently processing an LLM turn."""
    from backend.agent_runtime import agent_runtime
    busy = agent_runtime.is_agent_busy(agent_id)
    result = {'busy': busy}
    if busy:
        snapshot = agent_runtime.get_busy_agents()
        entry = snapshot.get(agent_id, {})
        result['session_id'] = entry.get('session_id')
        result['elapsed'] = entry.get('elapsed')
    return jsonify(result)


@agents_bp.route('/api/agents/status/stream', methods=['GET'])
def api_agents_status_stream():
    """SSE endpoint — pushes real-time agent busy/idle status changes.

    Subscribes to the 'agent_busy_changed' event (emitted by AgentRuntime
    when an agent starts or finishes an LLM turn) and forwards changes as
    SSE events to every connected client.  No session filtering — the
    browser-side JS decides which agent card to update.

    Events:
        event: agent_busy_changed
        data: {"agent_id": "...", "busy": true|false, "session_id": "..."}
    """
    import queue as _queue
    from backend.event_stream import event_stream

    q = _queue.Queue(maxsize=200)

    def handler(data):
        try:
            payload = {
                'agent_id': data.get('agent_id', ''),
                'busy': data.get('busy', False),
                'session_id': data.get('session_id', ''),
            }
            q.put_nowait(payload)
        except _queue.Full:
            pass

    event_stream.on('agent_busy_changed', handler)

    def generate():
        try:
            while True:
                try:
                    payload = q.get(timeout=30)
                except _queue.Empty:
                    # Heartbeat to keep the connection alive through proxies
                    yield ': heartbeat\n\n'
                    continue
                yield f'event: agent_busy_changed\ndata: {json.dumps(payload)}\n\n'
        finally:
            event_stream.off('agent_busy_changed', handler)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# ==================== Portal API ====================


@agents_bp.route('/api/agents/<agent_id>/portals', methods=['GET'])
def api_list_portals(agent_id):
    """List all portals for an agent."""
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    portals = db.get_agent_portals(agent_id)
    # Parse backend_config from JSON strings
    for p in portals:
        cfg = p.get('backend_config', '{}')
        if isinstance(cfg, str):
            try:
                p['backend_config'] = json.loads(cfg)
            except (json.JSONDecodeError, TypeError):
                p['backend_config'] = {}
    return jsonify({'portals': portals})


@agents_bp.route('/api/agents/<agent_id>/portals', methods=['POST'])
def api_create_portal(agent_id):
    """Create a new portal for an agent."""
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    virtual_path = (data.get('virtual_path') or '').strip()
    backend_type = (data.get('backend_type') or '').strip()
    real_path = (data.get('real_path') or '').strip()

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if not virtual_path:
        return jsonify({'error': 'virtual_path is required'}), 400
    if backend_type not in ('local', 'ssh', 'evonet'):
        return jsonify({'error': 'backend_type must be local, ssh, or evonet'}), 400
    if not real_path:
        return jsonify({'error': 'real_path is required'}), 400

    backend_config = data.get('backend_config', {})
    if isinstance(backend_config, str):
        try:
            backend_config = json.loads(backend_config)
        except (json.JSONDecodeError, ValueError):
            return jsonify({'error': 'backend_config must be valid JSON'}), 400

    portal_data = {
        'agent_id': agent_id,
        'name': name,
        'virtual_path': virtual_path,
        'backend_type': backend_type,
        'backend_config': backend_config,
        'real_path': real_path,
    }
    portal_id = db.create_portal(portal_data)

    # Invalidate portal cache for this agent
    from backend.tools._portal import invalidate_portal_cache
    invalidate_portal_cache(agent_id)

    portal = db.get_portal(portal_id)
    cfg = portal.get('backend_config', '{}')
    if isinstance(cfg, str):
        try:
            portal['backend_config'] = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            portal['backend_config'] = {}
    return jsonify(portal), 201


@agents_bp.route('/api/portals/<portal_id>', methods=['PUT'])
def api_update_portal(portal_id):
    """Update a portal's configuration."""
    portal = db.get_portal(portal_id)
    if not portal:
        return jsonify({'error': 'Portal not found'}), 404

    data = request.get_json() or {}
    updates = {}
    if 'name' in data:
        updates['name'] = (data['name'] or '').strip()
    if 'virtual_path' in data:
        updates['virtual_path'] = (data['virtual_path'] or '').strip()
    if 'backend_type' in data:
        btype = (data['backend_type'] or '').strip()
        if btype not in ('local', 'ssh', 'evonet'):
            return jsonify({'error': 'backend_type must be local, ssh, or evonet'}), 400
        updates['backend_type'] = btype
    if 'real_path' in data:
        updates['real_path'] = (data['real_path'] or '').strip()
    if 'backend_config' in data:
        cfg = data['backend_config']
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except (json.JSONDecodeError, ValueError):
                return jsonify({'error': 'backend_config must be valid JSON'}), 400
        updates['backend_config'] = json.dumps(cfg)

    if updates:
        db.update_portal(portal_id, updates)

    # Invalidate portal cache for the portal's agent
    from backend.tools._portal import invalidate_portal_cache
    invalidate_portal_cache(portal['agent_id'])

    portal = db.get_portal(portal_id)
    cfg = portal.get('backend_config', '{}')
    if isinstance(cfg, str):
        try:
            portal['backend_config'] = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            portal['backend_config'] = {}
    return jsonify(portal)


@agents_bp.route('/api/portals/<portal_id>', methods=['DELETE'])
def api_delete_portal(portal_id):
    """Delete a portal and disconnect its backend."""
    portal = db.get_portal(portal_id)
    if not portal:
        return jsonify({'error': 'Portal not found'}), 404

    # Disconnect backend if active
    try:
        from backend.portals import portal_manager
        portal_manager.disconnect(portal_id)
    except Exception:
        pass

    db.delete_portal(portal_id)

    # Invalidate portal cache
    from backend.tools._portal import invalidate_portal_cache
    invalidate_portal_cache(portal['agent_id'])

    return jsonify({'ok': True})


@agents_bp.route('/api/portals/<portal_id>/connect', methods=['POST'])
def api_portal_connect(portal_id):
    """Test connection for a portal — creates the backend if not already active."""
    portal = db.get_portal(portal_id)
    if not portal:
        return jsonify({'error': 'Portal not found'}), 404

    # Parse backend_config to dict
    cfg = portal.get('backend_config', '{}')
    if isinstance(cfg, str):
        try:
            portal['backend_config'] = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            portal['backend_config'] = {}

    try:
        from backend.portals import portal_manager
        backend = portal_manager.get_backend(portal)
        s = backend.status()
        db.update_portal_status(portal_id, 'connected')
        return jsonify({'ok': True, 'status': 'connected', 'backend': s})
    except Exception as e:
        db.update_portal_status(portal_id, 'disconnected', str(e))
        return jsonify({'ok': False, 'error': str(e)}), 500


@agents_bp.route('/api/portals/<portal_id>/disconnect', methods=['POST'])
def api_portal_disconnect(portal_id):
    """Disconnect a portal's backend."""
    portal = db.get_portal(portal_id)
    if not portal:
        return jsonify({'error': 'Portal not found'}), 404

    try:
        from backend.portals import portal_manager
        result = portal_manager.disconnect(portal_id)
        db.update_portal_status(portal_id, 'disconnected')
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
