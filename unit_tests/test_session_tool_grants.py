"""The session tool grant table: lend, read, expire, revoke — and it goes away with its session.

A grant is what lets an agent keep a plugin's tool for the rest of an agent-to-agent conversation without
the tool ever entering its assignment, so these rows must be scoped to one session, must expire on their
own, and must never outlive the session they belong to.
"""

import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.chat import AgentChatDB, _migrate_session_id, agent_chat_manager  # noqa: E402

OWNER = 'privnt'
QUEUE = 'plugin:privnt:privnt_digest_queue'
OTHER = 'plugin:privnt:privnt_other'


@pytest.fixture
def chat(tmp_path):
    """A per-agent chat DB in a temp directory, registered with the manager."""
    agent_id = 'grant_' + uuid.uuid4().hex[:8]
    instance = AgentChatDB.__new__(AgentChatDB)
    instance.agent_id = agent_id
    instance.db_path = str(tmp_path / f'{agent_id}_chat.db')
    instance._conn = None
    instance._lock = threading.Lock()
    instance._init_tables()
    agent_chat_manager._dbs[agent_id] = instance
    yield instance
    agent_chat_manager._dbs.pop(agent_id, None)


@pytest.fixture
def session(chat):
    return chat.get_or_create_session(chat.agent_id, '__agent__privnt')


def tool_ids(chat, session_id):
    return sorted(row['tool_id'] for row in chat.list_session_tool_grants(session_id))


def test_a_granted_tool_is_remembered_with_its_owner(chat, session):
    chat.grant_session_tools(session, [QUEUE, OTHER], OWNER, granted_by='privnt')
    rows = chat.list_session_tool_grants(session)
    assert sorted(r['tool_id'] for r in rows) == [QUEUE, OTHER]
    assert {r['owner'] for r in rows} == {OWNER}
    assert {r['granted_by'] for r in rows} == {'privnt'}


def test_regranting_refreshes_the_expiry_instead_of_duplicating(chat, session):
    chat.grant_session_tools(session, [QUEUE], OWNER, ttl_seconds=60)
    first = chat.list_session_tool_grants(session)[0]['expires_at']
    chat.grant_session_tools(session, [QUEUE], OWNER, ttl_seconds=3600)
    rows = chat.list_session_tool_grants(session)
    assert len(rows) == 1 and rows[0]['expires_at'] > first


def test_an_expired_grant_is_dropped_when_it_is_read(chat, session):
    chat.grant_session_tools(session, [QUEUE], OWNER)
    with chat._connect() as conn:
        conn.execute("UPDATE session_tool_grants SET expires_at = ? WHERE session_id = ?",
                     ('2000-01-01T00:00:00+00:00', session))
        conn.commit()
    assert chat.list_session_tool_grants(session) == []
    assert tool_ids(chat, session) == []                 # the row is gone, not just filtered


def test_grants_can_be_revoked_one_by_one_or_all_at_once(chat, session):
    chat.grant_session_tools(session, [QUEUE, OTHER], OWNER)
    assert chat.revoke_session_tools(session, tool_ids=[QUEUE]) == 1
    assert tool_ids(chat, session) == [OTHER]
    assert chat.revoke_session_tools(session) == 1
    assert tool_ids(chat, session) == []


def test_a_cleared_or_deleted_session_takes_its_grants_with_it(chat, session):
    chat.grant_session_tools(session, [QUEUE], OWNER)
    chat.clear_session(session)
    assert tool_ids(chat, session) == []

    chat.grant_session_tools(session, [QUEUE], OWNER)
    assert chat.delete_session(session) is True
    assert tool_ids(chat, session) == []


def test_grants_follow_a_renamed_session(chat, session):
    chat.grant_session_tools(session, [QUEUE], OWNER)
    renamed = session + '-renamed'
    with chat._connect() as conn:
        _migrate_session_id(conn.cursor(), session, renamed)
        conn.commit()
    assert tool_ids(chat, session) == []
    assert tool_ids(chat, renamed) == [QUEUE]
