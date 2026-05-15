import logging
import os
import re
from typing import Dict, Any

from flask import Blueprint, render_template, jsonify, request

import config
from models.db import db

_logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__)

_SENSITIVE_MODEL_KEYS = frozenset({'api_key'})


def _sanitize_model(model: Dict[str, Any]) -> Dict[str, Any]:
    """Strip sensitive fields (api_key) from a model dict before API response."""
    for key in _SENSITIVE_MODEL_KEYS:
        model.pop(key, None)
    return model


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
    # Built-in tools always appear first
    tools = tool_registry.get_builtin_tool_defs()
    tools += test_manager.list_tools()
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
        db.set_setting('two_pass_enabled', enabled)
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
        db.set_setting('public_history', enabled)
        return jsonify({'success': True, 'enabled': enabled == '1'})
    val = db.get_setting('public_history', '0')
    return jsonify({'enabled': val == '1'})


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
        db.set_setting('events_dispatch_enabled', enabled)
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
        db.set_setting('theme', theme)
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
        db.set_setting('task_classifier_enabled', enabled)
        db.set_setting('task_classifier_model_id', model_id)
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
        with db._connect() as conn:
            conn.execute("UPDATE llm_models SET is_default = 0")
            conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = ?", (model_id,))
            conn.commit()
        return jsonify({'success': True, 'model': _sanitize_model(db.get_default_model())})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- General settings bulk read ----

@settings_bp.route('/api/settings/general', methods=['GET'])
def api_get_general_settings():
    """Return all general-tab settings in a single response."""
    return jsonify({
        'public_history': db.get_setting('public_history', '0') == '1',
        'agent_timeout_retries': int(db.get_setting('agent_timeout_retries', str(config.AGENT_TIMEOUT_RETRIES))),
        'llm_max_retries': int(db.get_setting('llm_max_retries', '5')),
        'max_concurrent_llm_per_agent': int(db.get_setting('max_concurrent_llm_per_agent', '1')),
        'max_concurrent_llm_per_model': int(db.get_setting('max_concurrent_llm_per_model', '0')),
        'agent_queue_workers': int(db.get_setting('agent_queue_workers', str(config.AGENT_QUEUE_WORKERS))),
        'max_tool_iterations': int(db.get_setting('max_tool_iterations', str(config.AGENT_MAX_TOOL_ITERATIONS))),
        'theme': db.get_setting('theme', 'system'),
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

    # Theme
    if 'theme' in settings:
        theme = settings['theme']
        if theme not in ('light', 'dark', 'system'):
            theme = 'system'
        db.set_setting('theme', theme)
        results['theme'] = theme

    # Default Model
    if 'default_model_id' in settings:
        model_id = settings['default_model_id']
        if model_id:
            model = db.get_model_by_id(model_id)
            if model:
                with db._connect() as conn:
                    conn.execute("UPDATE llm_models SET is_default = 0")
                    conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = ?", (model_id,))
                    conn.commit()
                results['default_model_id'] = model_id
            else:
                errors.append('default_model_id: Model not found')

    if errors:
        return jsonify({
            'success': True,
            'partial': True,
            'results': results,
            'errors': errors
        })

    return jsonify({'success': True, 'results': results})

