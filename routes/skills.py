"""Skills management routes — list, upload, toggle, delete skills."""

import ipaddress
import os
import secrets
import tempfile
import zipfile
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, send_file, after_this_request
from backend.skills_manager import skills_manager
from backend.skillsets import list_skillsets, get_skillset, resolve_skillset, apply_skillset, update_skillset
from backend.audit_logger import audit
from backend.zip_validator import validate_upload_zip, MAX_UPLOAD_BYTES

skills_bp = Blueprint('skills', __name__)


@skills_bp.route('/skills')
def skills_page():
    """Skills management page."""
    return render_template('skills.html')


@skills_bp.route('/api/skills')
def api_list_skills():
    """List all installed skills."""
    skills = skills_manager.list_skills()
    # Remove internal fields
    for s in skills:
        s.pop('_dir', None)
    return jsonify({'skills': skills})


@skills_bp.route('/skills/<skill_id>')
def skill_detail_page(skill_id):
    """Skill detail page with settings and tools."""
    skill = skills_manager.get_skill(skill_id)
    if not skill:
        return redirect('/skills')
    return render_template('skill_detail.html', skill_id=skill_id)


@skills_bp.route('/api/skills/<skill_id>')
def api_get_skill(skill_id):
    """Get a single skill's details and tool list."""
    skill = skills_manager.get_skill(skill_id)
    if not skill:
        return jsonify({'error': 'Skill not found'}), 404
    skill.pop('_dir', None)
    return jsonify(skill)


@skills_bp.route('/api/skills/upload', methods=['POST'])
def api_upload_skill():
    """Upload and install a skill from a zip file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename or not file.filename.endswith('.zip'):
        return jsonify({'error': 'File must be a .zip'}), 400

    # --- Size check via Content-Length before reading ---
    content_length = request.content_length
    if content_length and content_length > MAX_UPLOAD_BYTES:
        size_mb = content_length / 1024 / 1024
        max_mb = MAX_UPLOAD_BYTES // 1024 // 1024
        return jsonify({'error': f'Upload too large ({size_mb:.1f} MB). Maximum is {max_mb} MB.'}), 413

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        # --- Validate zip content before extraction ---
        ok, err = validate_upload_zip(tmp_path, expected_filename=file.filename)
        if not ok:
            return jsonify({'error': err}), 400

        force = request.form.get('force', '').lower() in ('true', '1', 'yes')
        result = skills_manager.install_skill(tmp_path, force=force)
        if 'error' in result:
            status = 409 if 'already installed' in result['error'] else 400
            return jsonify(result), status
        result.pop('_dir', None)
        installed_id = result.get('id', '')
        if installed_id:
            audit.log_skill(user_id='admin', skill_id=installed_id, action='install', ip=request.remote_addr or '')
        return jsonify({'success': True, 'skill': result})
    finally:
        os.unlink(tmp_path)


@skills_bp.route('/api/skills/<skill_id>/toggle', methods=['PUT'])
def api_toggle_skill(skill_id):
    """Toggle a skill's enabled/disabled state."""
    data = request.get_json()
    enabled = data.get('enabled', True)
    result = skills_manager.set_skill_enabled(skill_id, enabled)
    if 'error' in result:
        return jsonify(result), 400
    result.pop('_dir', None)
    return jsonify({'success': True, 'skill': result})


@skills_bp.route('/api/skills/<skill_id>/config', methods=['GET'])
def api_get_skill_config(skill_id):
    """Get skill variables schema and current config values."""
    variables = skills_manager.get_skill_variables(skill_id)
    config = skills_manager.get_skill_config(skill_id)
    return jsonify({'variables': variables, 'config': config})


