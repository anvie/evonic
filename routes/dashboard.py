import os
import re
import sys

import logging
import threading
import time

from flask import Blueprint, render_template, jsonify, request, redirect

from models.db import db
from backend.plugin_manager import plugin_manager
from backend.skills_manager import skills_manager
from backend.skillsets import list_skillsets
from backend.setup import (run_setup, test_connection, PROVIDER_DEFAULTS,
                            LANGUAGE_PRESETS, check_docker_available)
import config

# `resource` is a POSIX-only stdlib module — absent on Windows.
# Guarded so the app can boot; restart-time FD cleanup is skipped there.
if sys.platform != 'win32':
    import resource

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
def index():
    """Platform dashboard — data loaded client-side via /api/dashboard/data"""
    return render_template('index.html')


@dashboard_bp.route('/setup')
def setup_page():
    """First-time super agent setup page."""
    if db.has_super_agent():
        return redirect('/')
    return render_template('setup.html',
                           providers=PROVIDER_DEFAULTS,
                           languages=LANGUAGE_PRESETS)


@dashboard_bp.route('/api/setup', methods=['POST'])
def api_setup():
    """Create the super agent. Only callable once."""
    if db.has_super_agent():
        return jsonify({'error': 'Super agent already exists'}), 400
    data = request.get_json() or {}

    # New wizard payload
    if 'provider' in data:
        # Auto-detect Docker; client may override with explicit sandbox_enabled
        sandbox_enabled = data.get('sandbox_enabled', False)
        if 'sandbox_enabled' not in data:
            sandbox_enabled = check_docker_available().get('available', False)
        result = run_setup(
            provider=data.get('provider', ''),
            model_name=data.get('model_name', '').strip(),
            base_url=data.get('base_url', '').strip(),
            api_key=(data.get('api_key') or '').strip(),
            agent_name=data.get('agent_name', '').strip(),
            agent_id=data.get('agent_id', '').strip() or None,
            description=data.get('description', ''),
            language=data.get('language', 'english'),
            sandbox_enabled=sandbox_enabled,
            password=(data.get('password') or '').strip(),
        )
        if 'error' in result:
            return jsonify(result), 400

        # Auto-restart after successful setup
        _log = logging.getLogger(__name__)
        _log.info("Setup complete — scheduling auto-restart")

        def _do_restart():
            time.sleep(1.5)
            try:
                from backend.channels.registry import channel_manager
                channel_manager.stop_all()
                time.sleep(1.0)
                if sys.platform != 'win32':
                    maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
                    if maxfd == resource.RLIM_INFINITY or maxfd > 65535:
                        maxfd = 4096
                    os.closerange(3, maxfd)
            except Exception as e:
                _log.error("Error during restart cleanup: %s", e, exc_info=True)
            _log.info("Re-executing server process")
            os.execv(sys.executable, [sys.executable] + sys.argv)

        t = threading.Thread(target=_do_restart, daemon=True)
        t.start()

        return jsonify(result)

    # Legacy payload (backward compat): {id, name, description, system_prompt, model}
    from routes.agents import _ensure_kb_dir, _write_system_prompt
    agent_id = data.get('id', '').strip().lower()
    name = data.get('name', '').strip()
    if not agent_id or not re.match(r'^[a-z0-9_]+$', agent_id):
        return jsonify({'error': 'Invalid ID. Use only lowercase alphanumeric characters and underscores (snake_case).'}), 400
    if not name:
        return jsonify({'error': 'Name is required.'}), 400
    if db.get_agent(agent_id):
        return jsonify({'error': 'Agent ID already exists.'}), 400
    sandbox_enabled = data.get('sandbox_enabled', check_docker_available().get('available', False))
    try:
        _ensure_kb_dir(agent_id)
        system_prompt = data.get('system_prompt', '').strip()
        if not system_prompt:
            _default_path = os.path.join(config.BASE_DIR, 'defaults', 'super_agent_system_prompt.md')
            if os.path.isfile(_default_path):
                with open(_default_path, 'r', encoding='utf-8') as _f:
                    system_prompt = _f.read()
        db.create_agent({
            'id': agent_id,
            'name': name,
            'description': data.get('description', ''),
            'system_prompt': system_prompt,
            'model': data.get('model') or None,
            'is_super': True,
            'workspace': config.BASE_DIR,
            'sandbox_enabled': 1 if sandbox_enabled else 0,
        })
        _write_system_prompt(agent_id, system_prompt)
        db.set_setting('super_agent_id', agent_id)
        db.set_setting('sandbox_default_enabled', '1' if sandbox_enabled else '0')
        db.set_agent_tools(agent_id, [
            'bash', 'runpy', 'patch', 'write_file', 'read_file',
            'skill:scheduler:create_schedule',
            'skill:scheduler:cancel_schedule',
            'skill:scheduler:list_schedules',
        ])
        db.set_agent_skills(agent_id, ['scheduler'])
        return jsonify({'success': True, 'agent_id': agent_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@dashboard_bp.route('/api/setup/test-connection', methods=['POST'])
def api_setup_test_connection():
    """Test connectivity to an LLM endpoint. Usable before setup is complete."""
    data = request.get_json() or {}
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip() or None
    result = test_connection(base_url, api_key)
    status = 200 if result['success'] else 400
    return jsonify(result), status


@dashboard_bp.route('/api/setup/docker-status', methods=['GET'])
def api_docker_status():
    """Check if Docker is available on the server."""
    result = check_docker_available()
    return jsonify(result)


@dashboard_bp.route('/api/dashboard/data')
def api_dashboard_data():
    """Full dashboard data for client-side rendering"""
    stats = db.get_dashboard_stats()
    recent_agents = db.get_recent_agents(limit=5)
    for a in recent_agents:
        a.pop('workspace', None)
    recent_runs = db.get_recent_runs(limit=5)
    leaderboard = db.get_model_leaderboard(limit=5)
    model_usage = db.get_model_usage()

    all_skills = skills_manager.list_skills()
    skill_stats = {
        'total': len(all_skills),
        'enabled': sum(1 for s in all_skills if s.get('enabled')),
        'skillset_count': len(list_skillsets()),
    }

    all_plugins = plugin_manager.list_plugins()
    plugin_stats = {
        'total': len(all_plugins),
        'enabled': sum(1 for p in all_plugins if p.get('enabled')),
    }

    all_schedules = db.get_schedules()
    schedule_stats = {
        'total': len(all_schedules),
        'active': sum(1 for s in all_schedules if s.get('enabled')),
    }

    # Plugin-provided dashboard cards (zero core-plugin coupling)
    plugin_cards = plugin_manager.get_dashboard_cards()

    return jsonify({
        'stats': stats,
        'recent_agents': recent_agents,
        'recent_runs': recent_runs,
        'leaderboard': leaderboard,
        'model_usage': model_usage,
        'skill_stats': skill_stats,
        'plugin_stats': plugin_stats,
        'schedule_stats': schedule_stats,
        'plugin_cards': plugin_cards,
    })


@dashboard_bp.route('/api/dashboard/stats')
def api_dashboard_stats():
    """Dashboard stats for async refresh"""
    stats = db.get_dashboard_stats()
    leaderboard = db.get_model_leaderboard(limit=5)
    model_usage = db.get_model_usage()
    return jsonify({
        'stats': stats,
        'leaderboard': leaderboard,
        'model_usage': model_usage,
    })
