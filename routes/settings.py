import json
import logging
import math
import os
import queue
import re
from typing import Dict, Any

from flask import Blueprint, render_template, jsonify, request, Response, stream_with_context

from backend.audit_logger import audit
import config
from models.boolean import FALSE_VALUES, TRUE_VALUES
from models.db import db

_logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__)

_SENSITIVE_MODEL_KEYS = frozenset({'api_key'})


def _audit_setting_change(key, old_val, new_val):
    """Log a setting change if old and new values differ."""
    ip = request.remote_addr or ''
    old_str = str(old_val)[:500] if old_val is not None else ''
    new_str = str(new_val)[:500]
    if old_str != new_str:
        audit.log_setting_change(user_id='admin', key=key, old_value=old_str, new_value=new_str, ip=ip)


def _sanitize_model(model: Dict[str, Any]) -> Dict[str, Any]:
    """Strip sensitive fields (api_key) from a model dict before API response."""
    for key in _SENSITIVE_MODEL_KEYS:
        model.pop(key, None)
    return model


def _coerce_boolean(value: Any) -> bool:
    """Convert an API boolean value, rejecting ambiguous input."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
    raise ValueError('must be a boolean')


@settings_bp.route('/system')
def settings():
    """System page - manage tests"""
    return render_template('settings.html')


@settings_bp.route('/system/models')
def settings_models():
    """Models system page"""
    return render_template('settings_models.html')


# ---- Domain operations ----

@settings_bp.route('/api/settings/domains', methods=['GET'])
def api_list_domains():
    """List all domains (including disabled for settings page)"""
    from evaluator.test_manager import test_manager
    domains = test_manager.list_domains(include_disabled=True)
    return jsonify({'domains': domains})


@settings_bp.route('/api/settings/domains/<domain_id>', methods=['GET'])
def api_get_domain(domain_id):
    """Get a single domain"""
    from evaluator.test_manager import test_manager
    domain = test_manager.get_domain(domain_id)
    if not domain:
        return jsonify({'error': 'Domain not found'}), 404
    return jsonify(domain)


@settings_bp.route('/api/settings/domains', methods=['POST'])
def api_create_domain():
    """Create a new domain"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        domain = test_manager.create_domain(data, is_custom=True)
        return jsonify({'success': True, 'domain': domain})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/domains/<domain_id>', methods=['PUT'])
def api_update_domain(domain_id):
    """Update a domain"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        domain = test_manager.update_domain(domain_id, data)
        return jsonify({'success': True, 'domain': domain})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/domains/<domain_id>', methods=['DELETE'])
def api_delete_domain(domain_id):
    """Delete a domain"""
    from evaluator.test_manager import test_manager
    try:
        success = test_manager.delete_domain(domain_id)
        return jsonify({'success': success})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ---- Level operations ----

@settings_bp.route('/api/settings/levels/<domain_id>/<int:level>', methods=['GET'])
def api_get_level(domain_id, level):
    """Get level configuration"""
    from evaluator.test_manager import test_manager
    result = test_manager.get_level(domain_id, level)
    return jsonify({'success': True, 'level': result})


@settings_bp.route('/api/settings/levels/<domain_id>/<int:level>', methods=['PUT'])
def api_update_level(domain_id, level):
    """Update level configuration"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        result = test_manager.update_level(domain_id, level, data)
        return jsonify({'success': True, 'level': result})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ---- Test operations ----

@settings_bp.route('/api/settings/tests', methods=['GET'])
def api_list_tests():
    """List tests"""
    from evaluator.test_manager import test_manager
    domain_id = request.args.get('domain')
    level = request.args.get('level', type=int)
    tests = test_manager.list_tests(domain_id=domain_id, level=level)
    return jsonify({'tests': tests})


@settings_bp.route('/api/settings/tests/<test_id>', methods=['GET'])
def api_get_test(test_id):
    """Get a single test"""
    from evaluator.test_manager import test_manager
    test = test_manager.get_test(test_id)
    if not test:
        return jsonify({'error': 'Test not found'}), 404
    return jsonify(test)


@settings_bp.route('/api/settings/tests', methods=['POST'])
def api_create_test():
    """Create a new test"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    domain_id = data.get('domain_id')
    level = data.get('level', 1)

    if not domain_id:
        return jsonify({'success': False, 'error': 'domain_id is required'}), 400

    try:
        test = test_manager.create_test(domain_id, level, data)
        return jsonify({'success': True, 'test': test})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/tests/<test_id>', methods=['PUT'])
def api_update_test(test_id):
    """Update a test"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        test = test_manager.update_test(test_id, data)
        return jsonify({'success': True, 'test': test})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/tests/<test_id>', methods=['DELETE'])
def api_delete_test(test_id):
    """Delete a test"""
    from evaluator.test_manager import test_manager
    try:
        success = test_manager.delete_test(test_id)
        return jsonify({'success': success})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/tests/<test_id>/move', methods=['POST'])
def api_move_test(test_id):
    """Move a test to different domain/level"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    new_domain = data.get('domain_id')
    new_level = data.get('level')

    if not new_domain or not new_level:
        return jsonify({'success': False, 'error': 'domain_id and level are required'}), 400

    try:
        test = test_manager.move_test(test_id, new_domain, new_level)
        return jsonify({'success': True, 'test': test})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ---- Evaluator operations ----

@settings_bp.route('/api/settings/evaluators', methods=['GET'])
def api_list_evaluators():
    """List all evaluators"""
    from evaluator.test_manager import test_manager
    evaluators = test_manager.list_evaluators()
    return jsonify({'evaluators': evaluators})


@settings_bp.route('/api/settings/evaluators/<evaluator_id>', methods=['GET'])
def api_get_evaluator(evaluator_id):
    """Get a single evaluator"""
    from evaluator.test_manager import test_manager
    evaluator = test_manager.get_evaluator(evaluator_id)
    if not evaluator:
        return jsonify({'error': 'Evaluator not found'}), 404
    return jsonify(evaluator)


@settings_bp.route('/api/settings/evaluators', methods=['POST'])
def api_create_evaluator():
    """Create a new custom evaluator"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        evaluator = test_manager.create_evaluator(data, is_custom=True)
        return jsonify({'success': True, 'evaluator': evaluator})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/evaluators/<evaluator_id>', methods=['PUT'])
