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
