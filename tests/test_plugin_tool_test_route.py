"""Running a plugin's tool from its detail page, the way an agent would call it."""
import json

import pytest
from flask import Flask

from backend.plugin_manager import plugin_manager
from routes.plugins import plugins_bp


TOOL = '''
def execute(agent, args):
    return {"caller": agent.get("id"), "args": args}
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    tool_file = tmp_path / 'echo_args.py'
    tool_file.write_text(TOOL)
    state = {'enabled': True}

    def get_plugin(plugin_id):
        if plugin_id != 'sample':
            return None
        return {'id': 'sample', 'enabled': state['enabled'],
                'tools': [{'id': 'plugin:sample:echo_args', 'name': 'echo_args'}]}

    monkeypatch.setattr(plugin_manager, 'get_plugin', get_plugin)
    monkeypatch.setattr(plugin_manager, 'find_plugin_tool_backend',
                        lambda name, plugin_id=None: (str(tool_file), 'sample') if name == 'echo_args' else (None, None))
    app = Flask(__name__)
    app.register_blueprint(plugins_bp)
    test_client = app.test_client()
    test_client.state = state
    return test_client


def run(client, tool='echo_args', plugin='sample', **body):
    return client.post(f'/api/plugins/{plugin}/tools/{tool}/test', data=json.dumps(body), content_type='application/json')


def test_the_tool_runs_with_the_arguments_and_sees_the_chosen_agent_as_its_caller(client):
    res = run(client, args={'text': 'hi'}, agent_id='aisyah')
    assert res.status_code == 200
    assert res.get_json() == {'result': {'caller': 'aisyah', 'args': {'text': 'hi'}}}


def test_the_caller_defaults_to_a_test_agent_and_no_arguments_is_fine(client):
    assert run(client).get_json() == {'result': {'caller': 'test', 'args': {}}}


def test_only_a_tool_the_plugin_declares_can_be_run(client):
    assert run(client, tool='other').status_code == 404
    assert run(client, plugin='missing').status_code == 404


def test_a_disabled_plugin_is_not_run(client):
    client.state['enabled'] = False
    res = run(client)
    assert res.status_code == 400 and 'Enable the plugin' in res.get_json()['error']


def test_arguments_must_be_a_json_object(client):
    res = run(client, args=['not', 'an', 'object'])
    assert res.status_code == 400 and 'object' in res.get_json()['error']