@skills_bp.route('/api/skills/<skill_id>/config', methods=['PUT'])
def api_set_skill_config(skill_id):
    """Save skill config values."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    result = skills_manager.set_skill_config(skill_id, data)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@skills_bp.route('/api/internal/skills/muktamar-agent/verify-token', methods=['POST'])
def api_verify_muktamar_registration_token():
    """Verify the registration credential directly against skill config storage."""
    try:
        source = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return jsonify({'valid': False}), 403
    if not source.is_loopback:
        return jsonify({'valid': False}), 403

    supplied = request.headers.get('X-API-Token', '')
    expected = str(
        skills_manager.get_skill_config('muktamar-agent').get('REGISTRATION_API_TOKEN') or ''
    ).strip()
    valid = bool(supplied and expected and secrets.compare_digest(supplied, expected))
    return jsonify({'valid': valid}), 200 if valid else 401


@skills_bp.route('/api/skills/<skill_id>/system-prompt', methods=['GET'])
def api_get_skill_system_prompt(skill_id):
    """Get the SYSTEM.md content for a skill."""
    import os
    skill_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'skills', skill_id)
    system_md = os.path.join(skill_dir, 'SYSTEM.md')
    if not os.path.isfile(system_md):
        return jsonify({'error': 'No SYSTEM.md found for this skill', 'has_system_md': False})
    with open(system_md) as f:
        content = f.read()
    return jsonify({'has_system_md': True, 'content': content})


@skills_bp.route('/api/skills/<skill_id>/system-prompt', methods=['PUT'])
def api_set_skill_system_prompt(skill_id):
    """Save the SYSTEM.md content for a skill."""
    import os
    data = request.get_json()
    if not data or 'content' not in data:
        return jsonify({'error': 'No content provided'}), 400
    skill_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'skills', skill_id)
    system_md = os.path.join(skill_dir, 'SYSTEM.md')
    if not os.path.isfile(system_md):
        return jsonify({'error': 'No SYSTEM.md found for this skill'}), 404
    with open(system_md, 'w') as f:
        f.write(data['content'])
    return jsonify({'success': True})


@skills_bp.route('/api/skills/<skill_id>/tools')
def api_get_skill_tools(skill_id):
    """Return full OpenAI-schema tool definitions for a skill (including parameters).
    Works regardless of whether the skill is enabled or disabled.
    """
    skill = skills_manager.get_skill(skill_id)
    if not skill:
        return jsonify({'error': 'Skill not found'}), 404
    skill_dir = skill.get('_dir', '')
    tool_defs = skills_manager._load_tool_defs(skill_dir, skill)
    return jsonify({'tools': tool_defs})


@skills_bp.route('/api/skills/<skill_id>/tools/test', methods=['POST'])
def api_test_skill_tool(skill_id):
    """Execute a skill tool with caller-supplied args for ad-hoc testing."""
    data = request.get_json() or {}
    tool_name = data.get('tool_name', '')
    args = data.get('args', {})
    if not tool_name:
        return jsonify({'error': 'tool_name is required'}), 400

    from backend.tools import tool_registry
    ctx = {
        'agent_id': '__test__',
        'agent_name': 'Test',
        'session_id': '__test__',
        'assigned_tool_ids': [f'skill:{skill_id}:{tool_name}'],
    }
    executor = tool_registry.get_real_executor(ctx)
    try:
        result = executor(tool_name, args)
        return jsonify({'result': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@skills_bp.route('/api/skills/<skill_id>', methods=['DELETE'])
def api_delete_skill(skill_id):
    """Uninstall and delete a skill."""
    result = skills_manager.uninstall_skill(skill_id)
    if 'error' in result:
        return jsonify(result), 400
    audit.log_skill(user_id='admin', skill_id=skill_id, action='uninstall', ip=request.remote_addr or '')
    return jsonify(result)


@skills_bp.route('/api/skills/<skill_id>/export')
def api_export_skill(skill_id):
    """Export a skill as a zip file, matching the CLI `evonic skill export <ID>` behavior."""
    skill = skills_manager.get_skill(skill_id)
    if not skill:
        return jsonify({'error': 'Skill not found'}), 404

    skill_dir = skill.get('_dir')
    if not skill_dir or not os.path.isdir(skill_dir):
        return jsonify({'error': 'Skill directory not found'}), 404

    # Create zip archive in a temp file
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(skill_dir):
                dirs[:] = [d for d in dirs if d != '__pycache__']
                for fname in files:
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, skill_dir)
                    arcname = f'{skill_id}/{rel_path}'
                    zf.write(full_path, arcname)

        @after_this_request
        def cleanup(response):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return response

        version = skill.get('version', '0.1.0')
        return send_file(
            tmp_path,
            as_attachment=True,
            download_name=f'{skill_id}-evonic-plugin-v{version}.zip',
            mimetype='application/zip',
        )
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return jsonify({'error': 'Failed to create export archive'}), 500


# ==================== Skillset Routes ====================

@skills_bp.route('/api/skillsets')
def api_list_skillsets():
    """List all available skillsets."""
    skillsets = list_skillsets()
    return jsonify({'skillsets': skillsets})


@skills_bp.route('/api/skillsets/<skill_id>')
def api_get_skillset(skill_id):
    """Get a single skillset's details."""
    skillset = get_skillset(skill_id)
    if not skillset:
        return jsonify({'error': 'Skillset not found'}), 404
    return jsonify(skillset)