def api_update_evaluator(evaluator_id):
    """Update an evaluator"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        evaluator = test_manager.update_evaluator(evaluator_id, data)
        return jsonify({'success': True, 'evaluator': evaluator})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/evaluators/<evaluator_id>', methods=['DELETE'])
def api_delete_evaluator(evaluator_id):
    """Delete a custom evaluator"""
    from evaluator.test_manager import test_manager
    try:
        success = test_manager.delete_evaluator(evaluator_id)
        return jsonify({'success': success})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ---- Tool operations ----

@settings_bp.route('/api/settings/tools', methods=['GET'])
def api_list_tools():
    """List all tools from registry (builtins first, then JSON tools, then skill tools)."""
    from evaluator.test_manager import test_manager
    from backend.skills_manager import skills_manager
    from backend.tools import tool_registry
    from backend.tools.agent_messaging import get_agent_messaging_tool_defs

    agent_context = None
    agent_id = request.args.get('agent_id')
    if agent_id:
        agent = db.get_agent(agent_id)
        if not agent:
            return jsonify({'error': 'Agent not found'}), 404
        agent_context = {
            'id': agent['id'],
            'enable_atg': bool(agent.get('enable_atg')) and bool(agent.get('enable_agent_state')),
            'enable_cmp': bool(agent.get('enable_cmp')) and bool(agent.get('enable_agent_state')),
            'always_execute': bool(agent.get('always_execute')),
        }

    # Built-ins are filtered by the selected agent's feature settings when supplied.
    tools = tool_registry.get_builtin_tool_defs(agent_context)
    tools += test_manager.list_tools()
    # Append agent messaging tools (auto-loaded when agent_messaging_enabled)
    for tool_def in get_agent_messaging_tool_defs():
        func = tool_def.get('function', {})
        tools.append({
            'id': func.get('name', ''),
            'name': func.get('name', ''),
            'description': func.get('description', ''),
            'function': func,
            '_auto_loaded': True,
        })
    # Append ALL skill tool definitions (no dedup — namespaced IDs disambiguate)
    for skill_def in skills_manager.get_all_skill_tool_defs():
        func = skill_def.get('function', {})
        tools.append({
            'id': skill_def.get('id', ''),  # namespaced: skill:skill_id:fn_name
            'name': func.get('name', ''),
            'description': func.get('description', ''),
            'function': func,
            '_skill_id': skill_def.get('_skill_id', ''),
        })
    # Append plugin tool definitions (namespaced: plugin:plugin_id:fn_name)
    from backend.plugin_manager import plugin_manager
    for plugin_def in plugin_manager.get_all_plugin_tool_defs():
        func = plugin_def.get('function', {})
        tools.append({
            'id': plugin_def.get('id', ''),
            'name': func.get('name', ''),
            'description': func.get('description', ''),
            'function': func,
            '_plugin_id': plugin_def.get('_plugin_id', ''),
        })
    return jsonify({'tools': tools})


@settings_bp.route('/api/settings/tools/<tool_id>', methods=['GET'])
def api_get_tool(tool_id):
    """Get a single tool"""
    from evaluator.test_manager import test_manager
    from backend.skills_manager import skills_manager
    tool = test_manager.get_tool(tool_id)
    if not tool and tool_id.startswith('skill:'):
        # Look up skill tool from skills_manager
        for skill_def in skills_manager.get_all_skill_tool_defs():
            if skill_def.get('id') == tool_id:
                func = skill_def.get('function', {})
                tool = {
                    'id': skill_def.get('id', ''),
                    'name': func.get('name', ''),
                    'description': func.get('description', ''),
                    'function': func,
                    '_skill_id': skill_def.get('_skill_id', ''),
                    'no_mock': skill_def.get('no_mock', False),
                }
                break
    if not tool and tool_id.startswith('plugin:'):
        # Look up plugin tool from plugin_manager
        from backend.plugin_manager import plugin_manager
        for plugin_def in plugin_manager.get_all_plugin_tool_defs():
            if plugin_def.get('id') == tool_id:
                func = plugin_def.get('function', {})
                tool = {
                    'id': plugin_def.get('id', ''),
                    'name': func.get('name', ''),
                    'description': func.get('description', ''),
                    'function': func,
                    '_plugin_id': plugin_def.get('_plugin_id', ''),
                    'no_mock': plugin_def.get('no_mock', False),
                }
                break
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404
    return jsonify(tool)


@settings_bp.route('/api/settings/tools', methods=['POST'])
def api_create_tool():
    """Create a new tool"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    try:
        tool = test_manager.create_tool(data)
        return jsonify({'success': True, 'tool': tool})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/tools/<tool_id>', methods=['PUT'])
def api_update_tool(tool_id):
    """Update a tool"""
    from evaluator.test_manager import test_manager
    from backend.skills_manager import skills_manager
    data = request.get_json()

    if tool_id.startswith('skill:'):
        # Skill tools: only persist no_mock into the skill's tool-defs JSON
        parts = tool_id.split(':', 2)
        if len(parts) != 3:
            return jsonify({'success': False, 'error': 'Invalid skill tool ID'}), 400
        _, skill_id, fn_name = parts
        result = skills_manager.update_skill_tool_field(skill_id, fn_name, 'no_mock', data.get('no_mock', False))
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 400
        return jsonify({'success': True})

    try:
        tool = test_manager.update_tool(tool_id, data)
        return jsonify({'success': True, 'tool': tool})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/tools/<tool_id>', methods=['DELETE'])
def api_delete_tool(tool_id):
    """Delete a tool"""
    from evaluator.test_manager import test_manager
    try:
        success = test_manager.delete_tool(tool_id)
        return jsonify({'success': success})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@settings_bp.route('/api/settings/tools/<tool_id>/backend', methods=['GET'])
def api_get_tool_backend(tool_id):
    """Get backend Python code for a tool"""
    if tool_id.startswith('skill:'):
        from backend.skills_manager import skills_manager
        parts = tool_id.split(':', 2)
        if len(parts) != 3:
            return jsonify({'error': 'Invalid skill tool ID'}), 400
        _, skill_id, fn_name = parts
        backend_path = skills_manager.find_tool_backend_path(fn_name, skill_id=skill_id)
        if backend_path and os.path.isfile(backend_path):
            with open(backend_path, 'r', encoding='utf-8') as f:
                return jsonify({'code': f.read(), 'exists': True})
        return jsonify({'code': '', 'exists': False})

    if tool_id.startswith('plugin:'):
        from backend.plugin_manager import plugin_manager
        parts = tool_id.split(':', 2)
        if len(parts) != 3:
            return jsonify({'error': 'Invalid plugin tool ID'}), 400
        _, plugin_id, fn_name = parts
        backend_path, _pid = plugin_manager.find_plugin_tool_backend(fn_name, plugin_id=plugin_id)
        if backend_path and os.path.isfile(backend_path):
            with open(backend_path, 'r', encoding='utf-8') as f:
                return jsonify({'code': f.read(), 'exists': True})
        return jsonify({'code': '', 'exists': False})

    if not re.match(r'^[a-zA-Z0-9_]+$', tool_id):
        return jsonify({'error': 'Invalid tool ID'}), 400
    backend_path = os.path.join(config.BASE_DIR, 'backend', 'tools', f'{tool_id}.py')
    backend_path = os.path.normpath(backend_path)
    if os.path.isfile(backend_path):
        with open(backend_path, 'r', encoding='utf-8') as f:
            return jsonify({'code': f.read(), 'exists': True})
    return jsonify({'code': '', 'exists': False})


@settings_bp.route('/api/settings/tools/<tool_id>/backend', methods=['PUT'])
def api_update_tool_backend(tool_id):
    """Update backend Python code for a tool"""
    data = request.get_json()
    code = data.get('code', '')

    if tool_id.startswith('skill:'):
        from backend.skills_manager import skills_manager
        parts = tool_id.split(':', 2)
        if len(parts) != 3:
            return jsonify({'error': 'Invalid skill tool ID'}), 400
        _, skill_id, fn_name = parts
        skill_dir = os.path.join(config.BASE_DIR, 'skills', skill_id)
        backend_dir = os.path.normpath(os.path.join(skill_dir, 'backend', 'tools'))
        os.makedirs(backend_dir, exist_ok=True)
        backend_path = os.path.normpath(os.path.join(backend_dir, f'{fn_name}.py'))
        if not backend_path.startswith(backend_dir):
            return jsonify({'error': 'Invalid path'}), 400
        with open(backend_path, 'w', encoding='utf-8') as f:
            f.write(code)
        return jsonify({'success': True})

    if not re.match(r'^[a-zA-Z0-9_]+$', tool_id):
        return jsonify({'error': 'Invalid tool ID'}), 400
    backend_dir = os.path.join(config.BASE_DIR, 'backend', 'tools')
    backend_path = os.path.normpath(os.path.join(backend_dir, f'{tool_id}.py'))
    if not backend_path.startswith(os.path.normpath(backend_dir)):
        return jsonify({'error': 'Invalid path'}), 400
    with open(backend_path, 'w', encoding='utf-8') as f:
        f.write(code)
    return jsonify({'success': True})


