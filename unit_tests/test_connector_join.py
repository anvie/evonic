"""Evonet join-token enrollment: POST /api/connector/join."""

import uuid

from models.db import db

DEVICE = str(uuid.uuid4())


def _client():
    from app import app
    return app.test_client()


def _join(client, token, device_id=DEVICE, **extra):
    body = {'join_token': token, 'device_id': device_id,
            'device_name': 'node-1', 'platform': 'linux', 'version': '1.3.0'}
    body.update(extra)
    return client.post('/api/connector/join', json=body)


def _enable():
    db.set_setting('evonet_join_enabled', '1')
    db.set_setting('evonet_join_token', 'ejt_secret')


def test_disabled_rejects_even_with_correct_token():
    db.set_setting('evonet_join_token', 'ejt_secret')
    assert _join(_client(), 'ejt_secret').status_code == 403


def test_wrong_token_rejected():
    _enable()
    assert _join(_client(), 'nope').status_code == 403


def test_bad_device_id_rejected():
    _enable()
    assert _join(_client(), 'ejt_secret', device_id='not-a-uuid').status_code == 400


def test_first_join_creates_tunnel_workplace_and_connector():
    _enable()
    resp = _join(_client(), 'ejt_secret', name='build-box', workspace_path='/srv/work')
    data = resp.get_json()

    assert resp.status_code == 200 and data['created'] is True
    wp = db.get_workplace(data['workplace_id'])
    assert wp['type'] == 'tunnel' and wp['name'] == 'build-box'
    connector = db.get_connector_by_token(data['connector_token'])
    assert connector['workplace_id'] == data['workplace_id']
    assert db.get_connector_by_device_id(DEVICE)['workplace_id'] == data['workplace_id']


def test_name_defaults_to_device_name():
    _enable()
    data = _join(_client(), 'ejt_secret').get_json()
    assert db.get_workplace(data['workplace_id'])['name'] == 'node-1'


def test_rejoin_reuses_workplace_and_rotates_token():
    _enable()
    first = _join(_client(), 'ejt_secret').get_json()
    second = _join(_client(), 'ejt_secret').get_json()

    assert second['created'] is False
    assert second['workplace_id'] == first['workplace_id']
    assert second['connector_token'] != first['connector_token']
    assert db.get_connector_by_token(first['connector_token']) is None
    assert db.get_connector_by_token(second['connector_token']) is not None


def test_rejoin_after_workplace_deleted_creates_new_one():
    _enable()
    first = _join(_client(), 'ejt_secret').get_json()
    db.delete_workplace(first['workplace_id'])
    second = _join(_client(), 'ejt_secret').get_json()

    assert second['created'] is True
    assert second['workplace_id'] != first['workplace_id']


def test_regenerating_token_keeps_enrolled_workplace_working():
    _enable()
    client = _client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    first = _join(client, 'ejt_secret').get_json()

    new_token = client.post('/api/settings/evonet-join/regenerate').get_json()['token']

    assert db.get_connector_by_token(first['connector_token']) is not None
    assert _join(client, 'ejt_secret').status_code == 403
    assert _join(client, new_token).status_code == 200


def test_settings_endpoint_generates_token_and_toggles():
    client = _client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    first = client.get('/api/settings/evonet-join').get_json()
    assert first['enabled'] is False and first['token'].startswith('ejt_')
    assert client.get('/api/settings/evonet-join').get_json()['token'] == first['token']

    assert client.put('/api/settings/evonet-join', json={'enabled': True}).get_json()['enabled'] is True
    assert client.get('/api/settings/evonet-join').get_json()['enabled'] is True
