"""
Unit tests for chat session lifecycle: create, messages, delete, context clearing.
Ensures deleting a session fully removes all messages so the bot loses context.
"""

import pytest
import json
import sys
import os
import uuid
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db, AgentChatDB, agent_chat_manager


def _auth(client):
    """Authenticate the test client session."""
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    return client


@pytest.fixture
def agent_id(tmp_path):
    """Create a test agent and return its ID.

    Self-isolating: saves/restores ``db.db_path`` so it never touches the
    production database even if the ``use_test_database`` autouse fixture
    fails to isolate correctly.
    """
    aid = f"test_agent_{uuid.uuid4().hex[:8]}"

    # Save the current db_path (already patched by use_test_database, or
    # the original production path if that fixture didn't run).
    original_path = db.db_path

    # Point the singleton at a fresh temp DB just for this fixture.
    db.db_path = str(tmp_path / f"{aid}_db.sqlite")
    # Nuke the thread-local cached connection so _connect() opens the new
    # path instead of reusing a stale handle pointing at the old path.
    db._tls.conn = None
    db._init_tables()

    db.create_agent({'id': aid, 'name': 'Test Agent', 'system_prompt': 'You are a test bot.'})
    # Ensure a super agent exists so Flask's enforce_auth doesn't return 503.
    if not db.has_super_agent():
        db.create_agent({'id': 'test_super', 'name': 'Super', 'system_prompt': '', 'is_super': True})
    yield aid

    # Teardown: restore the original path and clear the connection again
    # so the next test's fixture gets a clean slate.
    db._tls.conn = None
    db.db_path = original_path


@pytest.fixture
def chat_db(agent_id, tmp_path):
    """Create a per-agent chat DB in a temp directory."""
    chat = AgentChatDB.__new__(AgentChatDB)
    chat.agent_id = agent_id
    chat.db_path = str(tmp_path / f"{agent_id}_chat.db")
    chat._conn = None
    chat._lock = threading.Lock()
    chat._init_tables()
    # Inject into manager so db._chat_db(agent_id) finds it
    agent_chat_manager._dbs[agent_id] = chat
    yield chat
    agent_chat_manager._dbs.pop(agent_id, None)


