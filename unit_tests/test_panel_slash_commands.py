"""Coverage for panel actions assigned their own slash command."""

import importlib.util
import os
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backend.slash_commands import SlashCommand, command_registry, execute_command
from plugins.panel.db import PanelDB, validate_slash_command


def _load_panel_routes():
    """Load plugins/panel/routes.py the way plugin_lifecycle does (synthetic module)."""
    path = os.path.join(BASE_DIR, 'plugins', 'panel', 'routes.py')
    spec = importlib.util.spec_from_file_location('panel_routes_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def panel_db(tmp_path, monkeypatch):
    """Point PanelDB at a temp database for the duration of a test."""
    db_path = str(tmp_path / 'panel.db')
    monkeypatch.setattr('plugins.panel.db.DB_PATH', db_path)
    original_init = PanelDB.__init__

    def _init(self, agent_id, db_path=db_path):
        original_init(self, agent_id, db_path)

    monkeypatch.setattr(PanelDB, '__init__', _init)
    return db_path


def _add(agent_id, label, slash_command='', **kwargs):
    return PanelDB(agent_id).add_action(
        label=label,
        action_type=kwargs.pop('action_type', 'script'),
        content=kwargs.pop('content', 'echo hi'),
        slash_command=slash_command,
        **kwargs,
    )


# ── storage ──────────────────────────────────────────────────────────


def test_slash_command_round_trips(panel_db):
    action = _add('agent-a', 'Deploy', slash_command='deploy')
    assert action['slash_command'] == 'deploy'
    assert PanelDB('agent-a').find_by_slash_command('deploy')['id'] == action['id']


def test_find_by_slash_command_ignores_disabled(panel_db):
    action = _add('agent-a', 'Deploy', slash_command='deploy')
    PanelDB('agent-a').update_action(action['id'], enabled=False)
    assert PanelDB('agent-a').find_by_slash_command('deploy') is None


# ── validation ───────────────────────────────────────────────────────


def test_validation_normalizes_and_accepts(panel_db):
    assert validate_slash_command('agent-a', ' /Deploy ') == ('deploy', None)
    assert validate_slash_command('agent-a', '') == ('', None)


@pytest.mark.parametrize('bad', ['1deploy', 'de ploy', 'deploy!', 'a' * 33])
def test_validation_rejects_bad_syntax(panel_db, bad):
    name, error = validate_slash_command('agent-a', bad)
    assert name is None and 'Invalid slash_command' in error


def test_validation_rejects_builtin_collision(panel_db):
    name, error = validate_slash_command('agent-a', 'clear')
    assert name is None and 'built-in command' in error


def test_validation_rejects_duplicate_within_agent(panel_db):
    existing = _add('agent-a', 'Deploy', slash_command='deploy')
    name, error = validate_slash_command('agent-a', 'deploy')
    assert name is None and 'already assigned' in error
    # Re-validating for the same action (an update) is allowed.
    assert validate_slash_command(
        'agent-a', 'deploy', exclude_action_id=existing['id']
    ) == ('deploy', None)


def test_validation_reserves_names_of_disabled_actions(panel_db):
    action = _add('agent-a', 'Deploy', slash_command='deploy')
    PanelDB('agent-a').update_action(action['id'], enabled=False)
    name, error = validate_slash_command('agent-a', 'deploy')
    assert name is None and 'already assigned' in error


def test_same_command_allowed_for_a_different_agent(panel_db):
    _add('agent-a', 'Deploy', slash_command='deploy')
    assert validate_slash_command('agent-b', 'deploy') == ('deploy', None)


# ── registry providers ───────────────────────────────────────────────


def _with_provider(monkeypatch, provider):
    providers = dict(command_registry._providers)
    providers['test'] = provider
    monkeypatch.setattr(command_registry, '_providers', providers)


def test_provider_command_executes_for_its_agent(monkeypatch):
    calls = []

    def provider(agent_id):
        if agent_id != 'agent-a':
            return []
        return [SlashCommand('deploy', lambda s, a, u, c, args: calls.append(args) or 'ran')]

    _with_provider(monkeypatch, provider)

    assert execute_command('deploy', 'prod', 'sess', 'agent-a', 'user') == 'ran'
    assert calls == ['prod']
    # Another agent doesn't get the command — falls through to the LLM.
    assert execute_command('deploy', 'prod', 'sess', 'agent-b', 'user') is None


def test_static_command_wins_over_provider(monkeypatch):
    def provider(agent_id):
        return [SlashCommand('clear', lambda *a: 'panel action')]

    _with_provider(monkeypatch, provider)

    listed = command_registry.provided_commands('agent-a')
    assert [c.name for c in listed] == []


def test_failing_provider_does_not_break_resolution(monkeypatch):
    def provider(agent_id):
        raise RuntimeError('boom')

    _with_provider(monkeypatch, provider)

    assert command_registry.provided_commands('agent-a') == []
    assert execute_command('nope', '', 'sess', 'agent-a', 'user') is None


# ── positional args ──────────────────────────────────────────────────


PARAMS = [
    {'name': 'env', 'label': 'Environment', 'type': 'text', 'required': True},
    {'name': 'tag', 'label': 'Tag', 'type': 'text', 'required': False, 'default': 'latest'},
]


def test_positional_args_map_onto_params():
    routes = _load_panel_routes()
    action = {'label': 'Deploy', 'params': PARAMS}
    params, error = routes._map_positional_args(action, 'deploy', 'prod v2')
    assert error is None
    assert params == {'env': 'prod', 'tag': 'v2'}


def test_optional_param_falls_back_to_default():
    routes = _load_panel_routes()
    action = {'label': 'Deploy', 'params': PARAMS}
    params, error = routes._map_positional_args(action, 'deploy', 'prod')
    assert error is None
    assert params == {'env': 'prod', 'tag': 'latest'}


def test_missing_required_param_returns_usage():
    routes = _load_panel_routes()
    action = {'label': 'Deploy', 'params': PARAMS}
    params, error = routes._map_positional_args(action, 'deploy', '')
    assert params is None
    assert '/deploy <env> [tag]' in error


def test_quoted_args_are_kept_together():
    routes = _load_panel_routes()
    action = {'label': 'Deploy', 'params': PARAMS}
    params, _ = routes._map_positional_args(action, 'deploy', '"staging one" v2')
    assert params['env'] == 'staging one'


def test_action_without_params_ignores_args():
    routes = _load_panel_routes()
    action = {'label': 'Restart', 'params': '[]'}
    assert routes._map_positional_args(action, 'restart', 'whatever') == ({}, None)
