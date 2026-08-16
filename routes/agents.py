"""
Agent Management Blueprint — CRUD for agents, KB files, tools, and channels.
"""

import os
import re
import json
import shlex
import uuid
import queue
import logging
from typing import Dict, Any, List, Optional
from flask import Blueprint, render_template, jsonify, request, Response, session, stream_with_context, g
from models.db import db
from models.chatlog import chatlog_manager, _DISPLAY_TYPES
from backend.agent_portability import AgentPortabilityError, export_agent, import_agent, preflight_import
from backend.audit_logger import audit
from backend.tools import tool_registry
from backend.tools.agent_messaging import get_agent_messaging_tool_defs
from backend.tools.super_agent_tools import _sync_skill_tools

# Agent messaging tool IDs (auto-loaded when agent_messaging_enabled)
AGENT_MESSAGING_TOOL_IDS = frozenset({
    'send_agent_message',
    'escalate_to_user',
    'resolve_agent_approval',
    'list_sessions',
    'send_channel_message',
})
from backend.agent_runtime.evomem_client import get_graph_for_viz, get_engine
from backend.agent_runtime.context import validate_kb_frontmatter

logger = logging.getLogger(__name__)

agents_bp = Blueprint('agents', __name__)

def _audit_ip():
    return request.remote_addr or ''

_SENSITIVE_AGENT_KEYS = frozenset({'workspace'})



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
    """Docker sandbox is only supported on local workplaces.

    Remote/tunnel workplaces execute elsewhere, and bwrap workplaces provide
    their own isolation — all three force sandbox_enabled off.

    For bwrap workplaces, additionally verify that the host can actually
    enter the namespaces created by bubblewrap (catches WSL2 and similar
    environments with broken user-namespace support).  Raises ValueError
    with a descriptive message if the environment is incompatible.
    """
    if not workplace_id:
        return
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return
    wp_type = workplace.get('type')
    if wp_type in ('remote', 'tunnel', 'bwrap'):
        agent_data['sandbox_enabled'] = 0
    if wp_type == 'bwrap':
        from backend.tools.lib.backends.bwrap_backend import _availability_error
        # Fast path: binary-level check first (no overhead)
        err = _availability_error()
        if err:
            raise ValueError(err)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(BASE_DIR, 'agents')
WORKSPACE_DIR = os.path.join(BASE_DIR, 'shared', 'agents')

SLUG_RE = re.compile(r'^[a-z0-9_]+$')

# Sub-agent IDs follow the pattern parent_id_sub_N — reject user-created IDs that match this.
SUBAGENT_ID_RE = re.compile(r'_sub_\d+$')
USER_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\.@]{1,128}$')

# Tools managed exclusively by the artifacts_enabled agent setting.
# These cannot be toggled manually in the Tools tab — they are read-only.
ARTIFACT_TOOLS = frozenset({
    'save_artifact',
    'list_artifacts',
    'read_attachment',
    'cleanup_attachments',
    'portal_copy',
    'copy_status',
})

VISION_TOOLS = frozenset({'describe_image'})


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


def _sanitize_kb_path(filename: str) -> str:
    """Sanitize a KB file path: reject '..' traversal, allow subdirectory paths."""
    if not filename or '..' in filename or '\\' in filename:
        raise ValueError('Invalid filename')
    filename = filename.strip().lstrip('/')
    if not filename:
        raise ValueError('Invalid filename')
    if os.path.isabs(filename):
        raise ValueError('Invalid filename')
    normalized = os.path.normpath(filename)
    if normalized.startswith('..') or os.path.isabs(normalized):
        raise ValueError('Invalid filename')
    return normalized


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


_migration_done = False


def _ensure_migration():
    """Lazy wrapper: runs _migrate_system_prompts() once on first call."""
    global _migration_done
    if not _migration_done:
        _migrate_system_prompts()
        _migration_done = True


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
    else:
        # Skip local filesystem check for remote/tunnel workplaces (path is on remote host)
        wp_id = agent.get('workplace_id', '').strip() if agent.get('workplace_id') else ''
        is_remote_workplace = False
        if wp_id:
            workplace = db.get_workplace(wp_id)
            if workplace and workplace.get('type') in ('remote', 'tunnel'):
                is_remote_workplace = True
        if not is_remote_workplace and not os.path.isdir(ws):
            workspace_invalid = True
    g.agent_id = agent_id
    return render_template('agent_detail.html', agent=agent,
                           DEFAULT_SUMMARIZE_PROMPT=DEFAULT_SUMMARIZE_PROMPT,
                           workspace_invalid=workspace_invalid,
                           workspace_path=ws if ws else '(not set)',
                           memory_engine=get_engine())


# ==================== Agent CRUD API ====================

@agents_bp.route('/api/agents', methods=['GET'])
def api_list_agents():
    _ensure_migration()
    agents = db.get_agents()
    return jsonify({'agents': _sanitize_agents(agents)})


