from pathlib import Path

"""Plugin management routes — list, upload, toggle, configure, delete plugins."""

import io
import json
import os
import re
import tempfile
import zipfile
from flask import Blueprint, render_template, jsonify, request, redirect, send_file
from backend.plugin_manager import plugin_manager
from backend.plugin_lifecycle import PLUGINS_DIR
from backend.audit_logger import audit
from backend.zip_validator import validate_upload_zip, MAX_UPLOAD_BYTES

plugins_bp = Blueprint('plugins', __name__)


@plugins_bp.route('/plugins')
def plugins_page():
    """Plugins management page."""
    return render_template('plugins.html')


@plugins_bp.route('/api/plugins')
def api_list_plugins():
    """List all installed plugins."""
    plugins = plugin_manager.list_plugins()
    for p in plugins:
        p.pop('_dir', None)
    return jsonify({'plugins': plugins})


@plugins_bp.route('/plugins/<plugin_id>')
def plugin_detail_page(plugin_id):
    """Plugin detail page with settings and events."""
    plugin = plugin_manager.get_plugin(plugin_id)
    if not plugin:
        return redirect('/plugins')
    plugin_template_dir = Path(PLUGINS_DIR) / plugin_id / 'templates'
    widget_files = sorted([f.name for f in plugin_template_dir.glob('*_widget.html')]) if plugin_template_dir.exists() else []
    # Plugin-provided extra tabs: any `<slug>_tab.html` becomes a tab whose id
    # is the slug and whose label is the slug in Title Case
    # (e.g. `manage_tab.html` -> id "manage", label "Manage"). The partial is
    # included into the detail page's tab area; it may lazily initialise itself
    # by defining `window.tabInit_<slug>()`.
    tab_files = []
    if plugin_template_dir.exists():
        for path in sorted(plugin_template_dir.glob('*_tab.html')):
            slug = path.name[: -len('_tab.html')]
            if not re.fullmatch(r'[a-z0-9_-]+', slug):
                continue
            tab_files.append({
                'id': slug,
                'label': slug.replace('_', ' ').replace('-', ' ').title(),
                'file': path.name,
            })

    # Compute model_agent_map for agentapi plugin token widget
    # Embed server-side so the widget JS doesn't need an async fetch
    model_agent_map = {}
    if plugin_id == 'agentapi':
        cfg = plugin_manager.get_plugin_config('agentapi')
        raw = cfg.get('MODEL_AGENT_MAP', '{}')
        try:
            model_agent_map = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            model_agent_map = {}

    return render_template('plugin_detail.html',
                           plugin_id=plugin_id,
                           widgets=widget_files,
                           tabs=tab_files,
                           model_agent_map=model_agent_map)


@plugins_bp.route('/api/plugins/<plugin_id>')
def api_get_plugin(plugin_id):
    """Get a single plugin's details, events, and config."""
    plugin = plugin_manager.get_plugin(plugin_id)
    if not plugin:
        return jsonify({'error': 'Plugin not found'}), 404
    plugin.pop('_dir', None)
    return jsonify(plugin)


@plugins_bp.route('/api/plugins/upload', methods=['POST'])
def api_upload_plugin():
    """Upload and install a plugin from a zip file."""
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

    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        # --- Validate zip content before extraction ---
        ok, err = validate_upload_zip(tmp_path, expected_filename=file.filename)
        if not ok:
            return jsonify({'error': err}), 400

        force = request.form.get('force', '').lower() in ('true', '1', 'yes')
        result = plugin_manager.install_plugin(tmp_path, force=force)
        if 'error' in result:
            status = 409 if 'already installed' in result['error'] else 400
            return jsonify(result), status
        result.pop('_dir', None)
        installed_id = result.get('id', '')
        if installed_id:
            audit.log_plugin(user_id='admin', plugin_id=installed_id, action='install', ip=request.remote_addr or '')
        return jsonify({'success': True, 'plugin': result})
    finally:
        os.unlink(tmp_path)


