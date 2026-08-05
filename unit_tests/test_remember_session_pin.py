"""Verify the `remember` tool pins facts into the running session summary
instantly (no LLM), and that the summary survives re-pinning. Ledger-write and
keyed-supersession behavior is covered in test_keyed_ledger.py."""

import os
import sys
import uuid
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db, AgentChatDB, agent_chat_manager
from backend.agent_runtime.memory_manager import store_memory


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


def test_remember_creates_summary_with_bullet(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user1', 'ch1')
    resp = store_memory(agent_id, sid, 'User prefers English', category='preference')
    assert 'error' not in resp
    assert resp['result'].startswith('Noted')

    rec = db.get_summary(sid, agent_id=agent_id)
    assert rec is not None
    assert '- (noted, preference) User prefers English' in rec['summary']


def test_remember_appends_to_existing_summary_preserving_watermarks(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user2', 'ch1')
    # Seed an existing summary with real watermarks (as the summarizer would).
    db.upsert_summary(sid, 'Prior summary text.', 42, 7,
                      agent_id=agent_id, last_message_ts=123456)

    store_memory(agent_id, sid, 'Phone is 555-0100', category='user_info')

    rec = db.get_summary(sid, agent_id=agent_id)
    # Original content retained, fact appended, watermarks untouched.
    assert 'Prior summary text.' in rec['summary']
    assert '- (noted, user_info) Phone is 555-0100' in rec['summary']
    assert rec['last_message_id'] == 42
    assert rec['message_count'] == 7
    assert rec['last_message_ts'] == 123456


def test_remember_general_category_has_no_tag(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user3', 'ch1')
    store_memory(agent_id, sid, 'Likes hiking')  # default category 'general'
    rec = db.get_summary(sid, agent_id=agent_id)
    assert '- (noted) Likes hiking' in rec['summary']


def test_remember_rejects_empty_and_missing_session(agent_id, chat_db):
    sid = db.get_or_create_session(agent_id, 'user4', 'ch1')
    assert 'error' in store_memory(agent_id, sid, '   ')
    assert 'error' in store_memory(agent_id, '', 'something')