@settings_bp.route('/api/settings/tools/<tool_id>/test', methods=['POST'])
def api_test_tool(tool_id):
    """Test-execute a tool with given arguments in real or mock mode"""
    if not re.match(r'^[a-zA-Z0-9_]+$', tool_id):
        return jsonify({'error': 'Invalid tool ID'}), 400

    data = request.get_json()
    args = data.get('args', {})
    mode = data.get('mode', 'real')  # 'real' or 'mock'

    from backend.tools.registry import ToolRegistry
    registry = ToolRegistry()

    try:
        if mode == 'mock':
            # Find tool definition for mock response
            tool_def = None
            for td in registry.get_tool_defs_from_json():
                tid = td.get('id') or td.get('function', {}).get('name')
                if tid == tool_id:
                    tool_def = td
                    break
            if not tool_def or 'mock_response' not in tool_def:
                return jsonify({'error': f'No mock response defined for tool: {tool_id}'})
            mock_value = tool_def['mock_response']
            if isinstance(mock_value, dict):
                return jsonify({'result': mock_value})
            return jsonify({'result': {'result': mock_value}})
        else:
            # Real mode
            agent_context = {
                'agent_id': 'test',
                'agent_name': 'Test',
                'user_id': 'test_user',
                'channel_id': None,
                'session_id': 'test_session'
            }
            executor = registry.get_real_executor(agent_context)
            result = executor(tool_id, args)
            return jsonify({'result': result})
    except Exception as e:
        return jsonify({'error': str(e)})


# ---- Import/Export/Sync operations ----

@settings_bp.route('/api/settings/export', methods=['GET'])
def api_export_tests():
    """Export all test definitions"""
    from evaluator.test_manager import test_manager
    data = test_manager.export_all()
    return jsonify(data)


@settings_bp.route('/api/settings/import', methods=['POST'])
def api_import_tests():
    """Import test definitions"""
    from evaluator.test_manager import test_manager
    data = request.get_json()
    merge = data.get('merge', True)
    result = test_manager.import_all(data, merge=merge)
    return jsonify(result)


@settings_bp.route('/api/settings/sync', methods=['POST'])
def api_sync_tests():
    """Sync test definitions to database"""
    from evaluator.test_manager import test_manager
    test_manager.sync_to_db()
    return jsonify({'success': True})


# ---- App settings toggles ----

@settings_bp.route('/api/settings/two-pass-enabled', methods=['GET', 'PUT'])
def api_two_pass_enabled():
    """Get or set global two-pass (Pass 2) answer extraction for evaluation."""
    from models.db import db
    import config as app_config

    default = '1' if getattr(app_config, 'TWO_PASS_ENABLED', True) else '0'
    if request.method == 'PUT':
        data = request.get_json() or {}
        enabled = '1' if data.get('enabled', False) else '0'
        old_val = db.get_setting('two_pass_enabled', default)
        db.set_setting('two_pass_enabled', enabled)
        _audit_setting_change('two_pass_enabled', old_val, enabled)
        return jsonify({'success': True, 'enabled': enabled == '1'})
    val = db.get_setting('two_pass_enabled', default)
    return jsonify({'enabled': val == '1'})


@settings_bp.route('/api/settings/public-history', methods=['GET', 'PUT'])
def api_public_history():
    """Get or set the public history page toggle."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        enabled = '1' if data.get('enabled', False) else '0'
        old_val = db.get_setting('public_history', '0')
        db.set_setting('public_history', enabled)
        _audit_setting_change('public_history', old_val, enabled)
        return jsonify({'success': True, 'enabled': enabled == '1'})
    val = db.get_setting('public_history', '0')
    return jsonify({'enabled': val == '1'})


@settings_bp.route('/api/settings/long-running-guard', methods=['PUT'])
def api_long_running_guard():
    """Toggle the long-running command guard (detect build/compile commands)."""
    from models.db import db
    data = request.get_json()
    enabled = '1' if data.get('enabled', True) else '0'
    old_val = db.get_setting('long_running_guard_enabled', '1' if config.LONG_RUNNING_GUARD_ENABLED else '0')
    db.set_setting('long_running_guard_enabled', enabled)
    _audit_setting_change('long_running_guard_enabled', old_val, enabled)
    return jsonify({'success': True, 'enabled': enabled == '1'})


@settings_bp.route('/api/settings/message-wrapper', methods=['PUT'])
def api_message_wrapper():
    """Toggle the message wrapper globally."""
    from models.db import db
    data = request.get_json()
    enabled = '1' if data.get('enabled', True) else '0'
    old_val = db.get_setting('message_wrapper_enabled', '1')
    db.set_setting('message_wrapper_enabled', enabled)
    _audit_setting_change('message_wrapper_enabled', old_val, enabled)
    return jsonify({'success': True, 'enabled': enabled == '1'})


@settings_bp.route('/api/settings/agent-timeout-retries', methods=['GET', 'PUT'])
def api_agent_timeout_retries():
    """Get or set the number of auto-retries when LLM times out during chat."""
    from models.db import db
    from config import AGENT_TIMEOUT_RETRIES
    if request.method == 'PUT':
        data = request.get_json()
        value = max(0, int(data.get('value', AGENT_TIMEOUT_RETRIES)))
        db.set_setting('agent_timeout_retries', str(value))
        return jsonify({'success': True, 'value': value})
    val = db.get_setting('agent_timeout_retries', str(AGENT_TIMEOUT_RETRIES))
    return jsonify({'value': int(val)})


@settings_bp.route('/api/settings/llm-max-retries', methods=['GET', 'PUT'])
def api_llm_max_retries():
    """Get or set the maximum number of LLM API retry attempts on transient errors."""
    from models.db import db
    DEFAULT_MAX_RETRIES = 5
    if request.method == 'PUT':
        data = request.get_json()
        value = max(0, int(data.get('value', DEFAULT_MAX_RETRIES)))
        db.set_setting('llm_max_retries', str(value))
        return jsonify({'success': True, 'value': value})
    val = db.get_setting('llm_max_retries', str(DEFAULT_MAX_RETRIES))
    return jsonify({'value': int(val)})


@settings_bp.route('/api/settings/max-concurrent-llm-per-agent', methods=['GET', 'PUT'])
def api_max_concurrent_llm_per_agent():
    """Get or set max concurrent turns per agent (0 = unlimited)."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        value = max(0, int(data.get('value', 1)))
        db.set_setting('max_concurrent_llm_per_agent', str(value))
        try:
            from backend.agent_runtime.runtime import AgentRuntime
            if AgentRuntime._concurrency_mgr:
                AgentRuntime._concurrency_mgr.refresh_agent_limit()
        except Exception:
            pass
        return jsonify({'success': True, 'value': value})
    val = db.get_setting('max_concurrent_llm_per_agent', '1')
    return jsonify({'value': int(val)})


