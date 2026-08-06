"""Keyed Fact Ledger integration tests.

`remember` with a key writes the fact to the memories ledger (dimension=key),
supersedes older same-key rows, and REPLACES the same-key session pin in place.
`recall(mode='key')` is a deterministic point read of the current value.
"""

import os
import sys
import uuid
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db, AgentChatDB, agent_chat_manager
from backend.agent_runtime.memory_manager import store_memory, recall_by_key


@pytest.fixture
def agent_id(tmp_path):
    aid = f"test_agent_{uuid.uuid4().hex[:8]}"
    original_path = db.db_path
    db.db_path = str(tmp_path / f"{aid}_db.sqlite")
    db._tls.conn = None
    db._init_tables()
    db.create_agent({'id': aid, 'name': 'Test Agent', 'system_prompt': 'test'})
    yield aid
    db._tls.conn = None
    db.db_path = original_path


@pytest.fixture
def chat_db(agent_id, tmp_path):
    chat = AgentChatDB.__new__(AgentChatDB)
    chat.agent_id = agent_id
    chat.db_path = str(tmp_path / f"{agent_id}_chat.db")
    chat._conn = None
    chat._lock = threading.Lock()
    chat._init_tables()
    agent_chat_manager._dbs[agent_id] = chat
    yield chat
    agent_chat_manager._dbs.pop(agent_id, None)


def test_keyed_remember_supersedes_previous_value(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user1', 'ch1')
    r1 = store_memory(agent_id, sid, 'Deploy target is X', key='user.deploy_target')
    assert r1.get('key') == 'user.deploy_target'
    assert 'superseded' not in r1

    r2 = store_memory(agent_id, sid, 'Deploy target is Y', key='user.deploy_target')
    assert r2.get('superseded'), 'second write must supersede the first'

    rows = db.get_all_memories(agent_id, include_expired=True)
    by_content = {m['content']: m for m in rows}
    old, new = by_content['Deploy target is X'], by_content['Deploy target is Y']
    assert old['superseded_by'] == new['id']
    assert new['superseded_by'] is None
    assert new['dimension'] == 'user.deploy_target'

    # Only the current value is active for the key.
    active = db.get_memories_by_dimension(agent_id, 'user.deploy_target')
    assert [m['content'] for m in active] == ['Deploy target is Y']


def test_keyed_pin_replaced_in_summary_not_appended(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user2', 'ch1')
    db.upsert_summary(sid, 'Prior summary text.', 42, 7,
                      agent_id=agent_id, last_message_ts=123456)

    store_memory(agent_id, sid, 'Deploy target is X', key='user.deploy_target')
    store_memory(agent_id, sid, 'Deploy target is Y', key='user.deploy_target')

    rec = db.get_summary(sid, agent_id=agent_id)
    summary = rec['summary']
    assert 'Prior summary text.' in summary
    assert summary.count('(noted:user.deploy_target)') == 1, \
        'same-key pin must be replaced in place, not appended'
    assert 'Deploy target is Y' in summary
    assert 'Deploy target is X' not in summary
    # Watermarks untouched by the replace path.
    assert rec['last_message_id'] == 42
    assert rec['message_count'] == 7
    assert rec['last_message_ts'] == 123456


def test_keyless_remember_writes_ledger_with_null_dimension(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user3', 'ch1')
    store_memory(agent_id, sid, 'Likes hiking')  # no key — legacy behavior
    rows = db.get_all_memories(agent_id)
    assert len(rows) == 1
    assert rows[0]['content'] == 'Likes hiking'
    assert rows[0]['dimension'] is None
    # Legacy pin format unchanged.
    rec = db.get_summary(sid, agent_id=agent_id)
    assert '- (noted) Likes hiking' in rec['summary']


def test_key_normalized_and_category_derived(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user4', 'ch1')
    store_memory(agent_id, sid, 'Uses dark theme', key='  Preference.Theme  ')
    rows = db.get_all_memories(agent_id)
    assert rows[0]['dimension'] == 'preference.theme'
    assert rows[0]['category'] == 'preference'


def test_recall_by_key_returns_current_value_only(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user5', 'ch1')
    store_memory(agent_id, sid, 'Deploy target is X', key='user.deploy_target')
    store_memory(agent_id, sid, 'Deploy target is Y', key='user.deploy_target')

    result = recall_by_key(agent_id, 'user.deploy_target')
    assert result['count'] == 1
    assert result['content'] == 'Deploy target is Y'

    missing = recall_by_key(agent_id, 'user.nope')
    assert missing['count'] == 0

    assert 'error' in recall_by_key(agent_id, '')


def test_get_active_dimensions_excludes_superseded_and_expired(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user6', 'ch1')
    store_memory(agent_id, sid, 'Deploy target is X', key='user.deploy_target')
    store_memory(agent_id, sid, 'Deploy target is Y', key='user.deploy_target')
    store_memory(agent_id, sid, 'DB is sqlite', key='decision.database')
    # Expire the decision key entirely.
    for m in db.get_memories_by_dimension(agent_id, 'decision.database'):
        db.expire_memory(agent_id, m['id'])

    dims = db.get_active_dimensions(agent_id)
    assert dims == ['user.deploy_target'], \
        'one entry per active key; superseded/expired rows must not add entries'
