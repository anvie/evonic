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
    assert [model['id'] for model in models] == [
        'primary', 'fallback_1', 'fallback_2', 'agent_model', 'automatic',
    ]
