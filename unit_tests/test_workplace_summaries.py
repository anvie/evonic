"""GET /api/workplaces?summary=1: one query, display fields only, no secrets."""

import json

from flask import Flask

from models.db import db
from routes.workplaces import workplaces_bp


def _seed():
    tunnel = db.create_workplace({'name': 'build-server', 'type': 'tunnel',
                                  'config': {'workspace_path': '/srv/ws'}})
    db.create_connector({'workplace_id': tunnel, 'connector_token': 'ect_secret_token',
                         'device_name': 'build-01', 'platform': 'linux', 'version': '1.2.8'})
    db.update_connector(db.get_connector_by_workplace(tunnel)['id'], {'ip_addresses': '203.0.113.10'})
    remote = db.create_workplace({'name': 'Alpha-ssh', 'type': 'remote', 'config': {
        'host': 'staging.example.com', 'username': 'deploy', 'port': 2222,
        'password': 'hunter2', 'key_path': '/root/.ssh/id_rsa', 'workspace_path': '/srv/app'}})
    return tunnel, remote


def test_summaries_carry_display_fields_without_secrets():
    tunnel, remote = _seed()

    items = {w['id']: w for w in db.get_workplace_summaries()}

    assert items[tunnel]['connector'] == {'device_name': 'build-01', 'platform': 'linux', 'version': '1.2.8',
                                          'last_seen_at': None, 'ip_addresses': '203.0.113.10'}
    assert items[tunnel]['workspace_path'] == '/srv/ws'
    assert items[remote]['connector'] is None
    assert {items[remote][k] for k in ('host', 'username', 'port')} == {'staging.example.com', 'deploy', 2222}
    dumped = json.dumps(list(items.values()))
    for secret in ('ect_secret_token', 'hunter2', 'id_rsa', 'config', 'agents'):
        assert secret not in dumped


def test_summaries_count_assigned_agents():
    tunnel, remote = _seed()
    for agent_id in ('a1', 'a2'):
        db.create_agent({'id': agent_id, 'name': agent_id})
        db.update_agent(agent_id, {'workplace_id': tunnel})

    counts = {w['id']: w['agent_count'] for w in db.get_workplace_summaries()}

    assert counts[tunnel] == 2 and counts[remote] == 0


def test_summaries_are_sorted_case_insensitively():
    _seed()
    db.create_workplace({'name': 'zeta', 'type': 'local', 'config': {}})

    assert [w['name'] for w in db.get_workplace_summaries()] == ['Alpha-ssh', 'build-server', 'zeta']


def test_list_endpoint_serves_summary_on_request():
    tunnel, _ = _seed()
    app = Flask(__name__)
    app.register_blueprint(workplaces_bp)
    client = app.test_client()

    summary = client.get('/api/workplaces?summary=1').get_json()
    full = client.get('/api/workplaces').get_json()

    assert {w['id'] for w in summary} == {w['id'] for w in full}
    assert 'agents' not in summary[0] and 'agents' in full[0]
    assert next(w for w in summary if w['id'] == tunnel)['connector']['device_name'] == 'build-01'