class TestSessionCreate:
    def test_create_new_session(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1', 'ch1')
        assert sid is not None
        assert len(sid) > 0

    def test_same_user_returns_same_session(self, agent_id, chat_db):
        sid1 = db.get_or_create_session(agent_id, 'user1', 'ch1')
        sid2 = db.get_or_create_session(agent_id, 'user1', 'ch1')
        assert sid1 == sid2

    def test_different_user_returns_different_session(self, agent_id, chat_db):
        sid1 = db.get_or_create_session(agent_id, 'user1', 'ch1')
        sid2 = db.get_or_create_session(agent_id, 'user2', 'ch1')
        assert sid1 != sid2

    def test_no_channel_session(self, agent_id, chat_db):
        sid1 = db.get_or_create_session(agent_id, 'user1')
        sid2 = db.get_or_create_session(agent_id, 'user1')
        assert sid1 == sid2


class TestSessionMessages:
    def test_add_and_retrieve_messages(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        db.add_chat_message(sid, 'user', 'hello', agent_id=agent_id)
        db.add_chat_message(sid, 'assistant', 'hi there', agent_id=agent_id)

        msgs = db.get_session_messages(sid, agent_id=agent_id)
        user_msgs = [m for m in msgs if m['role'] == 'user']
        asst_msgs = [m for m in msgs if m['role'] == 'assistant']
        assert len(user_msgs) == 1
        assert len(asst_msgs) == 1
        assert user_msgs[0]['content'] == 'hello'
        assert asst_msgs[0]['content'] == 'hi there'

    def test_messages_ordered_by_id(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        id1 = db.add_chat_message(sid, 'user', 'msg1', agent_id=agent_id)
        id2 = db.add_chat_message(sid, 'assistant', 'msg2', agent_id=agent_id)
        id3 = db.add_chat_message(sid, 'user', 'msg3', agent_id=agent_id)

        # IDs should be strictly increasing
        assert id1 < id2 < id3

        msgs, _ = db.get_session_messages_full(sid, agent_id=agent_id)
        contents = [m['content'] for m in msgs]
        assert contents == ['msg1', 'msg2', 'msg3']

    def test_metadata_persisted(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        timeline = [{'type': 'thinking', 'content': 'I am thinking'}]
        db.add_chat_message(sid, 'assistant', 'response', agent_id=agent_id,
                            metadata={'timeline': timeline})

        msgs = db.get_session_messages(sid, agent_id=agent_id)
        asst = [m for m in msgs if m['role'] == 'assistant'][0]
        assert asst['metadata'] is not None
        assert asst['metadata']['timeline'][0]['type'] == 'thinking'

    def test_messages_full_returns_metadata(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        db.add_chat_message(sid, 'user', 'hi', agent_id=agent_id)
        db.add_chat_message(sid, 'assistant', 'hello', agent_id=agent_id,
                            metadata={'timeline': [{'type': 'thinking', 'content': 'hmm'}]})

        msgs, _ = db.get_session_messages_full(sid, agent_id=agent_id)
        assert len(msgs) == 2
        asst = [m for m in msgs if m['role'] == 'assistant'][0]
        assert asst['metadata']['timeline'][0]['content'] == 'hmm'

    def test_new_messages_polling(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        id1 = db.add_chat_message(sid, 'user', 'first', agent_id=agent_id)
        id2 = db.add_chat_message(sid, 'assistant', 'second', agent_id=agent_id)
        id3 = db.add_chat_message(sid, 'user', 'third', agent_id=agent_id)

        # Poll after id1 should return id2 and id3
        new = db.get_new_messages(sid, id1, agent_id=agent_id)
        assert len(new) == 2
        assert new[0]['content'] == 'second'
        assert new[1]['content'] == 'third'

        # Poll after id3 should return nothing
        new = db.get_new_messages(sid, id3, agent_id=agent_id)
        assert len(new) == 0


class TestAgentRequestMetadata:
    def test_intervening_tool_and_assistant_messages_do_not_affect_lookup(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, '__agent__sender')
        expected = {
            'agent_message': True,
            'from_agent_id': 'sender',
            'report_to_id': 'user-1',
        }
        db.add_chat_message(sid, 'user', 'delegate', metadata=expected, agent_id=agent_id)
        for i in range(25):
            db.add_chat_message(sid, 'assistant', f'call {i}', agent_id=agent_id)
            db.add_chat_message(sid, 'tool', f'result {i}', agent_id=agent_id)

        assert db.get_latest_agent_request_metadata(
            sid, agent_id=agent_id, sender_agent_id='sender',
        ) == expected

    def test_newer_unroutable_request_does_not_shadow_routable_delegation(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, '__agent__sender')
        expected = {
            'agent_message': True,
            'from_agent_id': 'sender',
            'report_to_id': 'user-1',
        }
        db.add_chat_message(sid, 'user', 'delegate', metadata=expected, agent_id=agent_id)
        db.add_chat_message(sid, 'user', 'background notification', metadata={
            'agent_message': True,
            'from_agent_id': 'sender',
        }, agent_id=agent_id)

        assert db.get_latest_agent_request_metadata(
            sid, agent_id=agent_id, sender_agent_id='sender',
        ) == expected

    def test_sender_isolation_and_compact_json(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, '__agent__sender')
        expected = {
            'agent_message': True,
            'from_agent_id': 'sender',
            'report_to_id': 'user-1',
        }
        db.add_chat_message(sid, 'user', 'other sender', metadata={
            'agent_message': True,
            'from_agent_id': 'other',
            'report_to_id': 'wrong-user',
        }, agent_id=agent_id)
        with chat_db._connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, metadata) VALUES (?, 'user', ?, ?)",
                (sid, 'compact', json.dumps(expected, separators=(',', ':'))),
            )

        assert db.get_latest_agent_request_metadata(
            sid, agent_id=agent_id, sender_agent_id='sender',
        ) == expected
        assert db.get_latest_agent_request_metadata(
            sid, agent_id=agent_id, sender_agent_id='missing',
        ) is None

    def test_invalid_and_non_agent_metadata_are_skipped(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, '__agent__sender')
        expected = {
            'agent_message': True,
            'from_agent_id': 'sender',
            'report_to_id': 'user-1',
        }
        db.add_chat_message(sid, 'user', 'delegate', metadata=expected, agent_id=agent_id)
        db.add_chat_message(sid, 'assistant', 'not a request', metadata={
            'agent_message': True,
            'from_agent_id': 'sender',
            'report_to_id': 'wrong-assistant-route',
        }, agent_id=agent_id)
        db.add_chat_message(sid, 'user', 'ordinary user', metadata={
            'from_agent_id': 'sender',
            'report_to_id': 'wrong-non-agent-route',
        }, agent_id=agent_id)
        with chat_db._connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, metadata) VALUES (?, 'user', ?, ?)",
                (sid, 'malformed', '{not-json'),
            )

        assert db.get_latest_agent_request_metadata(
            sid, agent_id=agent_id, sender_agent_id='sender',
        ) == expected


class TestSessionDelete:
    """Critical tests: deleting a session must fully remove all context."""

    def test_delete_removes_session(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1', 'ch1')
        db.add_chat_message(sid, 'user', 'hello', agent_id=agent_id)

        result = db.delete_session(sid, agent_id=agent_id)
        assert result is True

        # Session should not exist
        assert chat_db.has_session(sid) is False

    def test_delete_removes_all_messages(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1', 'ch1')
        for i in range(10):
            db.add_chat_message(sid, 'user', f'msg {i}', agent_id=agent_id)
            db.add_chat_message(sid, 'assistant', f'reply {i}', agent_id=agent_id)

        msgs_before = db.get_session_messages(sid, agent_id=agent_id)
        assert len(msgs_before) == 20

        db.delete_session(sid, agent_id=agent_id)

        msgs_after = db.get_session_messages(sid, agent_id=agent_id)
        assert len(msgs_after) == 0

    def test_delete_then_new_session_has_no_history(self, agent_id, chat_db):
        """After delete, the session is archived; next get_or_create reactivates it with no history."""
        sid1 = db.get_or_create_session(agent_id, 'telegram_user', 'tg_channel')
        # Simulate 10 conversations
        for i in range(10):
            db.add_chat_message(sid1, 'user', f'Question {i}', agent_id=agent_id)
            db.add_chat_message(sid1, 'assistant', f'Answer {i}', agent_id=agent_id)

        msgs = db.get_session_messages(sid1, agent_id=agent_id)
        assert len(msgs) == 20

        # Delete session (archives it and clears messages)
        db.delete_session(sid1, agent_id=agent_id)

        # Simulate next Telegram message — archived session is reactivated (same stable ID)
        sid2 = db.get_or_create_session(agent_id, 'telegram_user', 'tg_channel')

        # Reactivated session must have zero history
        msgs = db.get_session_messages(sid2, agent_id=agent_id)
        assert len(msgs) == 0

        # get_session_messages_full also empty
        msgs_full, _ = db.get_session_messages_full(sid2, agent_id=agent_id)
        assert len(msgs_full) == 0

    def test_delete_without_agent_id(self, agent_id, chat_db):
        """Test delete via _find_agent_for_session path (like the API does)."""
        sid = db.get_or_create_session(agent_id, 'user1', 'ch1')
        db.add_chat_message(sid, 'user', 'test', agent_id=agent_id)

        # Delete without passing agent_id — must still work
        result = db.delete_session(sid)
        assert result is True
        assert chat_db.has_session(sid) is False

    def test_delete_nonexistent_session(self, agent_id, chat_db):
        result = db.delete_session('nonexistent-session-id', agent_id=agent_id)
        assert result is False

    def test_clear_session_removes_messages_but_keeps_session(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        db.add_chat_message(sid, 'user', 'hello', agent_id=agent_id)
        db.add_chat_message(sid, 'assistant', 'hi', agent_id=agent_id)

        db.clear_session(sid, agent_id=agent_id)

        # Session still exists
        assert chat_db.has_session(sid) is True
        # But messages are gone
        msgs = db.get_session_messages(sid, agent_id=agent_id)
        assert len(msgs) == 0


class TestSessionDeleteAPI:
    """Test the /api/sessions/<id> DELETE endpoint end-to-end."""

    def test_api_delete_clears_context(self, agent_id, chat_db):
        from app import app

        # Create session with messages
        sid = db.get_or_create_session(agent_id, 'tg_user_123', 'tg_ch')
        for i in range(5):
            db.add_chat_message(sid, 'user', f'Q{i}', agent_id=agent_id)
            db.add_chat_message(sid, 'assistant', f'A{i}', agent_id=agent_id)

        with app.test_client() as client:
            _auth(client)
            # Delete via API
            resp = client.delete(f'/api/sessions/{sid}')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is True

        # Verify context is gone — archived session is reactivated with no messages
        sid2 = db.get_or_create_session(agent_id, 'tg_user_123', 'tg_ch')
        msgs = db.get_session_messages(sid2, agent_id=agent_id)
        assert len(msgs) == 0

    def test_api_get_session_after_delete_returns_404(self, agent_id, chat_db):
        from app import app

        sid = db.get_or_create_session(agent_id, 'user1')
        db.add_chat_message(sid, 'user', 'test', agent_id=agent_id)
        db.delete_session(sid, agent_id=agent_id)

        with app.test_client() as client:
            _auth(client)
            resp = client.get(f'/api/sessions/{sid}')
            assert resp.status_code == 404


class TestBotToggle:
    def test_bot_enabled_by_default(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')
        assert db.is_session_bot_enabled(sid, agent_id=agent_id) is True

    def test_disable_and_enable_bot(self, agent_id, chat_db):
        sid = db.get_or_create_session(agent_id, 'user1')

        db.set_session_bot_enabled(sid, False, agent_id=agent_id)
        assert db.is_session_bot_enabled(sid, agent_id=agent_id) is False

        db.set_session_bot_enabled(sid, True, agent_id=agent_id)
        assert db.is_session_bot_enabled(sid, agent_id=agent_id) is True


class TestGetAllSessions:
    """Tests for get_all_sessions which queries the session_index table."""

    @pytest.fixture(autouse=True)
    def _patch_agents_dir(self, monkeypatch, agent_id, chat_db, tmp_path):
        """get_all_sessions now queries session_index (populated by
        _sync_session_index called from get_or_create_session), so no
        filesystem-level setup is required. The chat_db fixture already
        injects the per-agent DB into agent_chat_manager._dbs."""
        pass

    def test_lists_sessions_across_agents(self, chat_db, agent_id):
        db.get_or_create_session(agent_id, 'user1')
        db.get_or_create_session(agent_id, 'user2')

        sessions, total = db.get_all_sessions()
        agent_sessions = [s for s in sessions if s['agent_id'] == agent_id]
        assert len(agent_sessions) == 2
        assert total >= 2

    def test_search_filters_by_user_id(self, chat_db, agent_id):
        db.get_or_create_session(agent_id, 'findme_123')
        db.get_or_create_session(agent_id, 'other_user')

        sessions, total = db.get_all_sessions(search='findme')
        assert total >= 1
        assert any(s['external_user_id'] == 'findme_123' for s in sessions)

    def test_search_filters_by_agent_name(self, chat_db, agent_id):
        db.get_or_create_session(agent_id, 'user1')

        sessions, total = db.get_all_sessions(search='Test Agent')
        assert total >= 1

    def test_lists_subagent_session_stored_in_parent_chat_db(self, chat_db, agent_id):
        subagent_id = f'{agent_id}_sub_1'
        session_id = db.get_or_create_session(
            subagent_id, 'subagent_user', db_agent_id=agent_id,
        )
        db.add_chat_message(
            session_id, 'user', 'delegated task', agent_id=subagent_id,
            db_agent_id=agent_id,
        )

        sessions, _ = db.get_all_sessions(exclude_test=False)
        session = next(s for s in sessions if s['id'] == session_id)
        assert session['agent_id'] == subagent_id
        assert session['last_message'] == 'delegated task'


class TestAgentCommandDiscoveryAPI:
    def test_returns_sorted_commands_available_to_agent(self, agent_id, chat_db):
        from app import app

        with app.test_client() as client:
            _auth(client)
            response = client.get(f'/api/agents/{agent_id}/commands')

        assert response.status_code == 200
        commands = response.get_json()['commands']
        names = [command['name'] for command in commands]
        assert names == sorted(names)
        assert 'help' in names
        assert 'restart' not in names
        assert all({'name', 'description', 'accepts_args', 'parameters'}.issubset(set(command)) for command in commands)

    def test_returns_404_for_unknown_agent(self, agent_id, chat_db):
        from app import app

        with app.test_client() as client:
            _auth(client)
            response = client.get('/api/agents/missing_agent/commands')

        assert response.status_code == 404