@settings_bp.route('/api/settings/max-concurrent-llm-per-model', methods=['GET', 'PUT'])
def api_max_concurrent_llm_per_model():
    """Get or set global max concurrent turns per model (0 = unlimited)."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        value = max(0, int(data.get('value', 0)))
        db.set_setting('max_concurrent_llm_per_model', str(value))
        try:
            from backend.agent_runtime.runtime import AgentRuntime
            if AgentRuntime._concurrency_mgr:
                AgentRuntime._concurrency_mgr.refresh_all_model_limits()
        except Exception:
            pass
        return jsonify({'success': True, 'value': value})
    val = db.get_setting('max_concurrent_llm_per_model', '0')
    return jsonify({'value': int(val)})


@settings_bp.route('/api/settings/agent-queue-workers', methods=['GET', 'PUT'])
def api_agent_queue_workers():
    """Get or set the number of agent queue worker threads (1-32)."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        raw_value = int(data.get('value', config.AGENT_QUEUE_WORKERS))
        if raw_value > 32:
            _logger.warning("Agent queue workers requested %d capped to max 32", raw_value)
        value = max(1, min(32, raw_value))
        db.set_setting('agent_queue_workers', str(value))
        result = {'success': True, 'value': value}
        try:
            from backend.agent_runtime import agent_runtime
            info = agent_runtime.resize_workers(value)
            if info.get('note'):
                result['note'] = info['note']
        except Exception:
            pass
        return jsonify(result)
    val = db.get_setting('agent_queue_workers', str(config.AGENT_QUEUE_WORKERS))
    return jsonify({'value': int(val)})


@settings_bp.route('/api/settings/max-tool-iterations', methods=['GET', 'PUT'])
def api_max_tool_iterations():
    """Get or set the maximum tool-call iterations per agent turn and per evaluation (1-1000)."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        raw_value = int(data.get('value', config.AGENT_MAX_TOOL_ITERATIONS))
        value = max(1, min(1000, raw_value))
        db.set_setting('max_tool_iterations', str(value))
        return jsonify({'success': True, 'value': value})
    val = db.get_setting('max_tool_iterations', str(config.AGENT_MAX_TOOL_ITERATIONS))
    return jsonify({'value': int(val)})


@settings_bp.route('/api/settings/events-dispatch', methods=['GET', 'PUT'])
def api_events_dispatch():
    """Get or set the global events dispatch toggle."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        enabled = '1' if data.get('enabled', True) else '0'
        old_val = db.get_setting('events_dispatch_enabled', '1')
        db.set_setting('events_dispatch_enabled', enabled)
        _audit_setting_change('events_dispatch_enabled', old_val, enabled)
        return jsonify({'success': True, 'enabled': enabled == '1'})
    val = db.get_setting('events_dispatch_enabled', '1')
    return jsonify({'enabled': val == '1'})


