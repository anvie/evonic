"""Regression guards: CMP additions must not change behavior when unused."""

import json

from backend.agent_state import AgentState


def test_default_state_has_no_cmp():
    assert AgentState().cmp is None


def test_old_format_deserializes_without_cmp_key():
    old = json.dumps({"mode": "execute", "tasks": [], "next_task_id": 1,
                      "plan_file": None, "states": {}, "focus": False,
                      "focus_reason": None, "auto_trivial": False,
                      "atg": None})
    ms = AgentState.deserialize(old)
    assert ms.cmp is None


def test_render_without_cmp_flag_unchanged():
    ms = AgentState()
    ms.cmp = {"active_id": "A1", "paths": {"A1": {"id": "A1", "title": "t",
                                                  "status": "active"}}}
    # cmp present but flag off → no CMP section (unflagged agents unaffected
    # even if state carries a leftover cmp dict)
    assert "Session Paths" not in ms.render()
    assert "Session Paths" in ms.render(cmp_enabled=True)


def test_render_with_flag_but_no_cmp_unchanged():
    assert "Session Paths" not in AgentState().render(cmp_enabled=True)


def test_persist_split_carries_cmp():
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    from models.db import db

    db.create_agent({'id': 'cmp_test_agent', 'name': 'C', 'system_prompt': ''})
    ms = AgentState()
    ms.cmp = {"version": 1, "active_id": "A1", "next_id": 2, "paths": {},
              "stats": {}}
    _persist_agent_state_split(ms, 'cmp_test_agent', 'sess-c1')
    data = json.loads(db.get_session_state('sess-c1', agent_id='cmp_test_agent'))
    assert data['cmp'] == ms.cmp


def test_chat_state_api_exposes_cmp_map():
    import json as _json
    from app import app
    from models.db import db
    from backend.agent_runtime.cmp import store

    db.create_agent({'id': 'cmp_api_agent', 'name': 'C', 'system_prompt': ''})
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='website task', goal='build it', now_ts=1000)
    store.create_path(ms.cmp, ms, 'invoice task', depends_on=['A1'], now_ts=2000)
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    _persist_agent_state_split(ms, 'cmp_api_agent', 'sess-api-1')

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        res = client.get('/api/agents/cmp_api_agent/chat/state?session_id=sess-api-1')
        assert res.status_code == 200
        data = res.get_json()
    cmp = data.get('cmp')
    assert cmp and cmp['active_id'] == 'B1'
    assert 'flowchart TD' in cmp['mermaid']
    ids = [p['id'] for p in cmp['paths']]
    assert ids == ['A1', 'B1']
    assert cmp['paths'][1]['depends_on'] == ['A1']
    # cards carry what the modal needs, nothing sensitive extra
    assert set(cmp['paths'][0]) == {'id', 'title', 'status', 'action', 'goal',
                                    'outcome', 'key_facts', 'artifacts',
                                    'depends_on', 'last_active', 'state_since',
                                    'tokens', 'llm_tokens', 'card_tokens', 'tags'}
    assert all('tokens' in c and 'card_tokens' in c for c in cmp['paths'])
    # dependency child gets the next level letter
    assert cmp['paths'][1]['id'] == 'B1'


def test_chat_state_api_no_cmp_key_when_absent():
    from app import app
    from models.db import db

    db.create_agent({'id': 'cmp_api_agent2', 'name': 'C', 'system_prompt': ''})
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    _persist_agent_state_split(AgentState(), 'cmp_api_agent2', 'sess-api-2')

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        res = client.get('/api/agents/cmp_api_agent2/chat/state?session_id=sess-api-2')
        assert res.status_code == 200
        assert 'cmp' not in res.get_json()


def test_cmp_model_setting_resolution():
    """cmp_model_id → task_classifier_model_id → default fallback chain."""
    from unittest.mock import MagicMock, patch
    from backend.task_classifier import _get_classifier_client

    model = {'id': 'm-cmp', 'name': 'CmpModel', 'model_name': 'x'}
    fallback_model = {'id': 'm-cls', 'name': 'ClsModel', 'model_name': 'y'}

    def _db(settings, models):
        db = MagicMock()
        db.get_setting.side_effect = lambda k, d='': settings.get(k, d)
        db.get_model_by_id.side_effect = lambda mid: models.get(mid)
        return db

    with patch('models.db.db', _db({'cmp_model_id': 'm-cmp'}, {'m-cmp': model})), \
         patch('backend.task_classifier.LLMClient') as llm:
        _get_classifier_client('cmp_model_id')
        llm.assert_called_once_with(model_config=model)

    # unset cmp model → task classifier model
    with patch('models.db.db',
               _db({'task_classifier_model_id': 'm-cls'}, {'m-cls': fallback_model})), \
         patch('backend.task_classifier.LLMClient') as llm:
        _get_classifier_client('cmp_model_id')
        llm.assert_called_once_with(model_config=fallback_model)

    # nothing configured → default client
    with patch('models.db.db', _db({}, {})), \
         patch('backend.task_classifier.LLMClient') as llm:
        _get_classifier_client('cmp_model_id')
        llm.assert_called_once_with()


def test_classifier_chat_retries_generation_timeout_with_doubled_budget():
    from unittest.mock import MagicMock
    from backend.task_classifier import classifier_chat

    client = MagicMock()
    client.chat_completion.side_effect = [
        {'success': False, 'error_type': 'generation_timeout'},
        {'success': True, 'response': {'choices': [{'message': {'content': 'OK'}}]}},
    ]
    response = classifier_chat(client, [{'role': 'user', 'content': 'x'}],
                               max_tokens=400)
    assert response['success']
    assert client.chat_completion.call_count == 2
    assert client.chat_completion.call_args_list[0][1]['max_tokens'] == 400
    assert client.chat_completion.call_args_list[1][1]['max_tokens'] == 800

    # only ONE retry — a second timeout returns the failure
    client = MagicMock()
    client.chat_completion.return_value = {'success': False,
                                           'error_type': 'generation_timeout'}
    response = classifier_chat(client, [{'role': 'user', 'content': 'x'}],
                               max_tokens=400)
    assert not response['success']
    assert client.chat_completion.call_count == 2

    # other error types never retry
    client = MagicMock()
    client.chat_completion.return_value = {'success': False,
                                           'error_type': 'api_error'}
    classifier_chat(client, [{'role': 'user', 'content': 'x'}], max_tokens=400)
    assert client.chat_completion.call_count == 1


def test_cmp_model_settings_api():
    from app import app
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        res = client.get('/api/settings/cmp-model')
        assert res.status_code == 200
        assert res.get_json()['model_id'] is None
        # unknown model rejected
        res = client.put('/api/settings/cmp-model', json={'model_id': 'nope'})
        assert res.status_code == 404
        # clearing works
        res = client.put('/api/settings/cmp-model', json={'model_id': ''})
        assert res.status_code == 200 and res.get_json()['success']


def test_clear_resets_cmp_key():
    # /clear writes a full-replace session_data including explicit cmp None
    import inspect
    from backend import slash_commands
    src = inspect.getsource(slash_commands)
    assert "'cmp': None" in src
