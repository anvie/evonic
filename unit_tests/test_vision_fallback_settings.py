from unittest.mock import patch

from app import app
from backend.tools.describe_image import _resolve_vision_models
from models.db import db


def _vision_model(model_id, vision_supported=1):
    db.create_model({
        'id': model_id, 'name': model_id, 'type': 'openai',
        'provider': 'openai', 'model_name': model_id,
        'enabled': 1, 'vision_supported': vision_supported,
    })


def test_general_settings_returns_and_saves_second_vision_fallback():
    _vision_model('primary')
    _vision_model('fallback_1')
    _vision_model('fallback_2')
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    response = client.post('/api/settings/batch', json={'settings': {
        'vision_model_id': 'primary',
        'vision_fallback_model_id': 'fallback_1',
        'vision_fallback_model_2_id': 'fallback_2',
    }})
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        'vision_model_id': 'primary',
        'vision_fallback_model_id': 'fallback_1',
        'vision_fallback_model_2_id': 'fallback_2',
    }}
    settings = client.get('/api/settings/general').get_json()
    assert settings['vision_fallback_model_2_id'] == 'fallback_2'


def test_general_settings_saves_and_clears_default_model_fallback():
    _vision_model('fallback')
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    response = client.post('/api/settings/batch', json={'settings': {
        'default_model_fallback_id': 'fallback',
    }})
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        'default_model_fallback_id': 'fallback',
    }}
    assert client.get('/api/settings/general').get_json()['default_model_fallback_id'] == 'fallback'

    response = client.post('/api/settings/batch', json={'settings': {
        'default_model_fallback_id': '',
    }})
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        'default_model_fallback_id': '',
    }}
    assert db.get_setting('default_model_fallback_id', 'missing') == ''


def test_general_settings_saves_kb_organizer_schedule_and_refreshes_scheduler():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    with patch('backend.scheduler.scheduler.refresh_kb_organizer_schedule') as refresh:
        response = client.post('/api/settings/batch', json={'settings': {
            'kb_organizer_nightly_time': '14:25',
        }})

    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        'kb_organizer_nightly_time': '14:25',
    }}
    refresh.assert_called_once_with('14:25')
    assert db.get_setting('kb_organizer_nightly_time') == '14:25'
    assert client.get('/api/settings/general').get_json()['kb_organizer_nightly_time'] == '14:25'


def test_general_settings_rejects_invalid_kb_organizer_schedule():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    with patch(
        'backend.scheduler.scheduler.refresh_kb_organizer_schedule',
        side_effect=ValueError('must be in HH:MM format'),
    ) as refresh:
        response = client.post('/api/settings/batch', json={'settings': {
            'kb_organizer_nightly_time': 'invalid',
        }})

    assert response.status_code == 200
    assert response.get_json() == {
        'success': True,
        'partial': True,
        'results': {},
        'errors': ['kb_organizer_nightly_time: must be in HH:MM format'],
    }
    refresh.assert_called_once_with('invalid')
    assert db.get_setting('kb_organizer_nightly_time') != 'invalid'


def test_general_settings_rejects_unknown_default_model_fallback():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    response = client.post('/api/settings/batch', json={'settings': {
        'default_model_fallback_id': 'unknown',
    }})
    assert response.status_code == 200
    assert response.get_json() == {
        'success': True,
        'partial': True,
        'results': {},
        'errors': ['default_model_fallback_id: Model not found'],
    }


def test_general_settings_rejects_duplicate_vision_models():
    _vision_model('vision')
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    response = client.post('/api/settings/batch', json={'settings': {
        'vision_model_id': 'vision',
        'vision_fallback_model_id': 'vision',
    }})
    body = response.get_json()
    assert body['partial'] is True
    assert sorted(body['errors']) == [
        'vision_fallback_model_id: Must differ from the other configured vision models',
        'vision_model_id: Must differ from the other configured vision models',
    ]
    assert db.get_setting('vision_model_id', '') == ''


def test_vision_resolver_orders_both_explicit_fallbacks_before_implicit_models():
    for model_id in ('primary', 'fallback_1', 'fallback_2', 'agent_model', 'automatic'):
        _vision_model(model_id)
    db.set_setting('vision_model_id', 'primary')
    db.set_setting('vision_fallback_model_id', 'fallback_1')
    db.set_setting('vision_fallback_model_2_id', 'fallback_2')
    agent = {'id': 'test_super_agent'}
    with patch.object(db, 'get_agent_model', return_value=db.get_model_by_id('agent_model')):
        models, error = _resolve_vision_models(agent)
    assert error is None
    # Implicit vision models come from `get_enabled_llm_models()`, which never
    # includes decision models such as the builtin `LiquidAI d1-3B`.
    assert [model['id'] for model in models] == [
        'primary', 'fallback_1', 'fallback_2', 'agent_model', 'automatic',
    ]