@settings_bp.route('/api/settings/theme', methods=['GET', 'PUT'])
def api_theme():
    """Get or set the UI theme (light, dark, system)."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json()
        theme = data.get('theme', 'system')
        if theme not in ('light', 'dark', 'system'):
            theme = 'system'
        old_val = db.get_setting('theme', 'system')
        db.set_setting('theme', theme)
        _audit_setting_change('theme', old_val, theme)
        return jsonify({'success': True, 'theme': theme})
    val = db.get_setting('theme', 'system')
    return jsonify({'theme': val})


@settings_bp.route('/api/settings/task-classifier', methods=['GET', 'PUT'])
def api_task_classifier():
    """Get or set task classifier settings (enabled toggle + model selection)."""
    from models.db import db
    default_enabled = '1' if config.TASK_CLASSIFIER_ENABLED else '0'
    if request.method == 'PUT':
        data = request.get_json() or {}
        enabled = '1' if data.get('enabled', True) else '0'
        model_id = data.get('model_id', '') or ''
        if model_id:
            model = db.get_model_by_id(model_id)
            if not model:
                return jsonify({'success': False, 'error': 'Model not found'}), 404
            model_id = model['id']  # canonicalize legacy ids
        db.set_setting('task_classifier_enabled', enabled)
        db.set_setting('task_classifier_model_id', model_id)
        old_enabled = db.get_setting('task_classifier_enabled', default_enabled)
        old_model_id = db.get_setting('task_classifier_model_id', '')
        _audit_setting_change('task_classifier_enabled', old_enabled, enabled)
        if old_model_id != model_id:
            _audit_setting_change('task_classifier_model_id', old_model_id, model_id)
        return jsonify({
            'success': True,
            'enabled': enabled == '1',
            'model_id': model_id or None,
        })
    enabled = db.get_setting('task_classifier_enabled', default_enabled)
    model_id = db.get_setting('task_classifier_model_id', '')
    return jsonify({
        'enabled': enabled == '1',
        'model_id': model_id or None,
    })


@settings_bp.route('/api/settings/cmp-model', methods=['GET', 'PUT'])
def api_cmp_model():
    """Get or set the model used by CMP (Context Memory Path): path-change
    boundary detection and path card summarization. Empty = fall back to
    the Task Classifier model, then the default model."""
    from models.db import db
    if request.method == 'PUT':
        data = request.get_json() or {}
        model_id = data.get('model_id', '') or ''
        if model_id:
            model = db.get_model_by_id(model_id)
            if not model:
                return jsonify({'success': False, 'error': 'Model not found'}), 404
            model_id = model['id']  # canonicalize legacy ids
        old_model_id = db.get_setting('cmp_model_id', '')
        db.set_setting('cmp_model_id', model_id)
        if old_model_id != model_id:
            _audit_setting_change('cmp_model_id', old_model_id, model_id)
        return jsonify({'success': True, 'model_id': model_id or None})
    return jsonify({'model_id': db.get_setting('cmp_model_id', '') or None})


# ---- Default Model operations ----

@settings_bp.route('/api/settings/default-model', methods=['GET'])
def api_get_default_model():
    """Get current default model config from DB."""
    model = db.get_default_model()
    if not model:
        return jsonify({'model': None})
    return jsonify({'model': _sanitize_model(model)})


@settings_bp.route('/api/settings/default-model', methods=['POST'])
def api_set_default_model():
    """Set default model by model_id."""
    data = request.get_json()
    model_id = data.get('model_id') if data else None
    if not model_id:
        return jsonify({'success': False, 'error': 'model_id is required'}), 400
    
    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({'success': False, 'error': 'Model not found'}), 404
    
    try:
        old_model = db.get_default_model()
        old_id = old_model.get('id', '') if old_model else ''
        with db._connect() as conn:
            conn.execute("UPDATE llm_models SET is_default = 0")
            conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = ?", (model['id'],))
            conn.commit()
        _audit_setting_change('default_model', old_id, model['id'])
        return jsonify({'success': True, 'model': _sanitize_model(db.get_default_model())})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- General settings bulk read ----

@settings_bp.route('/api/settings/general', methods=['GET'])
def api_get_general_settings():
    """Return all general-tab settings in a single response."""
    return jsonify({
        'public_history': db.get_setting('public_history', '0') == '1',
        'long_running_guard_enabled': db.get_setting('long_running_guard_enabled',
                                                     '1' if config.LONG_RUNNING_GUARD_ENABLED else '0') == '1',
        'message_wrapper_enabled': db.get_setting('message_wrapper_enabled', '1') == '1',
        'agent_timeout_retries': int(db.get_setting('agent_timeout_retries', str(config.AGENT_TIMEOUT_RETRIES))),
        'llm_max_retries': int(db.get_setting('llm_max_retries', '5')),
        'max_concurrent_llm_per_agent': int(db.get_setting('max_concurrent_llm_per_agent', '1')),
        'max_concurrent_llm_per_model': int(db.get_setting('max_concurrent_llm_per_model', '0')),
        'max_concurrent_llm_global': int(db.get_setting('max_concurrent_llm_global', '1')),
        'agent_queue_workers': int(db.get_setting('agent_queue_workers', str(config.AGENT_QUEUE_WORKERS))),
        'max_tool_iterations': int(db.get_setting('max_tool_iterations', str(config.AGENT_MAX_TOOL_ITERATIONS))),
        'agent_sidebar_limit': int(db.get_setting('agent_sidebar_limit', str(config.AGENT_SIDEBAR_LIMIT))),
        'theme': db.get_setting('theme', 'system'),
        'default_model_fallback_id': db.get_setting('default_model_fallback_id', ''),
        'vision_model_id': db.get_setting('vision_model_id', ''),
        'vision_fallback_model_id': db.get_setting('vision_fallback_model_id', ''),
        'vision_fallback_model_2_id': db.get_setting('vision_fallback_model_2_id', ''),
        'kb_organizer_model_id': db.get_setting('kb_organizer_model_id', ''),
        'kb_organizer_nightly_time': db.get_setting(
            'kb_organizer_nightly_time',
            os.getenv('EVOMEM_KB_ORGANIZER_NIGHTLY_TIME', '03:00'),
        ),
        # ── WhatsApp Safe Delivery (global) ──
        'whatsapp_safe_delivery_enabled': db.get_setting('whatsapp_safe_delivery_enabled',
                                                         '1' if config.WHATSAPP_SAFE_DELIVERY_ENABLED else '0') == '1',
        'whatsapp_pool_window_seconds': float(db.get_setting('whatsapp_pool_window_seconds',
                                                             str(config.WHATSAPP_POOL_WINDOW_SECONDS))),
        'whatsapp_min_send_interval_seconds': float(db.get_setting('whatsapp_min_send_interval_seconds',
                                                                    str(config.WHATSAPP_MIN_SEND_INTERVAL_SECONDS))),
        'whatsapp_typing_chars_per_second': float(db.get_setting('whatsapp_typing_chars_per_second',
                                                                  str(config.WHATSAPP_TYPING_CHARS_PER_SECOND))),
        'whatsapp_max_typing_delay_seconds': float(db.get_setting('whatsapp_max_typing_delay_seconds',
                                                                   str(config.WHATSAPP_MAX_TYPING_DELAY_SECONDS))),
        'whatsapp_delay_jitter_ratio': float(db.get_setting('whatsapp_delay_jitter_ratio',
                                                            str(config.WHATSAPP_DELAY_JITTER_RATIO))),
        'whatsapp_max_outbound_per_minute': int(db.get_setting('whatsapp_max_outbound_per_minute',
                                                               str(config.WHATSAPP_MAX_OUTBOUND_PER_MINUTE))),
        'whatsapp_natural_formatting_enabled': db.get_setting('whatsapp_natural_formatting_enabled',
                                                               '1' if config.WHATSAPP_NATURAL_FORMATTING_ENABLED else '0') == '1',
    })


# ---- Batch settings operations ----

@settings_bp.route('/api/settings/batch', methods=['POST'])
def api_batch_save():
    """Save multiple settings at once."""
    from models.db import db

    data = request.get_json()
    if not data or 'settings' not in data:
        return jsonify({'success': False, 'error': 'Missing "settings" object'}), 400

    settings = data['settings']
    results = {}
    errors = []

    # Agent Timeout Retries
    if 'agent_timeout_retries' in settings:
        try:
            value = max(0, int(settings['agent_timeout_retries']))
            db.set_setting('agent_timeout_retries', str(value))
            results['agent_timeout_retries'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'agent_timeout_retries: {e}')

    # LLM Max Retries
    if 'llm_max_retries' in settings:
        try:
            value = max(0, int(settings['llm_max_retries']))
            db.set_setting('llm_max_retries', str(value))
            results['llm_max_retries'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'llm_max_retries: {e}')

    # Max Concurrent per Agent
    if 'max_concurrent_llm_per_agent' in settings:
        try:
            value = max(0, int(settings['max_concurrent_llm_per_agent']))
            db.set_setting('max_concurrent_llm_per_agent', str(value))
            try:
                from backend.agent_runtime.runtime import AgentRuntime
                if AgentRuntime._concurrency_mgr:
                    AgentRuntime._concurrency_mgr.refresh_agent_limit()
            except Exception:
                pass
            results['max_concurrent_llm_per_agent'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'max_concurrent_llm_per_agent: {e}')

    # Max Concurrent per Model
    if 'max_concurrent_llm_per_model' in settings:
        try:
            value = max(0, int(settings['max_concurrent_llm_per_model']))
            db.set_setting('max_concurrent_llm_per_model', str(value))
            try:
                from backend.agent_runtime.runtime import AgentRuntime
                if AgentRuntime._concurrency_mgr:
                    AgentRuntime._concurrency_mgr.refresh_all_model_limits()
            except Exception:
                pass
            results['max_concurrent_llm_per_model'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'max_concurrent_llm_per_model: {e}')

    # Max Concurrent LLM (Global) — controls _llm_lock BoundedSemaphore
    if 'max_concurrent_llm_global' in settings:
        try:
            value = max(1, int(settings['max_concurrent_llm_global']))
            db.set_setting('max_concurrent_llm_global', str(value))
            try:
                from backend.agent_runtime.runtime import AgentRuntime
                AgentRuntime._llm_serializer.refresh_llm_global_limit()
            except Exception:
                pass
            results['max_concurrent_llm_global'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'max_concurrent_llm_global: {e}')

    # Agent Queue Workers
    if 'agent_queue_workers' in settings:
        try:
            raw_value = int(settings['agent_queue_workers'])
            value = max(1, min(32, raw_value))
            db.set_setting('agent_queue_workers', str(value))
            try:
                from backend.agent_runtime import agent_runtime
                agent_runtime.resize_workers(value)
            except Exception:
                pass
            results['agent_queue_workers'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'agent_queue_workers: {e}')

    # Max Tool Iterations
    if 'max_tool_iterations' in settings:
        try:
            raw_value = int(settings['max_tool_iterations'])
            value = max(1, min(1000, raw_value))
            db.set_setting('max_tool_iterations', str(value))
            results['max_tool_iterations'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'max_tool_iterations: {e}')

    # Agent Sidebar Limit
    if 'agent_sidebar_limit' in settings:
        try:
            raw_value = int(settings['agent_sidebar_limit'])
            value = max(1, min(500, raw_value))
            db.set_setting('agent_sidebar_limit', str(value))
            results['agent_sidebar_limit'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'agent_sidebar_limit: {e}')

    # Theme
    if 'theme' in settings:
        theme = settings['theme']
        if theme not in ('light', 'dark', 'system'):
            theme = 'system'
        db.set_setting('theme', theme)
        results['theme'] = theme

    # Long-Running Command Guard
    if 'long_running_guard_enabled' in settings:
        try:
            enabled = '1' if settings['long_running_guard_enabled'] else '0'
            db.set_setting('long_running_guard_enabled', enabled)
            results['long_running_guard_enabled'] = enabled == '1'
        except (ValueError, TypeError) as e:
            errors.append(f'long_running_guard_enabled: {e}')

    # Message Wrapper
    if 'message_wrapper_enabled' in settings:
        try:
            enabled = '1' if settings['message_wrapper_enabled'] else '0'
            db.set_setting('message_wrapper_enabled', enabled)
            results['message_wrapper_enabled'] = enabled == '1'
        except (ValueError, TypeError) as e:
            errors.append(f'message_wrapper_enabled: {e}')

    # ── WhatsApp Safe Delivery (global) ──
    _whatsapp_bool_keys = ('whatsapp_safe_delivery_enabled', 'whatsapp_natural_formatting_enabled')
    for key in _whatsapp_bool_keys:
        if key in settings:
            try:
                enabled = '1' if _coerce_boolean(settings[key]) else '0'
                db.set_setting(key, enabled)
                results[key] = enabled == '1'
            except (ValueError, TypeError) as e:
                errors.append(f'{key}: {e}')

    _whatsapp_float_keys = (
        ('whatsapp_pool_window_seconds', 0.1, 30.0),
        ('whatsapp_min_send_interval_seconds', 0.1, 60.0),
        ('whatsapp_typing_chars_per_second', 1.0, 100.0),
        ('whatsapp_max_typing_delay_seconds', 1.0, 120.0),
        ('whatsapp_delay_jitter_ratio', 0.0, 0.5),
    )
    for key, lo, hi in _whatsapp_float_keys:
        if key in settings:
            try:
                value = float(settings[key])
                if not math.isfinite(value):
                    raise ValueError('must be a finite number')
                value = max(lo, min(hi, value))
                db.set_setting(key, str(value))
                results[key] = value
            except (ValueError, TypeError) as e:
                errors.append(f'{key}: {e}')

    if 'whatsapp_max_outbound_per_minute' in settings:
        try:
            value = max(1, min(600, int(settings['whatsapp_max_outbound_per_minute'])))
            db.set_setting('whatsapp_max_outbound_per_minute', str(value))
            results['whatsapp_max_outbound_per_minute'] = value
        except (ValueError, TypeError) as e:
            errors.append(f'whatsapp_max_outbound_per_minute: {e}')

    # Default Model
    if 'default_model_id' in settings:
        model_id = settings['default_model_id']
        if model_id:
            model = db.get_model_by_id(model_id)
            if model:
                with db._connect() as conn:
                    conn.execute("UPDATE llm_models SET is_default = 0")
                    conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = ?", (model['id'],))
                    conn.commit()
                results['default_model_id'] = model['id']
            else:
                errors.append('default_model_id: Model not found')

    # Global default-model fallback. An empty value disables the fallback.
    if 'default_model_fallback_id' in settings:
        model_id = settings['default_model_fallback_id'] or ''
        if model_id:
            model = db.get_model_by_id(model_id)
            if model:
                db.set_setting('default_model_fallback_id', model['id'])
                results['default_model_fallback_id'] = model['id']
            else:
                errors.append('default_model_fallback_id: Model not found')
        else:
            db.set_setting('default_model_fallback_id', '')
            results['default_model_fallback_id'] = ''

    # Vision routing chain (primary + two fallbacks)
    vision_setting_keys = (
        'vision_model_id',
        'vision_fallback_model_id',
        'vision_fallback_model_2_id',
    )
    if any(key in settings for key in vision_setting_keys):
        vision_ids = {
            key: settings.get(key, db.get_setting(key, ''))
            for key in vision_setting_keys
        }
        duplicate_vision_ids = {
            model_id for model_id in vision_ids.values() if model_id
            and list(vision_ids.values()).count(model_id) > 1
        }
        for key in vision_setting_keys:
            if key not in settings:
                continue
            model_id = vision_ids[key]
            if model_id in duplicate_vision_ids:
                errors.append(f'{key}: Must differ from the other configured vision models')
                continue
            if model_id:
                model = db.get_model_by_id(model_id)
                if model and model.get('vision_supported'):
                    db.set_setting(key, model['id'])
                    results[key] = model['id']
                elif model:
                    errors.append(f'{key}: Model does not support vision')
                else:
                    errors.append(f'{key}: Model not found')
            else:
                db.set_setting(key, '')
                results[key] = ''

    # KB Organizer Model — global default for the KB organizer background sub-agent
    if 'kb_organizer_model_id' in settings:
        kb_organizer_model_id = settings['kb_organizer_model_id']
        if kb_organizer_model_id:
            model = db.get_model_by_id(kb_organizer_model_id)
            if model:
                db.set_setting('kb_organizer_model_id', model['id'])
                results['kb_organizer_model_id'] = model['id']
            else:
                errors.append('kb_organizer_model_id: Model not found')
        else:
            # Allow clearing the setting (falls back to env / agent default)
            db.set_setting('kb_organizer_model_id', '')
            results['kb_organizer_model_id'] = ''

    # KB Organizer nightly schedule — global time for the Vault Janitor.
    if 'kb_organizer_nightly_time' in settings:
        value = str(settings['kb_organizer_nightly_time']).strip()
        try:
            from backend.scheduler import scheduler
            scheduler.refresh_kb_organizer_schedule(value)
            old_value = db.get_setting('kb_organizer_nightly_time', '')
            db.set_setting('kb_organizer_nightly_time', value)
            _audit_setting_change('kb_organizer_nightly_time', old_value, value)
            results['kb_organizer_nightly_time'] = value
        except ValueError as e:
            errors.append(f'kb_organizer_nightly_time: {e}')

    if errors:
        return jsonify({
            'success': True,
            'partial': True,
            'results': results,
            'errors': errors
        })

    return jsonify({'success': True, 'results': results})


# ---- User Management (Admin) ----


@settings_bp.route('/api/settings/users', methods=['GET'])
def api_list_users():
    """List all users with optional status filter.

    Query params:
        filter: all | approved | blocked | pending  (default: all)
        limit: int (default: 50)
        offset: int (default: 0)
    """
    status_filter = request.args.get('filter', 'all')
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = max(int(request.args.get('offset', 0)), 0)

    with db._connect() as conn:
        conn.row_factory = db._row_factory
        cursor = conn.cursor()

        conditions = ['u.deleted_at IS NULL']
        params = []

        if status_filter == 'approved':
            conditions.append('u.is_approved = 1 AND u.blocked_at IS NULL')
        elif status_filter == 'blocked':
            conditions.append('u.is_approved = 2')
        elif status_filter == 'pending':
            conditions.append('(u.is_approved = 0 OR u.is_approved IS NULL)')

        where = ' AND '.join(conditions)

        cursor.execute(f"""
            SELECT u.*,
                   (SELECT COUNT(*) FROM user_audit_log WHERE user_id = u.id AND action IN ('blocked', 'unblocked')) as audit_count
            FROM users u
            WHERE {where}
            ORDER BY u.last_active_at DESC NULLS LAST
            LIMIT ? OFFSET ?
        """, params + [limit, offset])

        users = [dict(r) for r in cursor.fetchall()]

        # Count total for pagination
        cursor.execute(f'SELECT COUNT(*) FROM users u WHERE {where}', params)
        total = cursor.fetchone()[0]

    return jsonify({'users': users, 'total': total, 'limit': limit, 'offset': offset})


@settings_bp.route('/api/admin/blocked-users', methods=['GET'])
def api_blocked_users():
    """List all blocked users (dedicated endpoint)."""
    with db._connect() as conn:
        conn.row_factory = db._row_factory
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.*,
                   (SELECT created_at FROM user_audit_log
                    WHERE user_id = u.id AND action = 'blocked'
                    ORDER BY created_at DESC LIMIT 1) as blocked_at_audit,
                   (SELECT actor_id FROM user_audit_log
                    WHERE user_id = u.id AND action = 'blocked'
                    ORDER BY created_at DESC LIMIT 1) as blocked_by
            FROM users u
            WHERE u.is_approved = 2 AND u.deleted_at IS NULL
            ORDER BY u.blocked_at DESC
        """)
        users = [dict(r) for r in cursor.fetchall()]

    return jsonify({'users': users, 'total': len(users)})


