"""Tests for the centralized Shared Channel settings: schema migration,
inbox helpers, and the /api/shared-channels endpoints."""

import sqlite3

import pytest
from flask import Flask

from models.db import db


# ── Schema migration ────────────────────────────────────────────────────────

def test_channels_agent_id_is_nullable():
    # The autouse test DB was freshly initialized — NULL agent_id must insert
    chan_id = db.create_channel({
        'agent_id': None, 'type': 'whatsapp_shared', 'name': 'S',
        'config': {'mode': 'open', 'routes': {}},
    })
    ch = db.get_channel(chan_id)
    assert ch['agent_id'] is None


def test_migration_rebuilds_old_table_and_detaches_shared(tmp_path):
    """A DB created with the old NOT NULL schema is rebuilt in place and
    v1 shared channels are detached from their host agent."""
    import threading
    old_path, old_tls = db.db_path, db._tls
    p = str(tmp_path / 'old.db')
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE channels (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, type TEXT NOT NULL,
        name TEXT, config TEXT DEFAULT '{}', enabled BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
        UNIQUE(agent_id, name))""")
    con.execute("INSERT INTO channels (id, agent_id, type, name) VALUES ('c1','ag1','telegram','T')")
    con.execute("INSERT INTO channels (id, agent_id, type, name) VALUES ('c2','ag1','whatsapp_shared','S')")
    con.commit()
    con.close()
    db.db_path = p
    db._tls = threading.local()
    db._init_tables()
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='channels'").fetchone()[0]
    assert 'agent_id TEXT NOT NULL' not in sql
    rows = {r['id']: dict(r) for r in con.execute("SELECT * FROM channels").fetchall()}
    assert rows['c1']['agent_id'] == 'ag1'      # regular channel untouched
    assert rows['c2']['agent_id'] is None       # v1 shared channel detached
    assert con.execute("SELECT name FROM sqlite_master WHERE name='shared_channel_inbox'").fetchone()
    con.close()
    db.db_path = old_path
    db._tls = old_tls


# ── Inbox helpers ───────────────────────────────────────────────────────────

@pytest.fixture
def chan_id():
    return db.create_channel({
        'agent_id': None, 'type': 'whatsapp_shared', 'name': 'Shared',
        'config': {'mode': 'open', 'routes': {}},
    })


def test_inbox_upsert_and_delete(chan_id):
    db.record_inbox_sender(chan_id, '6281', alt_user_id='', push_name='Budi',
                           preview='first')
    db.record_inbox_sender(chan_id, '6281', alt_user_id='629', push_name='',
                           preview='second')
    entries = db.get_inbox(chan_id)
    assert len(entries) == 1
    e = entries[0]
    assert e['message_count'] == 2
    assert e['last_message'] == 'second'
    assert e['push_name'] == 'Budi'      # empty update keeps old name
    assert e['alt_user_id'] == '629'     # non-empty update wins
    assert db.get_inbox_entry(e['id'])['external_user_id'] == '6281'
    assert db.delete_inbox_entry(e['id'])
    assert db.get_inbox(chan_id) == []


def test_inbox_preview_capped_at_200(chan_id):
    db.record_inbox_sender(chan_id, '6281', preview='x' * 500)
    assert len(db.get_inbox(chan_id)[0]['last_message']) == 200


def test_inbox_pruned_to_cap(chan_id):
    for i in range(110):
        db.record_inbox_sender(chan_id, f'62{i:04d}')
    assert len(db.get_inbox(chan_id)) == 100


def test_inbox_cleanup_expires_entries_across_shared_channels(chan_id):
    other_channel = db.create_channel({
        'agent_id': None, 'type': 'whatsapp_shared', 'name': 'Other Shared',
        'config': {'mode': 'open', 'routes': {}},
    })
    db.record_inbox_sender(chan_id, 'expired-one')
    db.record_inbox_sender(other_channel, 'expired-two')
    db.record_inbox_sender(other_channel, 'recent')
    with db._connect() as conn:
        conn.execute("""
            UPDATE shared_channel_inbox
            SET last_seen = datetime('now', '-25 hours')
            WHERE external_user_id IN ('expired-one', 'expired-two')
        """)
        conn.commit()

    assert db.cleanup_expired_inbox_entries(24) == 2
    assert db.get_inbox(chan_id) == []
    assert [entry['external_user_id'] for entry in db.get_inbox(other_channel)] == ['recent']


def test_get_shared_channels_only_agentless(chan_id):
    db.create_agent({'id': 'ag1', 'name': 'A1'})
    db.create_channel({'agent_id': 'ag1', 'type': 'whatsapp', 'name': 'W'})
    shared = db.get_shared_channels()
    assert [c['id'] for c in shared] == [chan_id]


# ── API endpoints ───────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from routes.settings import settings_bp
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(settings_bp)
    return app.test_client()


@pytest.fixture
def agent_a():
    db.create_agent({'id': 'agent-a', 'name': 'Agent A'})
    return 'agent-a'


def test_create_list_delete_shared_channel(client, monkeypatch):
    # Don't spawn a real Baileys bridge in tests
    from backend.channels.registry import channel_manager
    monkeypatch.setattr(channel_manager, 'start_channel', lambda cid: True)
    monkeypatch.setattr(channel_manager, 'stop_channel', lambda cid: True)

    resp = client.post('/api/shared-channels', json={'name': 'Company WA'})
    assert resp.status_code == 200
    chan = resp.get_json()['channel']
    assert chan['agent_id'] is None
    assert chan['config']['mode'] == 'open'
    assert chan['config']['access_mode'] == 'assigned_only'
    assert chan['config']['default_agent_id'] == ''

    # Duplicate name rejected app-side (UNIQUE treats NULLs as distinct)
    assert client.post('/api/shared-channels',
                       json={'name': 'Company WA'}).status_code == 409

    listed = client.get('/api/shared-channels').get_json()['channels']
    assert [c['id'] for c in listed] == [chan['id']]

    assert client.delete(f"/api/shared-channels/{chan['id']}").status_code == 200
    assert client.get('/api/shared-channels').get_json()['channels'] == []


def test_unassigned_sender_retention_defaults_saves_and_validates(client):
    response = client.get('/api/shared-channels/settings')
    assert response.status_code == 200
    assert response.get_json()['unassigned_sender_retention_hours'] == 24

    response = client.put('/api/shared-channels/settings', json={
        'unassigned_sender_retention_hours': 48,
    })
    assert response.status_code == 200
    assert response.get_json()['unassigned_sender_retention_hours'] == 48
    assert db.get_setting('shared_channel_inbox_retention_hours') == '48'

    for value in (None, 'nope', 0, 8761):
        response = client.put('/api/shared-channels/settings', json={
            'unassigned_sender_retention_hours': value,
        })
        assert response.status_code == 400


def test_shared_channel_listing_cleans_expired_inbox_entries(client, chan_id):
    db.record_inbox_sender(chan_id, 'expired')
    with db._connect() as conn:
        conn.execute("UPDATE shared_channel_inbox SET last_seen = datetime('now', '-25 hours')")
        conn.commit()

    assert client.get('/api/shared-channels').get_json()['channels'][0]['inbox_count'] == 0


def test_routes_add_and_remove(client, chan_id, agent_a):
    resp = client.post(f'/api/shared-channels/{chan_id}/routes',
                       json={'user_id': '+62 812-345', 'agent_id': agent_a,
                             'name': 'Budi'})
    assert resp.status_code == 200
    config = db.get_channel(chan_id)['config']
    assert config['routes'] == {'62812345': agent_a}   # normalized to digits
    assert config['user_names']['62812345'] == 'Budi'

    assert client.post(f'/api/shared-channels/{chan_id}/routes',
                       json={'user_id': 'abc', 'agent_id': agent_a}).status_code == 400
    assert client.post(f'/api/shared-channels/{chan_id}/routes',
                       json={'user_id': '628', 'agent_id': 'ghost'}).status_code == 404

    assert client.delete(
        f'/api/shared-channels/{chan_id}/routes/62812345').status_code == 200
    config = db.get_channel(chan_id)['config']
    assert config['routes'] == {}
    assert config['user_names'] == {}


def test_access_settings_validate_and_preserve_routes(client, chan_id, agent_a):
    db.update_channel(chan_id, {'config': {
        'mode': 'open', 'routes': {'628111': agent_a},
        'user_names': {'628111': 'Budi'}, 'bridge_port': 3001,
    }})
    response = client.put(f'/api/shared-channels/{chan_id}', json={
        'access_mode': 'unrestricted', 'default_agent_id': agent_a})
    assert response.status_code == 200
    config = db.get_channel(chan_id)['config']
    assert config['access_mode'] == 'unrestricted'
    assert config['default_agent_id'] == agent_a
    assert config['routes'] == {'628111': agent_a}
    assert config['user_names'] == {'628111': 'Budi'}
    assert config['bridge_port'] == 3001


def test_access_settings_reject_invalid_or_disabled_default(client, chan_id):
    db.create_agent({'id': 'agent-off', 'name': 'Disabled', 'enabled': False})
    assert client.put(f'/api/shared-channels/{chan_id}', json={
        'access_mode': 'unknown'}).status_code == 400
    assert client.put(f'/api/shared-channels/{chan_id}', json={
        'access_mode': 'unrestricted', 'default_agent_id': 'ghost'}).status_code == 400
    assert client.put(f'/api/shared-channels/{chan_id}', json={
        'access_mode': 'unrestricted', 'default_agent_id': 'agent-off'}).status_code == 400


def test_access_settings_are_independent_per_number(client, agent_a):
    first = db.create_channel({'agent_id': None, 'type': 'whatsapp_shared', 'name': 'One',
                               'config': {'routes': {}}})
    second = db.create_channel({'agent_id': None, 'type': 'whatsapp_shared', 'name': 'Two',
                                'config': {'routes': {}}})
    assert client.put(f'/api/shared-channels/{first}', json={
        'access_mode': 'unrestricted', 'default_agent_id': agent_a}).status_code == 200
    assert db.get_channel(second)['config'].get('access_mode') is None
    assert db.get_channel(first)['config']['default_agent_id'] == agent_a


def test_route_removal_cleans_orphaned_names(client, chan_id, agent_a):
    db.update_channel(chan_id, {'config': {
        'mode': 'open',
        'routes': {'628111': agent_a, '628222': agent_a},
        'user_names': {
            '628111': 'Removed Contact',
            '628222': 'Active Contact',
            'status': 'Legacy Orphan',
        },
    }})

    resp = client.delete(f'/api/shared-channels/{chan_id}/routes/628111')

    assert resp.status_code == 200
    config = db.get_channel(chan_id)['config']
    assert config['routes'] == {'628222': agent_a}
    assert config['user_names'] == {'628222': 'Active Contact'}


def test_inbox_assign_routes_both_identifiers(client, chan_id, agent_a):
    db.record_inbox_sender(chan_id, '99988877', alt_user_id='62812345',
                           push_name='Budi', preview='hi')
    entry = db.get_inbox(chan_id)[0]

    resp = client.post(
        f"/api/shared-channels/{chan_id}/inbox/{entry['id']}/assign",
        json={'agent_id': agent_a, 'name': 'Pak Budi'})
    assert resp.status_code == 200

    config = db.get_channel(chan_id)['config']
    # Both the LID digits and the phone digits are routed
    assert config['routes'] == {'99988877': agent_a, '62812345': agent_a}
    assert config['user_names']['99988877'] == 'Pak Budi'
    assert config['user_names']['62812345'] == 'Pak Budi'
    assert db.get_inbox(chan_id) == []


def test_inbox_assign_defaults_name_to_push_name(client, chan_id, agent_a):
    db.record_inbox_sender(chan_id, '628999', push_name='Siti')
    entry = db.get_inbox(chan_id)[0]
    client.post(f"/api/shared-channels/{chan_id}/inbox/{entry['id']}/assign",
                json={'agent_id': agent_a})
    assert db.get_channel(chan_id)['config']['user_names']['628999'] == 'Siti'


def test_inbox_dismiss(client, chan_id):
    db.record_inbox_sender(chan_id, '628999')
    entry = db.get_inbox(chan_id)[0]
    assert client.delete(
        f"/api/shared-channels/{chan_id}/inbox/{entry['id']}").status_code == 200
    assert db.get_inbox(chan_id) == []
    assert client.delete(
        f"/api/shared-channels/{chan_id}/inbox/{entry['id']}").status_code == 404


def test_endpoints_reject_agent_owned_channels(client, agent_a):
    owned = db.create_channel({'agent_id': agent_a, 'type': 'whatsapp', 'name': 'W'})
    assert client.get(f'/api/shared-channels/{owned}/inbox').status_code == 404
    assert client.delete(f'/api/shared-channels/{owned}').status_code == 404