@skills_bp.route('/api/skillsets/<skill_id>/resolve')
def api_resolve_skillset(skill_id):
    """Resolve a skillset's tool names to actual available tool IDs."""
    resolved = resolve_skillset(skill_id)
    if not resolved:
        return jsonify({'error': 'Skillset not found'}), 404
    return jsonify(resolved)


@skills_bp.route('/api/skillsets/<skill_id>/apply', methods=['POST'])
def api_apply_skillset(skill_id):
    """Apply a skillset template to create a new agent."""
    from models.db import db
    import shutil

    agent_data = request.get_json() or {}
    if not agent_data.get('id'):
        return jsonify({'error': 'Agent ID is required.'}), 400

    result = apply_skillset(skill_id, agent_data)
    if 'error' in result:
        return jsonify(result), 404

    # Check if agent already exists
    if db.get_agent(result['id']):
        return jsonify({'error': f"Agent ID '{result['id']}' already exists."}), 409

    # Create the agent
    try:
        agents_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'agents')
        agent_dir = os.path.join(agents_dir, result['id'])
        kb_dir = os.path.join(agent_dir, 'kb')
        os.makedirs(kb_dir, exist_ok=True)

        # Write system prompt
        system_prompt_path = os.path.join(agent_dir, 'SYSTEM.md')
        with open(system_prompt_path, 'w', encoding='utf-8') as f:
            f.write(result.get('system_prompt', ''))

        # Create workspace directory at shared/agents/[agent-id]
        workspace_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'shared', 'agents', result['id'])
        os.makedirs(workspace_dir, exist_ok=True)

        # Create agent in DB
        db.create_agent({
            'id': result['id'],
            'name': result['name'],
            'description': result.get('description', ''),
            'system_prompt': result.get('system_prompt', ''),
            'model': result.get('model'),
            'workspace': workspace_dir,
        })

        # Assign tools
        tools = result.get('tools', [])
        if tools:
            db.set_agent_tools(result['id'], tools)

        # Apply skills
        for skill_name in result.get('skills', []):
            skills_manager.set_skill_enabled(skill_name, True)

        # Copy KB files
        for fname, content in result.get('kb_files', {}).items():
            kb_file_path = os.path.join(kb_dir, fname)
            with open(kb_file_path, 'w', encoding='utf-8') as f:
                f.write(content)

        return jsonify({
            'success': True,
            'agent_id': result['id'],
            'message': f"Agent '{result['name']}' created from skillset '{skill_id}'."
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@skills_bp.route('/api/skillsets/<skill_id>', methods=['PUT'])
def api_update_skillset(skill_id):
    """Update a skillset's configuration."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    # Don't allow changing the id
    data.pop('id', None)
    result = update_skillset(skill_id, data)
    if 'error' in result:
        return jsonify(result), 404
    return jsonify(result)


@skills_bp.route('/skillset/<skill_id>')
def edit_skillset_page(skill_id):
    """Edit skillset page."""
    skillset = get_skillset(skill_id)
    if not skillset:
        return redirect('/skills')
    return render_template('edit_skillset.html', skillset=skillset)


@skills_bp.route('/skills/edit/<skill_id>')
def edit_skillset_page_legacy(skill_id):
    """Legacy redirect for old edit URL."""
    return redirect(f'/skillset/{skill_id}')