@settings_bp.route('/api/settings/users/<user_id>/block', methods=['POST'])
def api_block_user(user_id):
    """Block a user by ID."""
    data = request.get_json() or {}
    reason = data.get('reason', '')
    ok = db.block_user(user_id, reason=reason, actor_type='admin', actor_id='web_admin')
    if ok:
        audit.log_user_management(user_id='admin', target_user=user_id, action='block', ip=request.remote_addr or '', detail=reason)
        return jsonify({'success': True, 'user_id': user_id})
    return jsonify({'error': 'User not found or already blocked'}), 404


@settings_bp.route('/api/settings/users/<user_id>/unblock', methods=['POST'])
def api_unblock_user(user_id):
    """Unblock a user by ID."""
    ok = db.unblock_user(user_id, actor_type='admin', actor_id='web_admin')
    if ok:
        audit.log_user_management(user_id='admin', target_user=user_id, action='unblock', ip=request.remote_addr or '')
        return jsonify({'success': True, 'user_id': user_id})
    return jsonify({'error': 'User not found or not blocked'}), 404


@settings_bp.route('/api/settings/users/<user_id>/audit', methods=['GET'])
def api_user_audit(user_id):
    """Get audit log for a specific user."""
    logs = db.get_audit_log(user_id=user_id)
    return jsonify({'user_id': user_id, 'audit_logs': logs})



