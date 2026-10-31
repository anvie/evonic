"""ask_user moved from a plugin into core: the per-agent flag, the one-time
migration that drops the plugin's leftovers, and the retired plugin id."""

import json

from backend.plugin_lifecycle import PluginManager
from models.db import db

PLUGIN_TOOL_ID = 'plugin:ask_user:ask_user_question'


def _agent(agent_id='ask_agent', **extra):
    db.create_agent({'id': agent_id, 'name': 'Ask Agent', **extra})
    return db.get_agent(agent_id)


def test_ask_user_enabled_defaults_on_and_can_be_turned_off():
    assert _agent()['ask_user_enabled'] == 1
    db.update_agent('ask_agent', {'ask_user_enabled': False})
    assert db.get_agent('ask_agent')['ask_user_enabled'] == 0
    db.update_agent('ask_agent', {'ask_user_enabled': 'true'})
    assert db.get_agent('ask_agent')['ask_user_enabled'] == 1


def test_create_agent_honours_explicit_off():
    assert _agent(ask_user_enabled=0)['ask_user_enabled'] == 0


def test_first_migration_drops_plugin_assignment_and_setting():
    _agent()
    with db._connect() as conn:
        conn.execute("INSERT INTO agent_tools (agent_id, tool_id) VALUES ('ask_agent', ?)", (PLUGIN_TOOL_ID,))
        conn.execute("INSERT INTO agent_tools (agent_id, tool_id) VALUES ('ask_agent', 'bash')")
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('plugin_enabled:ask_user', '1')")
        # A database from before the move has no ask_user_enabled column yet.
        conn.execute("ALTER TABLE agents DROP COLUMN ask_user_enabled")
        conn.commit()

    db._init_tables()

    assert db.get_agent_tools('ask_agent') == ['bash']
    assert db.get_setting('plugin_enabled:ask_user') is None
    assert db.get_agent('ask_agent')['ask_user_enabled'] == 1


def test_later_startups_leave_assignments_alone():
    _agent()
    with db._connect() as conn:
        conn.execute("INSERT INTO agent_tools (agent_id, tool_id) VALUES ('ask_agent', ?)", (PLUGIN_TOOL_ID,))
        conn.commit()
    db._init_tables()
    assert db.get_agent_tools('ask_agent') == [PLUGIN_TOOL_ID]


def test_retired_plugin_is_not_listed_or_installable(tmp_path, monkeypatch):
    plugins_dir = tmp_path / 'plugins'
    leftover = plugins_dir / 'ask_user'
    leftover.mkdir(parents=True)
    (leftover / 'plugin.json').write_text(json.dumps({'id': 'ask_user', 'name': 'Ask User'}))
    monkeypatch.setattr('backend.plugin_lifecycle.PLUGINS_DIR', str(plugins_dir))
    pm = PluginManager(load_plugins=False)

    assert [p['id'] for p in pm.list_plugins()] == []
    result = pm.install_plugin_from_dir(str(leftover), force=True)
    assert 'built into Evonic' in result['error']


def _client():
    from app import app
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


def test_agent_page_loads_question_card_and_settings_toggle():
    _agent()
    html = _client().get('/agents/ask_agent').get_data(as_text=True)
    assert 'js/ask-user.js' in html and 'css/ask-user.css' in html
    assert 'id="agent-ask-user-enabled"' in html
    assert "key: 'ask_user_enabled'" in html


def test_core_routes_answer_pending_questions():
    body = _client().get('/api/ask_user/pending?session_id=nope').get_json()
    assert body == {'questions': [], 'resolved': {}}


def test_realtime_snapshot_lists_open_questions_for_notifications():
    from backend.ask_user_store import normalize_questions, question_registry
    from routes.realtime import _ALLOWED_CHANNELS, _build_snapshot

    assert 'questions' in _ALLOWED_CHANNELS
    _agent()
    questions, _ = normalize_questions([{'question': 'Ship it?', 'header': 'Release',
                                         'options': ['Yes', 'No']}])
    pending = question_registry.create('sess-9', 'ask_agent', questions)
    try:
        events = [e for e in _build_snapshot({'questions'}) if e[0] == 'questions']
        assert events == [('questions', 'question_required', {
            'question_id': pending.question_id, 'agent_id': 'ask_agent',
            'agent_name': 'Ask Agent', 'session_id': 'sess-9',
            'header': 'Release', 'question': 'Ship it?', 'count': 1,
        })]
        question_registry.dismiss(pending.question_id)
        assert [e for e in _build_snapshot({'questions'}) if e[0] == 'questions'] == []
    finally:
        question_registry.remove(pending.question_id)
