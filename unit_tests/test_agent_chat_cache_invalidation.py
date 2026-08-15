"""
Tests for AgentChatManager cache invalidation on agent delete/recreate.

Regression: deleting an agent (which rmtrees its chat.db) while the
AgentChatManager still holds a cached AgentChatDB leaves a stale handle.
Recreating an agent with the same id returns that stale instance; if its
persistent connection was closed, _get_conn lazily opens a *fresh empty*
chat.db (mode=rwc) and _init_tables() never runs for it → queries fail
with 'no such table: chat_sessions'.
"""

import os
import shutil
import tempfile

import pytest

from models.chat import AgentChatManager, AGENTS_DIR


@pytest.fixture
def chat_env(monkeypatch, tmp_path):
    """Point AGENTS_DIR at a temp dir so tests never touch the real agents/."""
    agents_root = tmp_path / 'agents'
    agents_root.mkdir()
    monkeypatch.setattr('models.chat.AGENTS_DIR', str(agents_root))
    return str(agents_root)


def _create_and_use(mgr, agent_id):
    db = mgr.get(agent_id)
    with db._connect() as c:
        c.execute(
            "INSERT INTO chat_sessions (id, agent_id, external_user_id) VALUES (?, ?, ?)",
            ('s1', agent_id, 'u1'))
    return db


def test_recreate_after_delete_without_drop_fails(chat_env):
    """OLD behavior: stale cached instance + closed conn + rmtree → no such table."""
    mgr = AgentChatManager()
    agent_id = 'recreate_test'

    db1 = _create_and_use(mgr, agent_id)
    db1.close()                      # simulates GC/restart dropping the conn
    shutil.rmtree(os.path.join(chat_env, agent_id))
    os.makedirs(os.path.join(chat_env, agent_id))

    db2 = mgr.get(agent_id)          # cached stale instance
    assert db2 is db1                # same object → lazy reopen of empty file
    with pytest.raises(Exception) as exc:
        with db2._connect() as c:
            c.execute('SELECT * FROM chat_sessions').fetchall()
    assert 'no such table' in str(exc.value)


def test_drop_then_recreate_initializes_tables(chat_env):
    """NEW behavior: drop() invalidates the cache → fresh instance with tables."""
    mgr = AgentChatManager()
    agent_id = 'recreate_test'

    db1 = _create_and_use(mgr, agent_id)
    db1.close()
    shutil.rmtree(os.path.join(chat_env, agent_id))
    os.makedirs(os.path.join(chat_env, agent_id))

    mgr.drop(agent_id)               # the fix
    db3 = mgr.get(agent_id)
    assert db3 is not db1            # fresh AgentChatDB → _init_tables() ran
    with db3._connect() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'"
        ).fetchone()
    assert row is not None
    assert row[0] == 'chat_sessions'


def test_drop_missing_agent_is_noop(chat_env):
    mgr = AgentChatManager()
    mgr.drop('never_existed')        # should not raise