# ==================== Shared Channels (System Settings → Shared Channel) ====================
# Shared channels serve multiple agents from one connection (agent_id IS NULL);
# inbound senders are routed per-user/group via config.routes and unknown
# senders land in shared_channel_inbox for capture-and-assign.

_SHARED_INBOX_RETENTION_KEY = 'shared_channel_inbox_retention_hours'
_SHARED_INBOX_RETENTION_DEFAULT_HOURS = 24
_SHARED_INBOX_RETENTION_MIN_HOURS = 1
_SHARED_INBOX_RETENTION_MAX_HOURS = 24 * 365


def _shared_inbox_retention_hours() -> int:
    """Return the configured inbox retention, falling back safely to 24 hours."""
    try:
        value = int(db.get_setting(
            _SHARED_INBOX_RETENTION_KEY,
            str(_SHARED_INBOX_RETENTION_DEFAULT_HOURS),
        ))
        return max(_SHARED_INBOX_RETENTION_MIN_HOURS,
                   min(_SHARED_INBOX_RETENTION_MAX_HOURS, value))
    except (TypeError, ValueError):
        return _SHARED_INBOX_RETENTION_DEFAULT_HOURS


_SHARED_ACCESS_MODES = {'assigned_only', 'unrestricted'}


def _validate_shared_access_config(channel, data):
    """Return validated access settings, or an API error response tuple."""
    config = dict(channel.get('config') or {})
    mode = data.get('access_mode', config.get('access_mode', 'assigned_only'))
    default_agent_id = data.get(
        'default_agent_id', config.get('default_agent_id') or '')
    if mode not in _SHARED_ACCESS_MODES:
        return None, (jsonify({
            'error': 'access_mode must be assigned_only or unrestricted'}), 400)
    if mode == 'unrestricted':
        agent = db.get_agent(default_agent_id)
        if not agent or not agent.get('enabled'):
            return None, (jsonify({
                'error': 'default_agent_id must identify an enabled agent'}), 400)
    return {'access_mode': mode, 'default_agent_id': default_agent_id}, None

def _shared_channel_or_404(channel_id):
    channel = db.get_channel(channel_id)
    if not channel or channel.get('agent_id') is not None:
        return None
    return channel


@settings_bp.route('/api/shared-channels/settings', methods=['GET'])
def api_get_shared_channel_settings():
    """Return global settings that apply to every shared channel."""
    return jsonify({
        'unassigned_sender_retention_hours': _shared_inbox_retention_hours(),
    })


@settings_bp.route('/api/shared-channels/settings', methods=['PUT'])
def api_update_shared_channel_settings():
    """Persist validated global unassigned-sender retention."""
    data = request.get_json() or {}
    if 'unassigned_sender_retention_hours' not in data:
        return jsonify({'error': 'unassigned_sender_retention_hours is required'}), 400
    try:
        hours = int(data['unassigned_sender_retention_hours'])
    except (TypeError, ValueError):
        return jsonify({'error': 'unassigned_sender_retention_hours must be an integer'}), 400
    if not _SHARED_INBOX_RETENTION_MIN_HOURS <= hours <= _SHARED_INBOX_RETENTION_MAX_HOURS:
        return jsonify({'error': 'unassigned_sender_retention_hours must be between 1 and 8760'}), 400
    old_value = db.get_setting(
        _SHARED_INBOX_RETENTION_KEY, str(_SHARED_INBOX_RETENTION_DEFAULT_HOURS))
    db.set_setting(_SHARED_INBOX_RETENTION_KEY, str(hours))
    _audit_setting_change(_SHARED_INBOX_RETENTION_KEY, old_value, hours)
    db.cleanup_expired_inbox_entries(hours)
    return jsonify({'success': True, 'unassigned_sender_retention_hours': hours})


@settings_bp.route('/api/shared-channels', methods=['GET'])
def api_list_shared_channels():
    from backend.channels.registry import channel_manager
    db.cleanup_expired_inbox_entries(_shared_inbox_retention_hours())
    channels = db.get_shared_channels()
    for ch in channels:
        ch['running'] = channel_manager.is_running(ch['id'])
        ch['bridge_status'] = None
        ch['inbox_count'] = len(db.get_inbox(ch['id']))
        if ch['running']:
            instance = channel_manager.get_channel_instance(ch['id'])
            if instance:
                try:
                    ch['bridge_status'] = instance.get_bridge_status().get('status')
                except Exception:
                    pass
    return jsonify({'channels': channels})


@settings_bp.route('/api/shared-channels', methods=['POST'])
def api_create_shared_channel():
    from backend.channels.registry import channel_manager
    data = request.get_json() or {}
    name = (data.get('name') or 'Shared WhatsApp').strip()
    # UNIQUE(agent_id, name) treats NULLs as distinct — enforce app-side
    if any(c.get('name') == name for c in db.get_shared_channels()):
        return jsonify({'error': f"Shared channel '{name}' already exists"}), 409
    chan_id = db.create_channel({
        'agent_id': None,
        'type': 'whatsapp_shared',
        'name': name,
        'config': {'mode': 'open', 'access_mode': 'assigned_only',
                   'default_agent_id': '', 'routes': {}},
    })
    try:
        channel_manager.start_channel(chan_id)
    except Exception as e:
        _logger.error("Auto-start failed for shared channel %s: %s", chan_id, e)
    channel = db.get_channel(chan_id)
    channel['running'] = channel_manager.is_running(chan_id)
    audit.log_setting_change(user_id='admin', key='shared_channel.create',
                             old_value='', new_value=name, ip=request.remote_addr or '')
    return jsonify({'success': True, 'channel': channel})