@agents_bp.route('/api/agents/<agent_id>', methods=['GET'])
def api_get_agent(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
    agent_tools = db.get_agent_tools(agent_id)
    # Agent-messaging tools are part of the built-in executor chain. Do not
    # report them as assigned when that chain is disabled for this agent.
    if (agent.get('builtin_tools_enabled', True)
            and (agent.get('is_super') or agent.get('agent_messaging_enabled') != 0)):
        for tid in AGENT_MESSAGING_TOOL_IDS:
            if tid not in agent_tools:
                agent_tools = list(agent_tools) + [tid]
    agent['tools'] = agent_tools
    agent['channels'] = db.get_channels(agent_id)
    # Detect orphaned tools (skill uninstalled)
    known_ids = set()
    for td in tool_registry.get_all_tool_defs():
        known_ids.add(td.get('function', {}).get('name') or td.get('id', ''))
        if td.get('id'):
            known_ids.add(td['id'])
    # Agent messaging tools are auto-loaded — don't flag them as missing
    known_ids |= AGENT_MESSAGING_TOOL_IDS
    agent['missing_tools'] = [t for t in agent['tools'] if t not in known_ids]
    return jsonify(_sanitize_agent(agent))


@agents_bp.route('/api/agents/<agent_id>/commands', methods=['GET'])
def api_list_agent_commands(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    from backend.slash_commands import list_available_commands

    commands = [cmd.to_dict() for cmd in list_available_commands(agent_id)]
    return jsonify({'commands': commands})


@agents_bp.route('/api/agents/<agent_id>/export', methods=['GET'])
def api_export_agent(agent_id):
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    try:
        content = json.dumps(export_agent(db, agent_id, BASE_DIR), indent=2, ensure_ascii=False) + '\n'
        response = Response(content, mimetype='application/json')
        response.headers['Content-Disposition'] = f'attachment; filename="{agent_id}.agent.json"'
        return response
    except AgentPortabilityError as exc:
        return jsonify({'error': str(exc)}), 400


@agents_bp.route('/api/agents/import', methods=['POST'])
def api_import_agent():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Request body must be a JSON object.'}), 400
    if 'payload' in data:
        payload = data.get('payload')
        target_id = data.get('id')
        target_name = data.get('name')
    else:
        payload = data
        target_id = request.args.get('id')
        target_name = request.args.get('name')
    try:
        preflight = preflight_import(db, payload)
        warning = preflight['warning']
        if (warning['skills'] or warning['tools']) and not data.get('confirm_missing', False):
            return jsonify({'warning': warning, 'message': 'Unavailable dependencies will be skipped. Confirm to continue.'}), 409
        imported_id = import_agent(
            db, payload, BASE_DIR, agent_id=target_id, name=target_name,
            confirm_missing=bool(data.get('confirm_missing', False))
        )
        audit.log_agent_crud(
            user_id='admin', agent_id=imported_id, action='create', ip=_audit_ip()
        )
        return jsonify({'success': True, 'agent_id': imported_id}), 201
    except AgentPortabilityError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.exception('Agent import failed')
        return jsonify({'error': 'Agent import failed and was rolled back.'}), 500


@agents_bp.route('/api/agents', methods=['POST'])
def api_create_agent():
    if not db.has_super_agent():
        return jsonify({'error': 'Super agent must be set up before creating other agents.', 'setup_required': True}), 400
    data = request.get_json()
    agent_id = data.get('id', '').strip().lower()
    if not agent_id or not SLUG_RE.match(agent_id):
        return jsonify({'error': 'Invalid ID. Use only lowercase alphanumeric characters and underscores (snake_case).'}), 400
    if SUBAGENT_ID_RE.search(agent_id):
        return jsonify({'error': 'Agent ID cannot end with a sub-agent pattern (e.g. _sub_1). This naming convention is reserved for internal use.'}), 400
    if db.get_agent(agent_id):
        return jsonify({'error': 'Agent ID already exists.'}), 400
    if len(data.get('name', '')) > 200:
        return jsonify({'error': 'Name too long (max 200 characters).'}), 400
    if len(data.get('description', '')) > 2000:
        return jsonify({'error': 'Description too long (max 2000 characters).'}), 400
    if len(data.get('system_prompt', '')) > 102400:
        return jsonify({'error': 'System prompt too long (max 100 KB).'}), 400
    try:
        _apply_sandbox_workplace_policy(data, data.get('workplace_id'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
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
        # Add artifact tools for agents with artifacts enabled
        artifacts_enabled = data.get('artifacts_enabled')
        if artifacts_enabled is None or artifacts_enabled:
            for tool_id in ARTIFACT_TOOLS:
                db.add_agent_tool(agent_id, tool_id)
        # Add vision tools for agents with vision enabled
        vision_enabled = data.get('vision_enabled')
        if vision_enabled is None or vision_enabled:
            for tool_id in VISION_TOOLS:
                db.add_agent_tool(agent_id, tool_id)

        # Copy default knowledge base files from defaults/ directory
        import shutil as _shutil
        _defaults_dir = os.path.join(BASE_DIR, 'defaults')

        # evonic.md (from super_agent_kb_evonic.md)
        _src = os.path.join(_defaults_dir, 'super_agent_kb_evonic.md')
        if os.path.isfile(_src):
            _shutil.copy2(_src, os.path.join(_kb_dir(agent_id), 'evonic.md'))

        # reminder-and-schedule-creation-rules.md (scheduler/reminder guide)
        _src = os.path.join(_defaults_dir, 'reminder-and-schedule-creation-rules.md')
        if os.path.isfile(_src):
            _shutil.copy2(_src, os.path.join(_kb_dir(agent_id), 'reminder-and-schedule-creation-rules.md'))

        # evonet.md (Evonet connector reference)
        _src = os.path.join(_defaults_dir, 'evonet.md')
        if os.path.isfile(_src):
            _shutil.copy2(_src, os.path.join(_kb_dir(agent_id), 'evonet.md'))

        agent = db.get_agent(agent_id)
        agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
        audit.log_agent_crud(user_id='admin', agent_id=agent_id, action='create', ip=_audit_ip())
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
    if 'messaging_acl_mode' in data and data['messaging_acl_mode'] not in ('whitelist', 'blacklist'):
        return jsonify({'error': "messaging_acl_mode must be 'whitelist' or 'blacklist'."}), 400
    if 'messaging_acl' in data and data['messaging_acl'] is not None:
        import json as _json
        if isinstance(data['messaging_acl'], list):
            data['messaging_acl'] = _json.dumps(data['messaging_acl'])
        elif isinstance(data['messaging_acl'], str):
            try:
                _json.loads(data['messaging_acl'])
            except _json.JSONDecodeError:
                return jsonify({'error': 'messaging_acl must be a JSON array of agent IDs.'}), 400
    target_workplace_id = data.get('workplace_id', existing.get('workplace_id'))
    try:
        _apply_sandbox_workplace_policy(data, target_workplace_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if 'system_prompt' in data:
        _write_system_prompt(agent_id, data['system_prompt'])
    # Handle artifacts_enabled toggle: manage all artifact tools
    if 'artifacts_enabled' in data:
        old_artifacts = existing.get('artifacts_enabled', True) if existing.get('artifacts_enabled') is not None else True
        new_artifacts = bool(data['artifacts_enabled'])
        if new_artifacts != old_artifacts:
            if new_artifacts:
                for tool_id in ARTIFACT_TOOLS:
                    db.add_agent_tool(agent_id, tool_id)
            else:
                for tool_id in ARTIFACT_TOOLS:
                    db.remove_agent_tool(agent_id, tool_id)
    # Handle vision_enabled toggle: manage vision tools
    if 'vision_enabled' in data:
        old_vision = bool(existing.get('vision_enabled', 1))
        new_vision = bool(data['vision_enabled'])
        if new_vision != old_vision:
            if new_vision:
                for tool_id in VISION_TOOLS:
                    db.add_agent_tool(agent_id, tool_id)
            else:
                for tool_id in VISION_TOOLS:
                    db.remove_agent_tool(agent_id, tool_id)
    if 'model_id' in data and data['model_id'] == '':
        data['model_id'] = None  # empty string resets to global default
    db.update_agent(agent_id, data)
    agent = db.get_agent(agent_id)
    agent['system_prompt'] = _read_system_prompt(agent_id, fallback=agent.get('system_prompt', ''))
    audit.log_agent_crud(user_id='admin', agent_id=agent_id, action='update', ip=_audit_ip())
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
    # Drop the cached chat DB so a recreated agent with the same id gets a
    # fresh connection instead of a stale handle on the deleted chat.db inode.
    try:
        from models.chat import agent_chat_manager
        agent_chat_manager.drop(agent_id)
    except Exception:
        pass
    audit.log_agent_crud(user_id='admin', agent_id=agent_id, action='delete', ip=_audit_ip())
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
    if SUBAGENT_ID_RE.search(new_id):
        return jsonify({'error': 'Agent ID cannot end with a sub-agent pattern (e.g. _sub_1). This naming convention is reserved for internal use.'}), 400
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
    audit.log_agent_crud(user_id='admin', agent_id=agent_id, action='clone', ip=_audit_ip(), detail=f'cloned_to={cloned_id}')
    return jsonify({'success': True, 'agent': _sanitize_agent(agent)})


# ==================== Agent Tools API ====================

@agents_bp.route('/api/agents/<agent_id>/tools', methods=['GET'])
def api_get_agent_tools(agent_id):
    tool_ids = db.get_agent_tools(agent_id)
    agent = db.get_agent(agent_id)
    # Agent-messaging tools are part of the built-in executor chain. Do not
    # report them as assigned when that chain is disabled for this agent.
    if (agent and agent.get('builtin_tools_enabled', True)
            and (agent.get('is_super') or agent.get('agent_messaging_enabled') != 0)):
        for tid in AGENT_MESSAGING_TOOL_IDS:
            if tid not in tool_ids:
                tool_ids = list(tool_ids) + [tid]
    return jsonify({'tools': tool_ids})


@agents_bp.route('/api/agents/<agent_id>/tools', methods=['PUT'])
def api_set_agent_tools(agent_id):
    data = request.get_json()
    tool_ids = data.get('tools', [])
    # Enforce artifacts_enabled lock: artifact tools are managed exclusively
    # by the agent's artifacts_enabled setting, not via manual toggle.
    agent = db.get_agent(agent_id)
    if agent:
        artifacts_enabled = agent.get('artifacts_enabled', True)
        if artifacts_enabled:
            for tool_id in ARTIFACT_TOOLS:
                if tool_id not in tool_ids:
                    tool_ids.append(tool_id)
        else:
            tool_ids = [tid for tid in tool_ids if tid not in ARTIFACT_TOOLS]
        # Enforce vision_enabled lock: vision tools managed by vision setting
        vision_enabled = agent.get('vision_enabled', 1)
        if vision_enabled:
            for tool_id in VISION_TOOLS:
                if tool_id not in tool_ids:
                    tool_ids.append(tool_id)
        else:
            tool_ids = [tid for tid in tool_ids if tid not in VISION_TOOLS]
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
    result = _sync_skill_tools(agent_id, skill_ids)
    return jsonify({
        'success': True,
        'skills': skill_ids,
        'tools_added': result['tools_added'],
        'tools_removed': result['tools_removed'],
    })


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

def _build_kb_tree(kb_dir: str, rel_path: str = '') -> list:
    """Recursively build a tree of KB files and directories."""
    items = []
    current_dir = os.path.join(kb_dir, rel_path) if rel_path else kb_dir
    try:
        entries = sorted(os.listdir(current_dir))
    except OSError:
        return items

    for entry in entries:
        # Hide dot-prefixed entries — the evomem index (.evomem.db) and the
        # auto-managed .gitignore live in kb/ but are not user KB files.
        if entry.startswith('.'):
            continue
        full_path = os.path.join(current_dir, entry)
        rel = os.path.join(rel_path, entry).replace('\\', '/') if rel_path else entry
        if os.path.isdir(full_path):
            children = _build_kb_tree(kb_dir, rel)
            items.append({
                'name': entry,
                'type': 'dir',
                'path': rel,
                'children': children
            })
        elif os.path.isfile(full_path):
            stat = os.stat(full_path)
            items.append({
                'name': entry,
                'type': 'file',
                'path': rel,
                'size': stat.st_size,
                'modified': stat.st_mtime
            })
    return items


@agents_bp.route('/api/agents/<agent_id>/kb', methods=['GET'])
def api_list_kb(agent_id):
    kb = _kb_dir(agent_id)
    if not os.path.isdir(kb):
        return jsonify({'tree': []})
    tree = _build_kb_tree(kb)
    return jsonify({'tree': tree})


@agents_bp.route('/api/agents/<agent_id>/kb/<path:filename>', methods=['GET'])
def api_get_kb_file(agent_id, filename):
    try:
        filename = _sanitize_kb_path(filename)
    except ValueError:
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
        try:
            fname = _sanitize_kb_path(fname)
        except ValueError:
            return jsonify({'error': 'Invalid filename'}), 400
        fpath = os.path.join(kb, fname)
        if fname.endswith('.md'):
            err = validate_kb_frontmatter(f.read().decode('utf-8', 'replace'))
            f.stream.seek(0)
            if err:
                return jsonify({'error': err}), 400
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        f.save(fpath)
        return jsonify({'success': True, 'filename': fname})
    else:
        data = request.get_json()
        fname = data.get('filename', '').strip()
        content = data.get('content', '')
        if not fname:
            return jsonify({'error': 'filename is required'}), 400
        try:
            fname = _sanitize_kb_path(fname)
        except ValueError:
            return jsonify({'error': 'Invalid filename'}), 400
        if fname.endswith('.md'):
            err = validate_kb_frontmatter(content)
            if err:
                return jsonify({'error': err}), 400
        fpath = os.path.join(kb, fname)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True, 'filename': fname})


@agents_bp.route('/api/agents/<agent_id>/kb/<path:filename>', methods=['PUT'])
def api_update_kb_file(agent_id, filename):
    try:
        filename = _sanitize_kb_path(filename)
    except ValueError:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_kb_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    data = request.get_json()
    content = data.get('content', '')
    if filename.endswith('.md'):
        err = validate_kb_frontmatter(content)
        if err:
            return jsonify({'error': err}), 400
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({'success': True, 'filename': filename})


@agents_bp.route('/api/agents/<agent_id>/kb/<path:filename>', methods=['DELETE'])
def api_delete_kb_file(agent_id, filename):
    try:
        filename = _sanitize_kb_path(filename)
    except ValueError:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_kb_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    os.remove(fpath)
    return jsonify({'success': True})


def _extract_kb_meta(filepath: str) -> dict:
    """Extract display metadata from a KB markdown file in a single read.

    Returns {'title': str|None, 'type': str|None}.

    Title priority:
    1. Frontmatter `title`
    2. First # Heading (level 1) in the body
    3. Frontmatter `description`
    4. None (caller falls back to evomem-generated title or slug)
    """
    import re
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return {'title': None, 'type': None}

    fm_title = None
    description = None
    page_type = None
    thumbnail = None
    body = content

    # Strip YAML frontmatter (--- ... ---)
    if content.startswith('---'):
        second = content.find('---', 3)
        if second != -1:
            fm = content[3:second]
            body = content[second + 3:]
            for line in fm.split('\n'):
                line = line.strip()
                if line.startswith('title:'):
                    fm_title = line[len('title:'):].strip().strip('"\'') or None
                elif line.startswith('description:'):
                    description = line[len('description:'):].strip().strip('"\'') or None
                elif line.startswith('type:'):
                    page_type = line[len('type:'):].strip().strip('"\'') or None
                elif line.startswith('thumbnail:'):
                    thumbnail = line[len('thumbnail:'):].strip().strip('"\'') or None

    if fm_title:
        return {'title': fm_title, 'type': page_type, 'thumbnail': thumbnail}

    # Fall back to first # Heading from body
    for line in body.split('\n'):
        line = line.strip()
        m = re.match(r'^#\s+(.+?)(?:\s+#+)?$', line)
        if m:
            heading = m.group(1).strip()
            if heading:
                return {'title': heading, 'type': page_type, 'thumbnail': thumbnail}

    return {'title': description, 'type': page_type, 'thumbnail': thumbnail}


# ==================== KB Graph API ====================


@agents_bp.route('/api/agents/<agent_id>/kb-graph', methods=['GET'])
def api_kb_graph(agent_id):
    """Return the knowledge graph (KB pages + entities + typed edges) for viz."""
    if not session.get('authenticated'):
        return jsonify({'error': 'Authentication required'}), 401
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404

    from backend.agent_runtime.memory_manager import get_kb_activity
    stats = get_kb_activity(agent_id)

    graph = get_graph_for_viz(agent_id)
    if not graph or not graph.get('nodes'):
        return jsonify({'pages': {}, 'links': [], 'dangling_links': [], 'stats': stats})

    nodes = graph['nodes']
    links = graph['links']
    dangling_links = graph['dangling']
    kb_dir = _kb_dir(agent_id)

    # Incoming/outgoing degree per node, from the resolved links.
    inc, out = {}, {}
    for l in links:
        out[l['source']] = out.get(l['source'], 0) + 1
        inc[l['target']] = inc.get(l['target'], 0) + 1

    pages = {}
    for slug, node in nodes.items():
        is_entity = node.get('source_dir') == 'entities'
        title = node.get('title') or slug
        node_type = node.get('type')
        thumbnail = None
        if not is_entity:
            # Top-level KB doc: prefer the frontmatter title/type from disk.
            meta = _extract_kb_meta(os.path.join(kb_dir, slug + '.md'))
            if meta.get('title'):
                title = meta['title']
            node_type = meta.get('type') or node_type
            thumbnail = meta.get('thumbnail')
        pages[slug] = {
            'title': title,
            'type': node_type,
            'tags': node.get('tags', []),
            'is_entity': is_entity,
            'incoming_count': inc.get(slug, 0),
            'outgoing_count': out.get(slug, 0),
            'thumbnail': thumbnail,
        }

    return jsonify({
        'pages': pages,
        'links': links,                 # each carries source/target/edge_type
        'dangling_links': dangling_links,
        'stats': stats,
    })


@agents_bp.route('/api/agents/<agent_id>/kb-sync', methods=['POST'])
def api_kb_sync(agent_id):
    """Force a full evomem memory sync for the agent."""
    if not session.get('authenticated'):
        return jsonify({'error': 'Authentication required'}), 401
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    if get_engine() != 'evomem':
        return jsonify({'error': 'Memory engine is not evomem'}), 400

    from backend.agent_runtime.evomem_writer import sync_now
    try:
        ok = sync_now(agent_id)
    except Exception as e:
        logger.warning("api_kb_sync(%s): sync_now raised exception", agent_id, exc_info=True)
        return jsonify({'error': f'Sync failed: {e}'}), 500
    if not ok:
        logger.warning("api_kb_sync(%s): sync_now returned False", agent_id)
        return jsonify({'error': 'Sync failed'}), 500
    return jsonify({'ok': True})


def _artifacts_dir(agent_id: str) -> str:
    d = os.path.join(WORKSPACE_DIR, agent_id, 'artifacts')
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_to_parent_agent_id(agent_id: str) -> str:
    """If agent_id is a sub-agent, return its parent's ID; otherwise return agent_id."""
    from backend.subagent_manager import subagent_manager
    if subagent_manager.is_subagent(agent_id):
        sub = subagent_manager.get(agent_id)
        if sub:
            return sub.get('parent_id', agent_id)
    return agent_id


# ==================== Agent Artifacts API ====================


@agents_bp.route('/api/agents/<agent_id>/artifacts', methods=['GET'])
def api_list_artifacts(agent_id):
    agent_id = _resolve_to_parent_agent_id(agent_id)
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    artifacts_dir = _artifacts_dir(agent_id)
    if not os.path.isdir(artifacts_dir):
        return jsonify({'files': []})
    
    sort_param = request.args.get('sort', 'newest')
    query = (request.args.get('q', '') or '').strip().lower()
    type_filter = (request.args.get('type', '') or '').strip().lower()
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 24, type=int)
    
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
    
    # Pagination
    total = len(files)
    pages = max(1, -(-total // limit))  # ceil division
    start = (page - 1) * limit
    files = files[start:start + limit]
    
    return jsonify({
        'files': files,
        'total': total,
        'page': page,
        'limit': limit,
        'pages': pages,
    })


@agents_bp.route('/api/agents/<agent_id>/artifacts/<path:filename>', methods=['GET'])
def api_get_artifact(agent_id, filename):
    agent_id = _resolve_to_parent_agent_id(agent_id)
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
    agent_id = _resolve_to_parent_agent_id(agent_id)
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    if not session.get('authenticated'):
        return jsonify({'error': 'Authentication required'}), 401
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(_artifacts_dir(agent_id), filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    os.remove(fpath)
    return jsonify({'success': True})


@agents_bp.route('/api/agents/<agent_id>/artifacts', methods=['POST'])
def api_create_artifact(agent_id):
    agent_id = _resolve_to_parent_agent_id(agent_id)
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

# Generated variant filenames (see _process_avatar)
AVATAR_SMALL_NAME = 'avatar_small.webp'
AVATAR_LARGE_NAME = 'avatar_large.webp'
_AVATAR_SMALL_SIZE = 64   # square, covers 2x DPI of 30-36px displays
_AVATAR_LARGE_MAX = 512   # longest side, for profile / agents page

def _avatar_dir(agent_id: str) -> str:
    d = os.path.join(AVATAR_DIR, agent_id)
    os.makedirs(d, exist_ok=True)
    return d


def _process_avatar(file_storage, avatar_dir: str) -> str:
    """Decode an uploaded image and write compressed WebP variants
    (avatar_small.webp 64x64 center-cropped, avatar_large.webp max 512px).
    Returns the large variant filename. Raises ValueError on undecodable input.
    Animated GIF/WebP uploads keep only their first frame."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file_storage.stream)
        img.load()
        # Apply EXIF orientation before any resize (phone photos)
        img = ImageOps.exif_transpose(img) or img
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as e:
        raise ValueError(f'Invalid or corrupt image file: {e}')

    # Normalize mode: keep alpha (WebP supports it), convert exotic modes to RGB
    if img.mode in ('P', 'PA', 'LA'):
        img = img.convert('RGBA')
    elif img.mode not in ('RGB', 'RGBA', 'L'):
        img = img.convert('RGB')

    small = ImageOps.fit(img, (_AVATAR_SMALL_SIZE, _AVATAR_SMALL_SIZE), Image.LANCZOS)
    small.save(os.path.join(avatar_dir, AVATAR_SMALL_NAME), 'WEBP', quality=82, method=4)

    large = img.copy()
    large.thumbnail((_AVATAR_LARGE_MAX, _AVATAR_LARGE_MAX), Image.LANCZOS)
    large.save(os.path.join(avatar_dir, AVATAR_LARGE_NAME), 'WEBP', quality=82, method=4)

    return AVATAR_LARGE_NAME


def _abs_avatar_path(avatar_path):
    """Resolve a stored avatar_path (may be relative) to an absolute path."""
    if avatar_path and not os.path.isabs(avatar_path):
        return os.path.join(BASE_DIR, avatar_path)
    return avatar_path


@agents_bp.route('/api/agents/<agent_id>/avatar', methods=['GET'])
def api_get_avatar(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    avatar_path = agent.get('avatar_path', '')
    abs_path = _abs_avatar_path(avatar_path)
    if abs_path and os.path.isfile(abs_path):
        # ?size=small serves the compressed sibling variant when it exists;
        # legacy avatars uploaded before variants existed fall back to the
        # original file.
        if request.args.get('size') == 'small':
            small_path = os.path.join(os.path.dirname(abs_path), AVATAR_SMALL_NAME)
            if os.path.isfile(small_path):
                abs_path = small_path
        import mimetypes
        mime, _ = mimetypes.guess_type(abs_path)
        if mime is None:
            mime = 'application/octet-stream'
        from flask import send_file
        # Force download for all stored avatar files to prevent any stored SVG
        # from being rendered as an active document in the browser (XSS defence).
        resp = send_file(abs_path, mimetype=mime, as_attachment=True,
                         download_name=os.path.basename(abs_path),
                         conditional=True)
        # Short freshness window: the browser serves from cache without any
        # request for max-age, sparing avatar refetches on every re-render. After
        # the window it revalidates via the ETag/Last-Modified set by send_file,
        # so a re-uploaded avatar (mtime changes) goes stale within ~5 min.
        # 'private' because the avatar route is auth-gated.
        resp.headers['Cache-Control'] = 'private, max-age=300, must-revalidate'
        return resp
    # Return the default avatar as an inline SVG served from a static string.
    # This SVG is fully controlled server-side and contains no user content.
    default_svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" fill="none">
  <rect width="40" height="40" rx="20" fill="#e0e7ff"/>
  <path d="M20 8a5 5 0 100 10 5 5 0 000-10zm-8 18.5a8 8 0 0116 0" fill="#4f46e5"/>
</svg>'''
    from flask import Response
    return Response(default_svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=3600'})


@agents_bp.route('/api/agents/<agent_id>/avatar', methods=['POST'])
def api_upload_avatar(agent_id):
    # --- Validate extension first (fail-fast, before any DB or disk access) ---
    # SVG is intentionally excluded: SVG files can embed <script> tags and event
    # handlers that execute when the browser renders the file inline, enabling
    # stored XSS.  Only raster formats that cannot carry active content are
    # allowed.  (SEC-1)
    allowed_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed_exts:
        return jsonify({'error': f'Invalid image type. Allowed: {", ".join(sorted(allowed_exts))}'}), 400

    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404

    avatar_dir = _avatar_dir(agent_id)
    # Generate compressed variants first — on failure the old avatar and DB
    # entry stay untouched.
    try:
        fname = _process_avatar(f, avatar_dir)
    except ValueError:
        return jsonify({'error': 'Invalid or corrupt image file'}), 400
    # Remove old legacy avatar file if it isn't one of the variant files
    old_path = agent.get('avatar_path', '')
    old_abs = _abs_avatar_path(old_path)
    if old_abs and os.path.isfile(old_abs) and \
            os.path.basename(old_abs) not in (AVATAR_SMALL_NAME, AVATAR_LARGE_NAME):
        try:
            os.remove(old_abs)
        except OSError:
            pass
    rel_path = os.path.join('shared', 'avatars', agent_id, fname)
    db.update_agent(agent_id, {'avatar_path': rel_path})
    return jsonify({'success': True, 'avatar_path': f'/api/agents/{agent_id}/avatar'})


@agents_bp.route('/api/agents/<agent_id>/avatar', methods=['DELETE'])
def api_delete_avatar(agent_id):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    old_path = agent.get('avatar_path', '')
    old_abs = _abs_avatar_path(old_path)
    if old_abs and os.path.isfile(old_abs):
        try:
            os.remove(old_abs)
        except OSError:
            pass
        # Also remove generated variants alongside the stored file
        for variant in (AVATAR_SMALL_NAME, AVATAR_LARGE_NAME):
            vpath = os.path.join(os.path.dirname(old_abs), variant)
            if os.path.isfile(vpath):
                try:
                    os.remove(vpath)
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
        if ch.get('type') in ('whatsapp', 'whatsapp_shared') and ch['running']:
            instance = channel_manager.get_channel_instance(ch['id'])
            if instance:
                try:
                    ch['bridge_status'] = instance.get_bridge_status().get('status')
                except Exception:
                    ch['bridge_status'] = None
    return jsonify({'channels': channels})


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/debug/listen', methods=['GET'])
def api_channel_debug_listen(agent_id, channel_id):
    """Stream inbound WhatsApp diagnostics only for this agent-owned channel."""
    channel = db.get_channel(channel_id)
    if not channel or channel.get('agent_id') != agent_id:
        return jsonify({'error': 'Channel not found'}), 404
    if channel.get('type') not in ('whatsapp', 'whatsapp_shared'):
        return jsonify({'error': 'Debug listener is only available for WhatsApp channels'}), 400

    from backend.event_stream import event_stream

    event_queue = queue.Queue(maxsize=500)

    def handler(data):
        if data.get('channel_id') != channel_id:
            return
        try:
            event_queue.put_nowait(('whatsapp_inbound', data))
        except queue.Full:
            pass

    event_stream.on('whatsapp_inbound', handler)

    def generate():
        yield (
            'event: connected\n'
            f"data: {json.dumps({'type': 'connected', 'message': 'Listening for WhatsApp inbound messages...'})}\n\n"
        )
        try:
            while True:
                try:
                    event_name, payload = event_queue.get(timeout=30)
                except queue.Empty:
                    yield ': heartbeat\n\n'
                    continue
                yield f'event: {event_name}\ndata: {json.dumps(payload)}\n\n'
        except GeneratorExit:
            pass
        finally:
            event_stream.off('whatsapp_inbound', handler)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


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


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/resume-outbound', methods=['POST'])
def api_resume_whatsapp_outbound(agent_id, channel_id):
    """Clear a live WhatsApp reach-out pause after operator review."""
    from backend.channels.registry import channel_manager
    from backend.channels.whatsapp import WhatsAppChannel

    channel = db.get_channel(channel_id)
    if not channel or channel['agent_id'] != agent_id:
        return jsonify({'error': 'Channel not found for this agent'}), 404
    instance = channel_manager.get_channel_instance(channel_id)
    if not isinstance(instance, WhatsAppChannel) or not instance._dispatcher:
        return jsonify({'error': 'WhatsApp channel not running'}), 409
    instance._dispatcher.resume_after_restriction()
    return jsonify({'success': True})


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
    from backend.channels.whatsapp import WhatsAppChannel
    instance = channel_manager.get_channel_instance(channel_id)
    if not isinstance(instance, WhatsAppChannel):
        return jsonify({'error': 'WhatsApp channel not running'}), 404
    return jsonify(instance.get_qr())


@agents_bp.route('/api/agents/<agent_id>/channels/<channel_id>/bridge-status', methods=['GET'])
def api_whatsapp_bridge_status(agent_id, channel_id):
    """Return Baileys bridge connection status."""
    from backend.channels.registry import channel_manager
    from backend.channels.whatsapp import WhatsAppChannel
    instance = channel_manager.get_channel_instance(channel_id)
    if not isinstance(instance, WhatsAppChannel):
        return jsonify({'status': 'not_running'})
    return jsonify(instance.get_bridge_status())


@agents_bp.route('/api/whatsapp/disconnected-count', methods=['GET'])
def api_whatsapp_disconnected_count():
    """Count running WhatsApp channels whose bridge is not connected."""
    from backend.channels.registry import channel_manager

    def _bridge_down_status(ch):
        if ch.get('type') not in ('whatsapp', 'whatsapp_shared'):
            return None
        if not channel_manager.is_running(ch['id']):
            return None
        instance = channel_manager.get_channel_instance(ch['id'])
        if not instance:
            return None
        try:
            status = instance.get_bridge_status().get('status')
        except Exception:
            return None
        return status if status in ('disconnected', 'qr_pending') else None

    affected = []
    for agent in db.get_agents():
        for ch in db.get_channels(agent['id']):
            status = _bridge_down_status(ch)
            if status:
                affected.append({
                    'id': agent['id'],
                    'name': agent.get('name') or agent['id'],
                    'status': status,
                })
    for ch in db.get_shared_channels():
        status = _bridge_down_status(ch)
        if status:
            affected.append({
                'id': None,
                'name': ch.get('name') or 'Shared Channel',
                'status': status,
            })
    return jsonify({'count': len(affected), 'agents': affected})


@agents_bp.route('/api/channels/whatsapp-bridge/<channel_id>/callback', methods=['POST'])
def api_whatsapp_callback(channel_id):
    """Receive incoming WhatsApp messages from the Baileys sidecar."""
    import hmac
    from backend.channels.registry import channel_manager
    from backend.channels.whatsapp import WhatsAppChannel
    import threading
    instance = channel_manager.get_channel_instance(channel_id)
    if not isinstance(instance, WhatsAppChannel):
        return jsonify({'error': 'Channel not found'}), 404
    # Validate Bearer token set by the sidecar at startup
    auth_header = request.headers.get('Authorization', '')
    expected = f'Bearer {instance._callback_secret}'
    if not hmac.compare_digest(auth_header, expected):
        return jsonify({'error': 'Unauthorized'}), 401
    payload = request.get_json(silent=True) or {}

    def _run_callback():
        # handle_callback runs in a daemon thread — without this wrapper any
        # exception is swallowed by threading's default hook and the message
        # vanishes with no log. Surface it with a full traceback.
        try:
            instance.handle_callback(payload)
        except Exception:
            logging.getLogger('backend.channels.whatsapp').exception(
                "WhatsApp callback handler crashed for channel %s (sender=%s)",
                channel_id, payload.get('from'))

    threading.Thread(target=_run_callback, daemon=True).start()
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
    session_id = db.get_session_id(agent_id, user_id) or db.get_or_create_session(agent_id, user_id)

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

    # Support both JSON and multipart/form-data (including repeated files fields).
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        message = (request.form.get('message') or '').strip()
        user_id = (request.form.get('user_id') or 'anonymous').strip()
        files = [f for f in request.files.getlist('files') if f and f.filename]
        if not files:
            legacy_file = request.files.get('file')
            files = [legacy_file] if legacy_file and legacy_file.filename else []
    else:
        data = request.get_json() or {}
        message = data.get('message', '').strip()
        user_id = data.get('user_id', 'anonymous')
        files = []

    if not message and not files:
        return jsonify({'error': 'Message is required'}), 400

    from backend.agent_runtime import agent_runtime
    from routes.sessions import _process_upload, _ALLOWED_EXTS

    image_urls = []
    attachment_infos = []
    text_prefixes = []

    if files:
        session_id = db.get_or_create_session(agent_id, user_id)
        cfg = db.get_agent_attachment_config(agent_id)
        if not cfg.get('enabled'):
            return jsonify({'error': 'File attachments are not enabled for this agent. Enable them in agent settings.'}), 400

        max_bytes = cfg.get('max_size_mb', 20) * 1024 * 1024
        for file in files:
            ext = os.path.splitext(file.filename or '')[1].lower()
            if ext not in _ALLOWED_EXTS:
                return jsonify({'error': f'File type {ext} not supported'}), 400
            file.seek(0, os.SEEK_END)
            fsize = file.tell()
            file.seek(0)
            if fsize > max_bytes:
                return jsonify({'error': f'File too large (max {cfg.get("max_size_mb", 20)}MB)'}), 400
            try:
                upload = _process_upload(file, agent_id, session_id, user_id, None)
            except Exception as e:
                print(f"[WebChat] Upload processing failed: {e}")
                return jsonify({'error': 'Failed to process uploaded file'}), 500
            attachment_infos.append(upload['attachment_info'])
            if upload['image_url']:
                image_urls.append(upload['image_url'])
            if upload['text_prefix']:
                text_prefixes.append(upload['text_prefix'])

        if text_prefixes:
            message = '\n\n'.join(text_prefixes + ([message] if message else []))
        if not message:
            image_slot = 0
            placeholders = []
            for info in attachment_infos:
                if info.get('is_image'):
                    image_slot += 1
                    placeholders.append(f'[Image #{image_slot}]')
                else:
                    placeholders.append(f'[File: {info["filename"]}]')
            message = ' '.join(placeholders)

    image_url = image_urls[0] if image_urls else None
    attachment_info = attachment_infos[0] if attachment_infos else None
    upload_meta = None
    if attachment_infos:
        upload_meta = {'attachment_infos': attachment_infos}
        if len(attachment_infos) == 1:
            upload_meta['attachment_info'] = attachment_info

    try:
        result = agent_runtime.handle_message(
            agent_id, user_id, message,
            image_url=image_url,
            metadata=upload_meta,
        )
        if result.get('buffered'):
            resp = {'success': True, 'buffered': True}
            if attachment_infos:
                resp['attachment_infos'] = attachment_infos
                if len(attachment_infos) == 1:
                    resp['attachment_info'] = attachment_info
            return jsonify(resp)
        if result.get('injected'):
            resp = {'success': True, 'injected': True}
            if attachment_infos:
                resp['attachment_infos'] = attachment_infos
                if len(attachment_infos) == 1:
                    resp['attachment_info'] = attachment_info
            return jsonify(resp)
        resp = {
            'success': True,
            'response': result['response'],
            'tool_trace': result.get('tool_trace', []),
            'timeline': result.get('timeline', []),
            'slash_command': result.get('slash_command', False),
            'bash_exec': result.get('bash_exec', False),
            'clear_ui': result.get('clear_ui', False),
        }
        if attachment_infos:
            resp['attachment_infos'] = attachment_infos
            if len(attachment_infos) == 1:
                resp['attachment_info'] = attachment_info
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
        session_id = db.get_session_id(agent_id, user_id) or db.get_or_create_session(agent_id, user_id)

    # Sub-agents (including explorers) share the parent agent's chat DB and
    # chatlog files.  chatlog_manager.get() keys by agent_id, so using the
    # sub-agent's own ID would look in the wrong directory and return an
    # empty log — the entries were written under the parent's ID.
    from backend.subagent_manager import subagent_manager
    chatlog_agent_id = agent_id
    _sub = subagent_manager.get(agent_id)
    if _sub:
        chatlog_agent_id = _sub.get('parent_id', agent_id)
    chatlog = chatlog_manager.get(chatlog_agent_id, session_id)

    if after_ts:
        # Forward scan: entries newer than after_ts
        # Guard: after_ts=0 (e.g. fresh page load) would scan the entire file —
        # fall through to tail_by_messages instead.
        all_entries = chatlog.get_entries_after_ts(after_ts, types=_DISPLAY_TYPES)
        entries = all_entries[:limit]
        return jsonify({'entries': entries, 'has_more': len(all_entries) > limit})

    # Backward (tail) scan: entries older than to_ts, counted by logical messages
    entries, has_more = chatlog.tail_by_messages(limit=limit, to_ts=to_ts)
    return jsonify({'entries': entries, 'has_more': has_more})


@agents_bp.route('/api/agents/<agent_id>/chat/history', methods=['GET'])
def api_chat_history(agent_id):
    user_id = request.args.get('user_id', 'anonymous')
    session_id = db.get_session_id(agent_id, user_id) or db.get_or_create_session(agent_id, user_id)
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
    session_id = db.get_session_id(agent_id, user_id) or db.get_or_create_session(agent_id, user_id)
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


def _chat_summary_payload(agent_id: str, user_id: str) -> dict:
    session_id = db.get_session_id(agent_id, user_id) or db.get_or_create_session(agent_id, user_id)
    summary = db.get_summary(session_id, agent_id=agent_id)
    if summary:
        return {'summary': summary['summary'],
                'last_message_id': summary['last_message_id'],
                'message_count': summary['message_count'],
                'updated_at': summary.get('updated_at')}
    return {'summary': None}


@agents_bp.route('/api/agents/<agent_id>/chat/summary', methods=['GET'])
def api_chat_summary(agent_id):
    user_id = request.args.get('user_id', 'anonymous')
    return jsonify(_chat_summary_payload(agent_id, user_id))


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
    loaded_skills = []
    if session_id:
        session_content = db.get_session_state(session_id, agent_id=agent_id)
        session_data = _json.loads(session_content) if session_content else {}
        merged = {**agent_data, **session_data}

        # Resolve loaded skills for this session
        try:
            from backend.agent_runtime import agent_runtime
            from backend.skills_manager import skills_manager
            # Start with skills that have tools (inject_tools)
            seen_skill_ids = set()
            for sk in agent_runtime.get_session_skills(session_id):
                sk_id = sk['skill_id']
                seen_skill_ids.add(sk_id)
                try:
                    name = skills_manager.get_skill_name(sk_id)
                except Exception:
                    name = sk_id  # fallback to skill_id on error
                loaded_skills.append({
                    'skill_id': sk_id,
                    'name': name,
                    'tool_count': sk.get('tool_count', 0),
                })
            # Also include prompt-only skills (system_md only, no inject_tools)
            for sk_id in agent_runtime._session_skill_mds.get(session_id, {}):
                if sk_id not in seen_skill_ids:
                    try:
                        name = skills_manager.get_skill_name(sk_id)
                    except Exception:
                        name = sk_id
                    loaded_skills.append({
                        'skill_id': sk_id,
                        'name': name,
                        'tool_count': 0,
                    })
        except Exception:
            pass
    else:
        merged = agent_data

    # Explorer sub-agents have no DB row — resolve their config from the manager
    # so the model badge reflects the explorer's own (configured) model.
    from backend.subagent_manager import subagent_manager
    from backend.agent_runtime import explorer as _explorer
    _sub_cfg = subagent_manager.get(agent_id)
    _is_explorer = _sub_cfg is not None and _explorer.is_explorer(_sub_cfg)

    def _resolve_active_model():
        fb_id = agent_data.get('active_fallback_model_id')
        if fb_id:
            fb_model = db.get_model_by_id(fb_id)
            return {
                'name': (fb_model or {}).get('name', fb_id),
                'model_name': (fb_model or {}).get('model_name', fb_id),
                'is_fallback': True,
                'id': fb_id,
            }
        if _is_explorer:
            prim_model = _explorer.primary_model(_sub_cfg) or db.get_agent_model(agent_id)
        else:
            prim_model = db.get_agent_model(agent_id)
        if prim_model:
            return {
                'name': prim_model.get('name', 'unknown'),
                'model_name': prim_model.get('model_name', 'unknown'),
                'is_fallback': False,
                'id': prim_model.get('id', ''),
            }
        return None

    if merged:
        state = AgentState.deserialize(_json.dumps(merged))
        payload = {
            'mode': state.mode,
            'tasks': state.tasks,
            'plan_file': state.plan_file,
            'states': state.states,
            'focus': state.focus,
            'focus_reason': state.focus_reason,
            'active_model': _resolve_active_model(),
            'loaded_skills': loaded_skills,
        }
        # CMP session-path map (for the Session State panel's map modal)
        if state.cmp and state.cmp.get('paths'):
            try:
                from backend.agent_runtime.cmp.render import render_map
                from backend.agent_runtime.cmp.compactor import (
                    card_token_estimate, path_llm_token_estimate,
                    path_token_estimate)
                from models.chatlog import chatlog_manager
                agent_row = db.get_agent(agent_id)
                agent_name = (agent_row or {}).get('name') or agent_id.replace('_', ' ').title()
                _cmp_chatlog = chatlog_manager.get(agent_id, session_id)
                _path_cards = []
                for _, p in sorted(state.cmp['paths'].items()):
                    card = {k: p.get(k) for k in
                            ('id', 'title', 'status', 'action', 'goal', 'outcome',
                             'key_facts', 'artifacts', 'depends_on', 'last_active',
                             'state_since', 'tags')}
                    card['tokens'] = path_token_estimate(_cmp_chatlog, p)
                    card['llm_tokens'] = path_llm_token_estimate(_cmp_chatlog, p)
                    card['card_tokens'] = card_token_estimate(p)
                    _path_cards.append(card)
                payload['cmp'] = {
                    'active_id': state.cmp.get('active_id'),
                    'agent_id': agent_id,
                    'agent_name': agent_name,
                    'has_avatar': bool((agent_row or {}).get('avatar_path')),
                    'mermaid': render_map(state.cmp, agent_name),
                    'paths': _path_cards,
                }
            except Exception:
                pass
    elif _is_explorer:
        # Minimal state, but still surface the explorer's model badge.
        payload = {
            'mode': 'execute',
            'active_model': _resolve_active_model(),
            'loaded_skills': loaded_skills,
        }
    else:
        payload = {'mode': None, 'active_model': None, 'loaded_skills': loaded_skills}

    # Background processes tracked for this session (Session State panel).
    if session_id:
        background_processes = []
        try:
            from backend.agent_runtime.background_jobs import (
                background_jobs, refresh_statuses_async)
            jobs = background_jobs.list_for_session(session_id)
            # Nothing else notices an unmonitored process exiting, so a finished
            # job would keep a live row here forever. Probe in the background
            # (throttled) and let the next poll pick up the result.
            if any(j.status == 'running' for j in jobs):
                refresh_statuses_async(
                    session_id,
                    {**(db.get_agent(agent_id) or {}), 'agent_id': agent_id})
            # Only hit the schedules table when there is something to annotate —
            # this endpoint is polled by the browser.
            watched = set()
            if jobs:
                from backend.agent_runtime import monitors
                watched = monitors.monitored_job_ids(agent_id, session_id)
            for j in jobs:
                background_processes.append({
                    'job_id': j.job_id,
                    'command': j.command,
                    'kind': j.kind,
                    'status': j.status,
                    'exit_code': j.exit_code,
                    'started_at': j.started_at,
                    'finished_at': j.finished_at,
                    'log_file': j.log_file,
                    'session_id': j.session_id,
                    'monitored': j.job_id in watched,
                })
        except Exception:
            pass
        payload['background_processes'] = background_processes

    # Context monitor: tokens consumed by the last LLM call vs the model's
    # context window (model.context_window, falling back to the global
    # llm_context_length setting; unknown window → max/percent are null).
    # After session clear, context_usage is gone from session state — fall back
    # to the compiled context token count (system prompt + tool definitions).
    if session_id:
        _cu = merged.get('context_usage') if isinstance(merged, dict) else None
        _used = None
        if _cu and (_cu.get('prompt_tokens') or 0) > 0:
            _used = _cu.get('total_tokens') or (
                (_cu.get('prompt_tokens') or 0) + (_cu.get('completion_tokens') or 0))
        if _used is None:
            # Fallback: compute compiled context token count
            try:
                from backend.agent_runtime import agent_runtime
                _ctx = agent_runtime.get_compiled_context(agent_id, user_id=request.args.get('user_id'))
                _used = _ctx.get('tokens', {}).get('total', 0)
            except Exception:
                pass
        if _used and _used > 0:
            ctx_max = 0
            _am = payload.get('active_model') or {}
            if _am.get('id'):
                _am_row = db.get_model_by_id(_am['id'])
                ctx_max = int((_am_row or {}).get('context_window') or 0)
            if ctx_max <= 0:
                try:
                    ctx_max = int(db.get_setting('llm_context_length', 0) or 0)
                except (TypeError, ValueError):
                    ctx_max = 0
            payload['context_usage'] = {
                'used': _used,
                'max': ctx_max if ctx_max > 0 else None,
                'percent': min(100, round(_used * 100 / ctx_max)) if ctx_max > 0 else None,
            }

    # ?include=summary piggybacks the session summary on this response so the
    # UI gets state + summary in one round trip instead of two requests.
    if request.args.get('include') == 'summary':
        payload['summary'] = _chat_summary_payload(
            agent_id, request.args.get('user_id', 'anonymous'))
    return jsonify(payload)


@agents_bp.route('/api/agents/<agent_id>/skills/<skill_id>/unload', methods=['POST'])
def api_unload_agent_skill(agent_id, skill_id):
    """Unload a lazy-loaded skill from the agent's current session."""
    from backend.agent_runtime import agent_runtime

    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404

    session_id = request.args.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id is required'}), 400

    removed = agent_runtime.remove_session_skill(session_id, skill_id)
    if not removed:
        return jsonify({'result': f'Skill "{skill_id}" was not loaded in this session.'})

    return jsonify({
        'success': True,
        'result': f'Skill "{skill_id}" has been unloaded from the session.'
    })


@agents_bp.route('/api/agents/<agent_id>/plan-file', methods=['GET'])
def api_agent_plan_file(agent_id):
    """Return the rendered plan file content for an agent.

    Reads plan_file path from session state (query param ?session_id=)
    and returns the markdown content from disk.
    """
    import json as _json

    session_id = request.args.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    session_content = db.get_session_state(session_id, agent_id=agent_id)
    session_data = _json.loads(session_content) if session_content else {}
    plan_file = session_data.get('plan_file')

    if not plan_file:
        return jsonify({'error': 'No plan file set', 'plan_file': None}), 404

    from backend.agent_state import AgentState
    state = AgentState(plan_file=plan_file)
    # Read the full file (no LLM-context truncation cap) for the UI viewer.
    content = state._read_plan_file(agent_id, max_chars=None)

    if not content:
        return jsonify({'error': 'Plan file is empty or could not be read', 'plan_file': plan_file}), 404

    return jsonify({'plan_file': plan_file, 'content': content})


@agents_bp.route('/api/agents/<agent_id>/plan-file', methods=['PUT'])
def api_agent_plan_file_update(agent_id):
    """Save edited plan file content back to disk.

    Reads plan_file path from session state (query param ?session_id=)
    and writes the provided content to the file on disk.
    """
    import json as _json
    import os as _os

    session_id = request.args.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    data = request.get_json(silent=True) or {}
    new_content = data.get('content')
    if new_content is None:
        return jsonify({'error': 'content field is required'}), 400

    session_content = db.get_session_state(session_id, agent_id=agent_id)
    session_data = _json.loads(session_content) if session_content else {}
    plan_file = session_data.get('plan_file')

    if not plan_file:
        return jsonify({'error': 'No plan file set', 'plan_file': None}), 404

    # Resolve the file path (same logic as _read_plan_file)
    project_root = _os.path.join(_os.path.dirname(__file__), '..')
    project_root = _os.path.realpath(project_root)

    candidates = []
    agents_dir = _os.path.join(project_root, 'agents')
    agent_plan = _os.path.normpath(
        _os.path.join(agents_dir, agent_id, plan_file)
    )
    agent_root = _os.path.realpath(_os.path.join(agents_dir, agent_id))
    if _os.path.realpath(agent_plan).startswith(agent_root + _os.sep):
        candidates.append(agent_plan)

    legacy_path = _os.path.normpath(_os.path.join(project_root, plan_file))
    if legacy_path.startswith(project_root):
        candidates.append(legacy_path)

    if not candidates:
        return jsonify({'error': 'Plan file path rejected: outside allowed directories'}), 403

    # Write to the first existing candidate, or the first one if none exist
    written = False
    last_error = None
    for path in candidates:
        try:
            # Create parent directories if needed
            parent = _os.path.dirname(path)
            if parent:
                _os.makedirs(parent, exist_ok=True)
            # Write only text content (not full Markdown/HTML rendered version)
            # Ensure content is a plain string
            text_content = str(new_content) if not isinstance(new_content, str) else new_content
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text_content)
            written = True
            break
        except Exception as e:
            last_error = str(e)
            # Try next candidate

    if not written:
        return jsonify({'error': f'Failed to write plan file: {last_error}'}), 500

    return jsonify({'success': True, 'plan_file': plan_file})


@agents_bp.route('/api/agents/<agent_id>/bg-job/log', methods=['GET'])
def api_bg_job_log(agent_id):
    """Read log output for a background job.

    Query params:
        file      - log file path (must start with /tmp/evonic_build_)
        session_id - execution session owning the log file
        lines     - number of lines to return (default 200, max 2000)
        direction - 'tail' (default) or 'head'
    """
    log_path = (request.args.get('file') or '').strip()
    if not log_path.startswith('/tmp/evonic_build_') or '..' in log_path:
        return jsonify({'error': 'Invalid log file path'}), 400

    real = os.path.realpath(log_path)
    if not real.startswith('/tmp/evonic_build_'):
        return jsonify({'error': 'Invalid log file path'}), 400

    try:
        lines_count = min(max(int(request.args.get('lines', 200)), 1), 2000)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid lines value'}), 400
    direction = request.args.get('direction', 'tail')
    if direction not in ('tail', 'head'):
        return jsonify({'error': 'Invalid direction'}), 400

    # Guard-wrapper logs normally live inside the execution sandbox.  Reading
    # the host /tmp only works for local jobs, so use the same session backend
    # that launched the process when a session id is supplied.
    if not os.path.isfile(real):
        session_id = (request.args.get('session_id') or '').strip()
        if not session_id:
            return jsonify({'error': 'session_id is required for sandbox logs'}), 400
        try:
            from backend.agent_runtime.background_jobs import background_jobs
            job = next((j for j in background_jobs.list_for_session(session_id)
                        if j.log_file == real), None)
            if not job:
                return jsonify({'error': 'Log file not found'}), 404
            agent = db.get_agent(agent_id)
            if not agent:
                return jsonify({'error': 'Agent not found'}), 404
            from backend.tools.lib.exec_backend import registry
            backend = registry.get_backend(session_id, {**agent, 'agent_id': agent_id})
            command = f"{direction} -n {lines_count} -- {shlex.quote(real)}"
            result = backend.run_bash(command, 15, {})
            if result.get('exit_code') != 0:
                return jsonify({'error': 'Log file not found'}), 404
            content = (result.get('stdout') or '').splitlines()
            return jsonify({'content': content, 'file_size': len(result.get('stdout') or ''),
                            'total_lines': len(content)})
        except Exception as e:
            logger.warning('Failed to read sandbox background log: %s', e)
            return jsonify({'error': 'Unable to read background log'}), 502

    try:
        file_size = os.path.getsize(real)
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as f:
            if direction == 'tail':
                avg_line = 120
                seek_pos = max(0, file_size - lines_count * avg_line)
                if seek_pos > 0:
                    f.seek(seek_pos)
                    f.readline()
                all_lines = f.readlines()
                content = [l.rstrip('\n\r') for l in all_lines[-lines_count:]]
            else:
                content = []
                for i, line in enumerate(f):
                    if i >= lines_count:
                        break
                    content.append(line.rstrip('\n\r'))

        return jsonify({
            'content': content,
            'file_size': file_size,
            'total_lines': len(content),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
    session_id = db.get_session_id(agent_id, user_id) or db.get_or_create_session(agent_id, user_id)
    return jsonify({'session_id': session_id})


@agents_bp.route('/api/agents/<agent_id>/chat/stream', methods=['GET'])
def api_chat_stream(agent_id):
    """SSE endpoint — pushes live thinking/tool events for a session to the browser.

    Still the active chat transport: the chat UI bundle has not been migrated to the
    unified GET /api/realtime/stream?chat=1 (the unified chat path needs its own
    seq-correctness work first). Kept intentionally; do not remove."""
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    from backend.event_stream import event_stream

    # Release the thread-local DB connection acquired by enforce_auth.
    # This SSE thread will block for 30+s; without close() it leaks an FD.
    db.close()

    # SSE connection limiting — max 5 concurrent per user/IP (FINDING-004)
    from flask import session as _flask_session
    from models.api_rate_limit import sse_register, sse_unregister, SSE_MAX_CONCURRENT
    _sse_id = (
        f"user:{_flask_session.get('_user_id', 'admin')}"
        if _flask_session.get('authenticated')
        else f"ip:{request.remote_addr or '0.0.0.0'}"
    )
    _sse_allowed, _sse_count = sse_register(_sse_id)
    if not _sse_allowed:
        return jsonify({
            'error': 'too_many_sse_connections',
            'message': f'Maximum {SSE_MAX_CONCURRENT} concurrent SSE connections allowed.',
            'retry_after': 30,
        }), 429, {'Retry-After': '30'}

    q = queue.Queue(maxsize=200)

    _SENTINEL = object()

    def _make_handler(sse_event_name, transform):
        def handler(data):
            if data.get('session_id') != session_id:
                return
            try:
                payload = transform(data)
                if payload is not None:
                    payload['seq'] = data.get('_chat_seq')
                    q.put_nowait((sse_event_name, payload, data.get('_chat_seq')))
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
        'state:changed':      ('state:changed',    lambda d: {
            key: d[key] for key in ('mode', 'plan_file', 'tasks', 'loaded_skills')
            if key in d
        }),
        'tasks:auto_transition': ('tasks:auto_transition', lambda d: {
            key: d[key] for key in ('task_ids', 'tasks') if key in d
        }),
        'tasks:stale': ('tasks:stale', lambda d: {
            key: d[key] for key in ('task_ids', 'tasks') if key in d
        }),
        'llm_response_chunk': ('response_chunk',  lambda d: {
            'content': d.get('content', ''),
            'is_final': d.get('is_final', False),
            'send_as_message': d.get('send_as_message', False),
        }),
        'turn_complete':      ('done',             lambda d: {
            'thinking_duration': d.get('thinking_duration'),
            'response': d.get('response', ''),
            'slash_command': d.get('slash_command', False),
        }),
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
        'message_received': ('message_received', lambda d: {
            'message': d.get('message', ''),
            'metadata': d.get('metadata', {}),
        }),
        'session_clear': ('session_clear', lambda d: {
            'session_id': d.get('session_id', ''),
            'agent_id': d.get('agent_id', ''),
        }),
        'turn_split': ('turn_split', lambda d: {}),
        'evonic:agent-state-changed': ('state_changed', lambda d: {
            'agent_id': d.get('agent_id', ''),
            'session_id': d.get('session_id', ''),
        }),
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
            payload['seq'] = entry['chat_seq']
            try:
                q.put_nowait((sse_name, payload, entry['chat_seq']))
            except queue.Full:
                break

    def generate():
        try:
            while True:
                try:
                    item = q.get(timeout=30)
                except queue.Empty:
                    # No events for 30s — send a heartbeat as a real SSE event so the
                    # client can detect it (EventSource ignores comment-only lines).
                    yield "event: heartbeat\ndata: {}\n\n"
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
            # Unregister SSE connection (FINDING-004)
            sse_unregister(_sse_id)

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
    to every connected client.
    DEPRECATED: Use unified GET /api/realtime/stream?channels=approvals instead."""
    import logging as _log_depr
    _log_depr.getLogger(__name__).warning(
        "DEPRECATED endpoint /api/approvals/stream used — "
        "migrate to /api/realtime/stream?channels=approvals")
    from backend.event_stream import event_stream

    # Release the thread-local DB connection acquired by enforce_auth.
    db.close()

    # SSE connection limiting — max 5 concurrent per user/IP (FINDING-004)
    from flask import session as _flask_session
    from models.api_rate_limit import sse_register, sse_unregister, SSE_MAX_CONCURRENT
    _sse_id = (
        f"user:{_flask_session.get('_user_id', 'admin')}"
        if _flask_session.get('authenticated')
        else f"ip:{request.remote_addr or '0.0.0.0'}"
    )
    _sse_allowed, _sse_count = sse_register(_sse_id)
    if not _sse_allowed:
        return jsonify({
            'error': 'too_many_sse_connections',
            'message': f'Maximum {SSE_MAX_CONCURRENT} concurrent SSE connections allowed.',
            'retry_after': 30,
        }), 429, {'Retry-After': '30'}

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
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                sse_event, payload, seq = item
                id_line = f"id: {seq}\n" if seq is not None else ''
                yield f"{id_line}event: {sse_event}\ndata: {json.dumps(payload)}\n\n"
        finally:
            for event_name, handler in handlers.items():
                event_stream.off(event_name, handler)
            # Unregister SSE connection (FINDING-004)
            sse_unregister(_sse_id)

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
        'state:changed':     ('state:changed',    lambda d: {key: d[key] for key in ('mode', 'plan_file', 'tasks', 'loaded_skills') if key in d}),
        'tasks:auto_transition': ('tasks:auto_transition', lambda d: {key: d[key] for key in ('task_ids', 'tasks') if key in d}),
        'tasks:stale':       ('tasks:stale',       lambda d: {key: d[key] for key in ('task_ids', 'tasks') if key in d}),
        'llm_response_chunk':('response_chunk',  lambda d: {'content': d.get('content', ''), 'is_final': d.get('is_final', False), 'send_as_message': d.get('send_as_message', False)}),
        'turn_complete':     ('done',             lambda d: {
            'thinking_duration': d.get('thinking_duration'),
            'response': d.get('response', ''),
            'slash_command': d.get('slash_command', False),
        }),
        'approval_required': ('approval_required', lambda d: {'approval_id': d.get('approval_id', ''), 'agent_id': d.get('agent_id', ''), 'source_agent_id': d.get('source_agent_id', ''), 'source_agent_name': d.get('source_agent_name', ''), 'tool': d.get('tool_name', ''), 'args': d.get('tool_args', {}), 'approval_info': d.get('approval_info', {}), 'reasons': d.get('reasons', []), 'score': d.get('score')}),
        'approval_resolved': ('approval_resolved', lambda d: {'approval_id': d.get('approval_id', ''), 'decision': d.get('decision', ''), 'timed_out': d.get('timed_out', False)}),
        'llm_retry':         ('retry',             lambda d: {'retry_count': d.get('retry_count', 0), 'max_retries': d.get('max_retries', 0), 'error_type': d.get('error_type', ''), 'message': d.get('user_message', '')}),
        'message_injected':  ('message_injected',  lambda d: {'message': d.get('message', '')}),
        'message_injection_applied': ('message_injection_applied', lambda d: {'content': d.get('content', ''), 'count': d.get('count', 1)}),
        'message_received':  ('message_received', lambda d: {'message': d.get('message', ''), 'metadata': d.get('metadata', {})}),
        'session_clear':     ('session_clear',     lambda d: {'session_id': d.get('session_id', ''), 'agent_id': d.get('agent_id', '')}),
        'turn_split':        ('turn_split',        lambda d: {}),
        'evonic:agent-state-changed': ('state_changed', lambda d: {'agent_id': d.get('agent_id', ''), 'session_id': d.get('session_id', '')}),
    }

    if up_to_seq is None:
        raw = event_stream.get_session_events(session_id, after_seq)
    else:
        raw = event_stream.get_events_in_range(session_id, after_seq, up_to_seq)

    # Strip boundary events (turn_complete, session_clear) on fresh requests so
    # restoreActiveReasoning() never replays completed turns or past session_clear
    # events that would create a stale thinking bubble. Mirror the SSE stream logic
    # at lines 1668-1674. Only strip when after_seq==0; on gap-fill reconnects
    # (after_seq>0), the client hasn't seen these events and needs them.
    if after_seq == 0:
        last_boundary = -1
        for i, e in enumerate(raw):
            if e['event'] in ('turn_complete', 'session_clear'):
                last_boundary = i
        if last_boundary >= 0:
            raw = raw[last_boundary + 1:]

    events = []
    for entry in raw:
        event_name = entry['event']
        if event_name in _TRANSFORM_MAP:
            sse_name, transform = _TRANSFORM_MAP[event_name]
            payload = transform(entry['data'])
            payload['seq'] = entry['chat_seq']
            events.append({'event': sse_name, 'seq': entry['chat_seq'], 'data': payload})

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


@agents_bp.route('/api/approvals/pending', methods=['GET'])
def api_approvals_pending():
    """Return tool approvals currently blocking an agent (in-memory registry).

    Pull-based safety-net for the web approval modal: the SSE 'approval_required'
    push is best-effort and can be lost in transit, so the UI polls this endpoint
    (while a tab is visible and an agent is busy) to guarantee the modal appears.
    """
    from backend.agent_runtime.approval import approval_registry
    return jsonify({'pending': approval_registry.list_pending()})


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
    """SSE endpoint — pushes real-time agent busy/idle status changes and
    turn-complete notifications to every connected client.
    DEPRECATED: Use unified GET /api/realtime/stream?channels=status instead.

    Events:
        event: agent_busy_changed
        data: {"agent_id": "...", "busy": true|false, "session_id": "..."}

        event: agent_turn_complete
        data: {"agent_id": "...", "agent_name": "...", "response": "...",
               "external_user_id": "...", "session_id": "..."}
    """
    import logging as _log_depr
    _log_depr.getLogger(__name__).warning(
        "DEPRECATED endpoint /api/agents/status/stream used — "
        "migrate to /api/realtime/stream?channels=status")
    import queue as _queue
    from backend.event_stream import event_stream

    # Release the thread-local DB connection acquired by enforce_auth.
    db.close()

    # SSE connection limiting (max 5 concurrent per user/IP, FINDING-004)
    from flask import session as _flsk_sess
    from models.api_rate_limit import sse_register, sse_unregister, SSE_MAX_CONCURRENT
    _sse_ident = (
        'user:' + (_flsk_sess.get('_user_id', 'admin') if _flsk_sess.get('authenticated') else '')
        if _flsk_sess.get('authenticated')
        else 'ip:' + (request.remote_addr or '0.0.0.0')
    )
    _ok, _cnt = sse_register(_sse_ident)
    if not _ok:
        return jsonify({
            'error': 'too_many_sse_connections',
            'message': 'Maximum ' + str(SSE_MAX_CONCURRENT) + ' concurrent SSE connections allowed.',
            'retry_after': 30,
        }), 429, {'Retry-After': '30'}

    q = _queue.Queue(maxsize=200)

    def busy_handler(data):
        try:
            payload = {
                'agent_id': data.get('agent_id', ''),
                'busy': data.get('busy', False),
                'session_id': data.get('session_id', ''),
            }
            q.put_nowait(('busy', payload))
        except _queue.Full:
            pass

    def turn_handler(data):
        try:
            response = data.get('response', '')
            if not response or data.get('is_error'):
                return
            payload = {
                'agent_id': data.get('agent_id', ''),
                'agent_name': data.get('agent_name', ''),
                'response': response,
                'session_id': data.get('session_id', ''),
                'external_user_id': data.get('external_user_id', ''),
            }
            q.put_nowait(('turn', payload))
        except _queue.Full:
            pass

    event_stream.on('agent_busy_changed', busy_handler)
    event_stream.on('turn_complete', turn_handler)

    def generate():
        try:
            while True:
                try:
                    kind, payload = q.get(timeout=30)
                except _queue.Empty:
                    yield 'event: heartbeat\ndata: {}\n\n'
                    continue
                if kind == 'busy':
                    yield f'event: agent_busy_changed\ndata: {json.dumps(payload)}\n\n'
                elif kind == 'turn':
                    yield f'event: agent_turn_complete\ndata: {json.dumps(payload)}\n\n'
        finally:
            event_stream.off('agent_busy_changed', busy_handler)
            event_stream.off('turn_complete', turn_handler)
            sse_unregister(_sse_ident)

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