@plugins_bp.route('/api/plugins/<plugin_id>/toggle', methods=['PUT'])
def api_toggle_plugin(plugin_id):
    """Toggle a plugin's enabled/disabled state."""
    data = request.get_json()
    enabled = data.get('enabled', True)
    result = plugin_manager.set_plugin_enabled(plugin_id, enabled)
    if 'error' in result:
        return jsonify(result), 400
    result.pop('_dir', None)
    return jsonify({'success': True, 'plugin': result})


@plugins_bp.route('/api/plugins/<plugin_id>/config', methods=['GET'])
def api_get_plugin_config(plugin_id):
    """Get plugin variables schema and current config values."""
    variables = plugin_manager.get_plugin_variables(plugin_id)
    config = plugin_manager.get_plugin_config(plugin_id)
    return jsonify({'variables': variables, 'config': config})


@plugins_bp.route('/api/plugins/<plugin_id>/config', methods=['PUT'])
def api_set_plugin_config(plugin_id):
    """Save plugin config values."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    result = plugin_manager.set_plugin_config(plugin_id, data)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@plugins_bp.route('/api/plugins/<plugin_id>/logs')
def api_get_plugin_logs(plugin_id):
    """Get plugin log entries."""
    limit = min(request.args.get('limit', 200, type=int), 1000)
    since = request.args.get('since', None)
    logs = plugin_manager.get_logs(plugin_id, limit=limit, since=since)
    return jsonify({'logs': logs})


@plugins_bp.route('/api/plugins/<plugin_id>/logs', methods=['DELETE'])
def api_clear_plugin_logs(plugin_id):
    """Clear all log entries for a plugin."""
    plugin_manager.clear_logs(plugin_id)
    return jsonify({'success': True})


@plugins_bp.route('/api/plugins/<plugin_id>/export')
def api_export_plugin(plugin_id):
    """Export a plugin as a downloadable ZIP file."""
    # Validate plugin_id
    if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
        return jsonify({'error': 'Invalid plugin id'}), 400

    plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
    if not os.path.isdir(plugin_dir):
        return jsonify({'error': f'Plugin not found: {plugin_id}'}), 404

    manifest_path = os.path.join(plugin_dir, 'plugin.json')
    if not os.path.isfile(manifest_path):
        return jsonify({'error': f'No plugin.json found for: {plugin_id}'}), 400

    # Build in-memory ZIP
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(plugin_dir):
                # Skip __pycache__ directories
                dirs[:] = [d for d in dirs if d != '__pycache__']

                for filename in files:
                    # Skip .pyc files (compiled bytecode) and dot-files (VCS/editor artifacts)
                    if filename.endswith('.pyc') or filename.startswith('.'):
                        continue
                    file_path = os.path.join(root, filename)
                    arcname = os.path.relpath(file_path, plugin_dir)
                    zf.write(file_path, arcname)

        buf.seek(0)
        return send_file(
            buf,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{plugin_id}.zip',
        )

    except PermissionError:
        return jsonify({'error': f'Permission denied reading plugin files for: {plugin_id}'}), 500
    except OSError as exc:
        return jsonify({'error': f'Failed to export plugin: {exc}'}), 500


@plugins_bp.route('/api/plugins/<plugin_id>', methods=['DELETE'])
def api_delete_plugin(plugin_id):
    """Uninstall and delete a plugin."""
    result = plugin_manager.uninstall_plugin(plugin_id)
    if 'error' in result:
        return jsonify(result), 400
    audit.log_plugin(user_id='admin', plugin_id=plugin_id, action='uninstall', ip=request.remote_addr or '')
    return jsonify(result)


# ── Per-Agent Plugin Settings ──

@plugins_bp.route('/api/agents/<agent_id>/plugin-settings')
def api_agent_plugin_settings(agent_id):
    """Get all per-agent plugin settings for an agent."""
    settings = plugin_manager.get_all_agent_plugin_settings(agent_id)
    return jsonify({'plugin_settings': settings})


@plugins_bp.route('/api/agents/<agent_id>/plugin-settings/<plugin_id>', methods=['PUT'])
def api_set_agent_plugin_settings(agent_id, plugin_id):
    """Save per-agent settings for a specific plugin."""
    data = request.get_json(force=True) or {}
    result = plugin_manager.set_agent_plugin_settings(plugin_id, agent_id, data)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)