@settings_bp.route('/api/shared-channels/<channel_id>', methods=['PUT'])
def api_update_shared_channel(channel_id):
    from backend.channels.registry import channel_manager
    channel = _shared_channel_or_404(channel_id)
    if not channel:
        return jsonify({'error': 'Shared channel not found'}), 404
    data = request.get_json() or {}
    access, error = _validate_shared_access_config(channel, data)
    if error:
        return error
    updates = {k: v for k, v in data.items() if k in ('name', 'enabled')}
    if 'access_mode' in data or 'default_agent_id' in data:
        config = dict(channel.get('config') or {})
        config.update(access)
        updates['config'] = config
    if updates:
        db.update_channel(channel_id, updates)
    if 'enabled' in data:
        try:
            if data['enabled']:
                channel_manager.start_channel(channel_id)
            else:
                channel_manager.stop_channel(channel_id)
        except Exception as e:
            _logger.error("Toggle failed for shared channel %s: %s", channel_id, e)
    return jsonify({'success': True, 'running': channel_manager.is_running(channel_id)})


@settings_bp.route('/api/shared-channels/<channel_id>', methods=['DELETE'])
def api_delete_shared_channel(channel_id):
    from backend.channels.registry import channel_manager
    channel = _shared_channel_or_404(channel_id)
    if not channel:
        return jsonify({'error': 'Shared channel not found'}), 404
    try:
        channel_manager.stop_channel(channel_id)
    except Exception:
        pass
    db.delete_channel(channel_id)
    audit.log_setting_change(user_id='admin', key='shared_channel.delete',
                             old_value=channel.get('name') or channel_id, new_value='',
                             ip=request.remote_addr or '')
    return jsonify({'success': True})


@settings_bp.route('/api/shared-channels/<channel_id>/qr', methods=['GET'])
def api_shared_channel_qr(channel_id):
    from backend.channels.registry import channel_manager
    from backend.channels.whatsapp import WhatsAppChannel
    instance = channel_manager.get_channel_instance(channel_id)
    if not isinstance(instance, WhatsAppChannel):
        return jsonify({'error': 'Shared channel not running'}), 404
    return jsonify(instance.get_qr())


@settings_bp.route('/api/shared-channels/<channel_id>/bridge-status', methods=['GET'])
def api_shared_channel_bridge_status(channel_id):
    from backend.channels.registry import channel_manager
    from backend.channels.whatsapp import WhatsAppChannel
    instance = channel_manager.get_channel_instance(channel_id)
    if not isinstance(instance, WhatsAppChannel):
        return jsonify({'status': 'not_running'})
    return jsonify(instance.get_bridge_status())


def _add_shared_route(channel, user_id, agent_id, display_name='', alt_user_id=''):
    """Write route entries (and optional annotation) into the channel config.
    Routes both identifier namespaces when the alternate is known."""
    config = channel.get('config') or {}
    routes = config.get('routes') or {}
    routes[user_id] = agent_id
    if alt_user_id:
        routes[alt_user_id] = agent_id
    config['routes'] = routes
    if display_name:
        names = config.get('user_names') or {}
        names[user_id] = display_name
        if alt_user_id:
            names[alt_user_id] = display_name
        config['user_names'] = names
    db.update_channel(channel['id'], {'config': config})


@settings_bp.route('/api/shared-channels/<channel_id>/routes', methods=['POST'])
def api_add_shared_route(channel_id):
    channel = _shared_channel_or_404(channel_id)
    if not channel:
        return jsonify({'error': 'Shared channel not found'}), 404
    data = request.get_json() or {}
    user_id = re.sub(r'[+\s-]', '', str(data.get('user_id') or ''))
    agent_id = data.get('agent_id') or ''
    if not user_id or not user_id.isdigit():
        return jsonify({'error': 'user_id must be digits (phone number or group ID)'}), 400
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    _add_shared_route(channel, user_id, agent_id, data.get('name') or '')
    return jsonify({'success': True})


@settings_bp.route('/api/shared-channels/<channel_id>/routes/<user_id>', methods=['DELETE'])
def api_delete_shared_route(channel_id, user_id):
    channel = _shared_channel_or_404(channel_id)
    if not channel:
        return jsonify({'error': 'Shared channel not found'}), 404
    config = channel.get('config') or {}
    routes = config.get('routes') or {}
    if user_id not in routes:
        return jsonify({'error': 'Route not found'}), 404
    del routes[user_id]
    config['routes'] = routes

    # Names annotate active routes only. Remove the selected name plus any
    # historical/orphaned metadata so stale contacts cannot reappear later.
    names = config.get('user_names')
    if isinstance(names, dict):
        config['user_names'] = {
            route_id: display_name
            for route_id, display_name in names.items()
            if route_id in routes
        }

    db.update_channel(channel_id, {'config': config})
    return jsonify({'success': True})


@settings_bp.route('/api/shared-channels/<channel_id>/inbox', methods=['GET'])
def api_shared_channel_inbox(channel_id):
    if not _shared_channel_or_404(channel_id):
        return jsonify({'error': 'Shared channel not found'}), 404
    db.cleanup_expired_inbox_entries(_shared_inbox_retention_hours())
    return jsonify({'inbox': db.get_inbox(channel_id)})


@settings_bp.route('/api/shared-channels/<channel_id>/inbox/<entry_id>/assign', methods=['POST'])
def api_assign_inbox_entry(channel_id, entry_id):
    channel = _shared_channel_or_404(channel_id)
    if not channel:
        return jsonify({'error': 'Shared channel not found'}), 404
    entry = db.get_inbox_entry(entry_id)
    if not entry or entry.get('channel_id') != channel_id:
        return jsonify({'error': 'Inbox entry not found'}), 404
    data = request.get_json() or {}
    agent_id = data.get('agent_id') or ''
    if not db.get_agent(agent_id):
        return jsonify({'error': 'Agent not found'}), 404
    display_name = data.get('name') or entry.get('push_name') or ''
    _add_shared_route(channel, entry['external_user_id'], agent_id,
                      display_name, entry.get('alt_user_id') or '')
    db.delete_inbox_entry(entry_id)
    return jsonify({'success': True})


@settings_bp.route('/api/shared-channels/<channel_id>/inbox/<entry_id>', methods=['DELETE'])
def api_dismiss_inbox_entry(channel_id, entry_id):
    if not _shared_channel_or_404(channel_id):
        return jsonify({'error': 'Shared channel not found'}), 404
    if not db.delete_inbox_entry(entry_id):
        return jsonify({'error': 'Inbox entry not found'}), 404
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Debug Listener — SSE endpoint for real-time WhatsApp inbound monitoring
# ---------------------------------------------------------------------------

@settings_bp.route('/api/shared-channels/debug/listen', methods=['GET'])
def api_shared_channel_debug_listen():
    """SSE endpoint: push all whatsapp_inbound events to the client in real-time."""
    from backend.event_stream import event_stream

    q = queue.Queue(maxsize=500)

    def handler(data):
        try:
            q.put_nowait(('whatsapp_inbound', data, None))
        except queue.Full:
            pass  # drop oldest; queue bounded at 500

    event_stream.on('whatsapp_inbound', handler)

    def generate():
        # Send initial connected event so the client knows it's live
        yield (
            f"event: connected\n"
            f"data: {json.dumps({'type': 'connected', 'message': 'Listening for WhatsApp inbound messages...'})}\n\n"
        )
        try:
            while True:
                try:
                    item = q.get(timeout=30)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                sse_event, payload, _seq = item
                yield f"event: {sse_event}\ndata: {json.dumps(payload)}\n\n"
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
