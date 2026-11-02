from flask import Blueprint, Flask

from backend.plugin_lifecycle import PluginManager


def test_get_plugin_endpoints_lists_only_blueprint_routes():
    manager = PluginManager.__new__(PluginManager)
    blueprint = Blueprint('sample_plugin', __name__, url_prefix='/plugin/sample')

    @blueprint.route('/items', methods=['GET', 'POST'])
    def items():
        return ''

    app = Flask(__name__)
    app.register_blueprint(blueprint)
    manager._blueprints = {'sample': blueprint}

    with app.app_context():
        endpoints = manager.get_plugin_endpoints('sample')

    assert endpoints == [{
        'path': '/plugin/sample/items',
        'methods': ['GET', 'POST'],
        'endpoint': 'sample_plugin.items',
    }]


def test_get_plugin_endpoints_returns_empty_for_plugin_without_blueprint():
    manager = PluginManager.__new__(PluginManager)
    manager._blueprints = {}

    assert manager.get_plugin_endpoints('missing') == []


def _manager_with_plugin(tmp_path, monkeypatch, manifest, tools=None, implemented=()):
    import json
    from backend import plugin_lifecycle

    plugin_dir = tmp_path / 'sample'
    (plugin_dir / 'backend' / 'tools').mkdir(parents=True, exist_ok=True)
    (plugin_dir / 'plugin.json').write_text(json.dumps(manifest))
    if tools is not None:
        (plugin_dir / 'tools.json').write_text(json.dumps(tools))
    for name in implemented:
        (plugin_dir / 'backend' / 'tools' / f'{name}.py').write_text('def execute(agent, args):\n    return {}\n')
    monkeypatch.setattr(plugin_lifecycle, 'PLUGINS_DIR', str(tmp_path))
    manager = PluginManager.__new__(PluginManager)
    manager._json_file_cache = {}
    return manager


def test_get_plugin_tools_lists_declared_tools_with_their_parameters(tmp_path, monkeypatch):
    tools = [{'type': 'function', 'function': {
        'name': 'set_note', 'description': 'Save a note.',
        'parameters': {'type': 'object', 'required': ['text'], 'properties': {
            'text': {'type': 'string', 'description': 'The note.'},
            'kind': {'type': 'string', 'enum': ['a', 'b']},
            'tags': {'type': 'array', 'items': {'type': 'integer'}}}}}},
        {'type': 'function', 'function': {'name': 'list_notes', 'description': 'List notes.'}},
        {'type': 'function', 'function': {}}]
    manager = _manager_with_plugin(tmp_path, monkeypatch, {'id': 'sample', 'tools_file': 'tools.json'}, tools, implemented=['set_note'])

    found = manager.get_plugin_tools('sample')

    assert [t['id'] for t in found] == ['plugin:sample:set_note', 'plugin:sample:list_notes']
    assert found[0]['implemented'] is True and found[1]['implemented'] is False
    assert found[0]['parameters'] == [
        {'name': 'text', 'type': 'string', 'description': 'The note.', 'enum': None, 'items': None, 'required': True},
        {'name': 'kind', 'type': 'string', 'description': '', 'enum': ['a', 'b'], 'items': None, 'required': False},
        {'name': 'tags', 'type': 'array', 'description': '', 'enum': None, 'items': 'integer', 'required': False}]
    assert found[1]['parameters'] == []


def test_get_plugin_tools_is_empty_without_a_tools_file_or_with_a_broken_one(tmp_path, monkeypatch):
    assert _manager_with_plugin(tmp_path, monkeypatch, {'id': 'sample'}).get_plugin_tools('sample') == []
    assert _manager_with_plugin(tmp_path, monkeypatch, {'id': 'sample', 'tools_file': 'missing.json'}).get_plugin_tools('sample') == []
    assert _manager_with_plugin(tmp_path, monkeypatch, {'id': 'sample', 'tools_file': 'tools.json'}, {'not': 'a list'}).get_plugin_tools('sample') == []
    assert PluginManager.__new__(PluginManager).get_plugin_tools('no-such-plugin') == []


def test_plugin_detail_page_has_endpoints_and_tools_tabs():
    import os
    html = open(os.path.join(os.path.dirname(__file__), '..', 'templates', 'plugin_detail.html'), encoding='utf-8').read()
    for tab in ('endpoints', 'tools'):
        assert f"showTab('{tab}')" in html and f'id="tab-{tab}"' in html
    assert html.index('id="tab-endpoints"') < html.index('id="endpoints-list"') < html.index('id="tab-tools"') < html.index('id="tools-list"')
    assert html.index('id="tab-settings"') < html.index('id="events-list"') < html.index('id="tab-endpoints"')