def test_general_settings_saves_main_model_and_fallback_chain_together():
    for model_id in ('chain_main', 'chain_fb1', 'chain_fb2', 'chain_fb3'):
        _vision_model(model_id)
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    response = client.post('/api/settings/batch', json={'settings': {
        'default_model_id': 'chain_main',
        'default_model_fallback_ids': ['chain_fb1', 'chain_fb2', 'chain_fb3'],
    }})
    assert response.get_json() == {'success': True, 'results': {
        'default_model_id': 'chain_main',
        'default_model_fallback_ids': ['chain_fb1', 'chain_fb2', 'chain_fb3'],
    }}
    general = client.get('/api/settings/general').get_json()
    assert general['default_model_fallback_ids'] == ['chain_fb1', 'chain_fb2', 'chain_fb3']
    assert general['default_model_fallback_id'] == 'chain_fb1'
    assert general['default_model_fallback_max'] == 5

    # The main model cannot also be a fallback; the stored chain is untouched.
    response = client.post('/api/settings/batch', json={'settings': {
        'default_model_fallback_ids': ['chain_main'],
    }})
    body = response.get_json()
    assert body['partial'] is True
    assert 'differ from the main model' in body['errors'][0]

    # Duplicates and over-long chains are rejected.
    for bad in (['chain_fb1', 'chain_fb1'], ['chain_fb1'] * 6):
        body = client.post('/api/settings/batch', json={'settings': {
            'default_model_fallback_ids': bad,
        }}).get_json()
        assert body.get('partial') is True
    assert db.get_setting('default_model_fallback_id') == 'chain_fb1'

    # An empty list clears the chain and the legacy mirror.
    client.post('/api/settings/batch', json={'settings': {'default_model_fallback_ids': []}})
    assert client.get('/api/settings/general').get_json()['default_model_fallback_ids'] == []
    assert db.get_setting('default_model_fallback_id') == ''


def test_settings_page_renders_the_model_chain_component():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    html = client.get('/system').get_data(as_text=True)
    assert 'id="default-model-chain"' in html
    assert 'data-fallbacks="5"' in html
    assert 'id="vision-model-chain"' in html and 'data-main-label="Primary"' in html
    assert 'js/model-chain.js' in html and 'css/model-chain.css' in html


def test_explorer_skill_saves_a_model_chain_and_mirrors_the_legacy_fallback():
    from backend.skills_manager import skills_manager
    for model_id in ('ex_main', 'ex_fb1', 'ex_fb2'):
        _vision_model(model_id)

    result = skills_manager.set_skill_config('explorer', {
        'model_id': 'ex_main',
        'fallback_model_ids': ['ex_fb1', 'ex_main', 'ex_fb2', 'ex_fb1'],
    })
    assert result.get('success') is True
    cfg = skills_manager.get_skill_config('explorer')
    assert cfg['model_id'] == 'ex_main'
    assert cfg['fallback_model_ids'] == ['ex_fb1', 'ex_fb2']
    assert cfg['fallback_model_id'] == 'ex_fb1'

    # Back to the global default: the explorer keeps no fallbacks of its own.
    skills_manager.set_skill_config('explorer', {
        'model_id': '', 'fallback_model_ids': ['ex_fb1'],
    })
    cfg = skills_manager.get_skill_config('explorer')
    assert cfg['model_id'] == ''
    assert cfg['fallback_model_ids'] == []
    assert cfg['fallback_model_id'] == ''


def test_explorer_skill_reads_a_legacy_single_fallback_as_a_chain():
    from backend.skills_manager import skills_manager
    db.set_setting('skill_config:explorer:fallback_model_id', 'legacy_fb')
    with db._connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?",
                     ('skill_config:explorer:fallback_model_ids',))
        conn.commit()
    db.invalidate_settings_cache()
    cfg = skills_manager.get_skill_config('explorer')
    assert cfg['fallback_model_ids'] == ['legacy_fb']


def test_vision_chain_saves_primary_and_five_fallbacks():
    ids = ['vc_primary'] + [f'vc_fb{i}' for i in range(1, 6)]
    for model_id in ids:
        _vision_model(model_id)
    _vision_model('vc_text_only', vision_supported=0)
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    body = client.post('/api/settings/batch', json={'settings': {
        'vision_model_id': 'vc_primary',
        'vision_fallback_model_ids': ids[1:],
    }}).get_json()
    assert body == {'success': True, 'results': {
        'vision_model_id': 'vc_primary',
        'vision_fallback_model_ids': ids[1:],
    }}
    general = client.get('/api/settings/general').get_json()
    assert general['vision_fallback_model_ids'] == ids[1:]
    # The legacy per-slot keys mirror the first two fallbacks.
    assert general['vision_fallback_model_id'] == 'vc_fb1'
    assert general['vision_fallback_model_2_id'] == 'vc_fb2'

    models, err = _resolve_vision_models({'id': 'agent-x'})
    assert [m['id'] for m in models][:6] == ids

    # Non-vision models, the primary, and over-long chains are rejected.
    for bad in (['vc_text_only'], ['vc_primary'], ids[1:] + ['vc_primary']):
        body = client.post('/api/settings/batch', json={'settings': {
            'vision_fallback_model_ids': bad,
        }}).get_json()
        assert body.get('partial') is True, bad
    assert client.get('/api/settings/general').get_json()['vision_fallback_model_ids'] == ids[1:]

    # A legacy slot write keeps the rest of the chain.
    client.post('/api/settings/batch', json={'settings': {'vision_fallback_model_2_id': ''}})
    assert db.get_setting('vision_fallback_model_ids') == \
        '["vc_fb1", "vc_fb3", "vc_fb4", "vc_fb5"]'
