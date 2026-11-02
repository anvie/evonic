"""agent_oneshot: a fresh session per question, the final answer returned, old sessions purged."""

import threading
import time
from unittest.mock import patch

import pytest

from backend import agent_oneshot
import importlib


def _am():
    """The agent_messaging module as it is right now (other tests may re-import it)."""
    return importlib.import_module('backend.tools.agent_messaging')


class FakeChat:
    def __init__(self, db_):
        self.db = db_

    def _connect(self):
        return self.db._connect()

    def delete_session(self, session_id):
        return self.db.delete_session(session_id)


class FakeDb:
    def __init__(self, agent=None):
        self.agent = agent if agent is not None else {'id': 'assistant', 'name': 'Assistant', 'enabled': 1}
        self.deleted = []

    def _chat_db(self, agent_id):
        return FakeChat(self)

    def get_agent(self, agent_id):
        return self.agent if agent_id == self.agent.get('id') else None

    def delete_session(self, session_id, agent_id=None):
        self.deleted.append(session_id)
        return True


def _answer_later(reply, delay=0.05):
    """A notify_agent stand-in that 'runs the agent' and pushes its final answer into the wait registry."""
    seen = []

    def notify(**kw):
        seen.append(kw)
        rid = kw['metadata']['reply_to_id']

        def run():
            time.sleep(delay)
            am = _am()
            with am._WAIT_REGISTRY_LOCK:
                q = am._WAIT_REGISTRY.pop(rid, None)
            if q is not None:
                q.put_nowait(reply)
        threading.Thread(target=run).start()
        return {'success': True, 'session_id': 'sess-' + kw['external_user_id'][-6:]}
    return notify, seen


def test_ask_returns_the_final_answer_from_a_fresh_session():
    notify, seen = _answer_later('{"ok": true}')
    with patch.object(agent_oneshot, 'db', FakeDb()), \
         patch('backend.agent_runtime.notifier.notify_agent', notify):
        a = agent_oneshot.ask('assistant', 'do the thing', tag='SYSTEM/Briefing', sender='briefer', timeout=10)
        b = agent_oneshot.ask('assistant', 'do the thing', tag='SYSTEM/Briefing', sender='briefer', timeout=10)
    assert a['success'] and a['reply'] == '{"ok": true}' and a['error'] is None
    assert seen[0]['tag'] == 'SYSTEM/Briefing' and seen[0]['message'] == 'do the thing' and seen[0]['dedup'] is False
    assert seen[0]['external_user_id'].startswith('__agent__oneshot__briefer_')
    assert agent_oneshot.is_oneshot_external_id(seen[0]['external_user_id'])
    assert seen[0]['external_user_id'] != seen[1]['external_user_id']          # a new session every time
    meta = seen[0]['metadata']
    assert meta['agent_message'] is True and meta['from_agent_id'] == seen[0]['external_user_id'][len('__agent__'):]
    assert meta['report_to_id'] is None and meta['agent_message_depth'] == 1
    assert _am()._WAIT_REGISTRY == {}                                              # nothing left waiting


def test_ask_reports_a_timeout_and_cleans_up_the_waiter():
    notify = lambda **kw: {'success': True, 'session_id': 's1'}
    with patch.object(agent_oneshot, 'db', FakeDb()), patch('backend.agent_runtime.notifier.notify_agent', notify):
        out = agent_oneshot.ask('assistant', 'x', timeout=5)
    assert out['success'] is False and out['timed_out'] is True and out['session_id'] == 's1'
    assert _am()._WAIT_REGISTRY == {}


def test_ask_reports_delivery_failures_missing_and_disabled_agents():
    with patch.object(agent_oneshot, 'db', FakeDb()), \
         patch('backend.agent_runtime.notifier.notify_agent', lambda **kw: {'success': False, 'reason': 'no_route'}):
        assert 'no_route' in agent_oneshot.ask('assistant', 'x', timeout=5)['error']
        assert 'not found' in agent_oneshot.ask('ghost', 'x')['error']
    with patch.object(agent_oneshot, 'db', FakeDb({'id': 'assistant', 'name': 'Assistant', 'enabled': 0})):
        assert 'disabled' in agent_oneshot.ask('assistant', 'x')['error']
    assert _am()._WAIT_REGISTRY == {}


def test_purge_removes_only_old_oneshot_sessions_of_that_sender(tmp_path):
    import sqlite3
    path = tmp_path / 'chat.db'
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chat_sessions (id TEXT, agent_id TEXT, external_user_id TEXT, archived INT DEFAULT 0, updated_at TEXT)")
    rows = [('old1', 'assistant', '__agent__oneshot__briefer_a1', 0, '2000-01-01 00:00:00'),
            ('new1', 'assistant', '__agent__oneshot__briefer_b2', 0, time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())),
            ('other', 'assistant', '__agent__oneshot__someone_c3', 0, '2000-01-01 00:00:00'),
            ('human', 'assistant', 'web-user', 0, '2000-01-01 00:00:00'),
            ('done', 'assistant', '__agent__oneshot__briefer_d4', 1, '2000-01-01 00:00:00'),
            ('wild', 'assistant', '__agent__oneshot__brieXfer_e5', 0, '2000-01-01 00:00:00')]
    conn.executemany("INSERT INTO chat_sessions VALUES (?,?,?,?,?)", rows)
    conn.commit()
    fake = FakeDb()
    fake._connect = lambda: sqlite3.connect(path)
    with patch.object(agent_oneshot, 'db', fake):
        assert agent_oneshot.purge('assistant', sender='briefer', older_than_seconds=3600) == 1
    assert fake.deleted == ['old1']            # not the recent one, other senders, people, archived ones, or a look-alike sender


def test_ordinary_agent_to_agent_sessions_are_not_oneshot():
    assert not agent_oneshot.is_oneshot_external_id('__agent__aisyah')
    assert not agent_oneshot.is_oneshot_external_id('web_user') and not agent_oneshot.is_oneshot_external_id(None)


def test_a_focused_agent_is_not_interrupted():
    class Focused(FakeDb):
        def get_agent_state(self, agent_id=None):
            from backend.agent_state import AgentState
            st = AgentState()
            st.focus, st.focus_reason = True, 'working on a task'
            return st.serialize()
    fake = Focused({'id': 'asep', 'name': 'Asep', 'enabled': 1, 'enable_agent_state': 1})
    sent = []
    with patch.object(agent_oneshot, 'db', fake), \
         patch('backend.agent_runtime.notifier.notify_agent', lambda **kw: sent.append(kw) or {'success': True, 'session_id': 's'}):
        out = agent_oneshot.ask('asep', 'q', timeout=5)
    assert out['success'] is False and 'focused' in out['error'] and 'working on a task' in out['error'] and sent == []


def test_the_senders_messaging_acl_is_respected():
    class Acl(FakeDb):
        def get_agent(self, agent_id):
            return {'asep': {'id': 'asep', 'name': 'Asep', 'enabled': 1},
                    'assistant': {'id': 'assistant', 'name': 'Assistant', 'enabled': 1, 'messaging_acl': '["linus"]',
                              'messaging_acl_mode': 'whitelist'}}.get(agent_id)
    sent = []
    with patch.object(agent_oneshot, 'db', Acl()), \
         patch('backend.agent_runtime.notifier.notify_agent', lambda **kw: sent.append(kw) or {'success': True, 'session_id': 's'}):
        out = agent_oneshot.ask('asep', 'q', timeout=5, acl_sender='assistant')
    assert out['success'] is False and 'not allowed' in out['error'] and sent == []


def test_one_shot_sessions_are_not_turned_into_knowledge():
    from backend.agent_runtime import _on_summary_updated
    import backend.agent_runtime.memory_manager as mm
    calls = []

    class Db:
        def get_agent(self, agent_id):
            return {'id': agent_id, 'name': 'Asep'}

        def get_session_with_details(self, session_id):
            return {'oneshot': {'external_user_id': '__agent__oneshot__verifier_ab12'},
                    'a2a': {'external_user_id': '__agent__aisyah'}}[session_id]

    def run(session_id):
        with patch('models.db.db', Db()), patch.object(mm, 'process_knowledge', lambda *a, **k: calls.append(session_id)), \
             patch('threading.Thread', lambda target, daemon=None: type('T', (), {'start': staticmethod(target)})()):
            _on_summary_updated({'agent_id': 'asep', 'session_id': session_id, 'summary': 'a summary'})
    run('oneshot')
    run('a2a')
    assert calls == ['a2a']                      # ordinary conversations still become knowledge


# ── save_memory / ephemeral on notify_agent ──

def test_flags_keep_a_session_out_of_memory_and_let_it_expire(tmp_path):
    import sqlite3, time as _t
    from backend import ephemeral_sessions as es

    class Flags:
        def __init__(self):
            self.rows, self.deleted = {}, []
        def set_session_flags(self, sid, agent_id, *, save_memory=True, expires_at=None):
            row = self.rows.setdefault(sid, {'agent': agent_id, 'no_memory': False, 'expires_at': None})
            row['no_memory'] = row['no_memory'] or not save_memory
            row['expires_at'] = expires_at if expires_at is not None else row['expires_at']
        def get_session_flags(self, sid):
            r = self.rows.get(sid) or {}
            return {'no_memory': bool(r.get('no_memory')), 'expires_at': r.get('expires_at')}
        def expired_session_flags(self, now=None):
            return [{'session_id': s, 'agent_id': r['agent']} for s, r in self.rows.items()
                    if r['expires_at'] is not None and r['expires_at'] <= (now or _t.time())]
        def drop_session_flags(self, sid):
            self.rows.pop(sid, None)
        def delete_session(self, sid, agent_id=None):
            self.deleted.append(sid)
            return True
    fake = Flags()
    with patch.object(es, 'db', fake):
        es.mark('s-private', 'asep', save_memory=False)                       # not remembered, never expires
        es.mark('s-brief', 'asep', save_memory=False, ttl=3600)               # not remembered, removed in an hour
        es.mark('s-normal', 'asep')                                           # an ordinary session: untouched
        assert es.is_memory_blocked('s-private') and es.is_memory_blocked('s-brief') and not es.is_memory_blocked('s-normal')
        assert es.purge_expired() == 0 and fake.deleted == []
        assert es.purge_expired(now=_t.time() + 7200) == 1 and fake.deleted == ['s-brief']
        assert 's-brief' not in fake.rows and 's-private' in fake.rows        # the private one is kept until something removes it


def test_notify_agent_flags_the_session_and_marks_the_message():
    from backend.agent_runtime import notifier
    marked, saved = [], []
    with patch('backend.ephemeral_sessions.mark', lambda *a, **k: marked.append((a, k))), \
         patch.object(notifier, 'db') as fake_db, \
         patch('backend.agent_runtime.agent_runtime.handle_message',
               lambda *a, **k: saved.append(k.get('metadata')) or {'success': True}, create=True):
        fake_db.get_or_create_session.return_value = 'sess-1'
        out = notifier.notify_agent('asep', 'SYSTEM/Briefing', 'hello', external_user_id='__agent__oneshot__x_1', dedup=False,
                                    save_memory=False, ephemeral=True, ephemeral_ttl=600)
    assert marked and marked[0][0][:2] == ('sess-1', 'asep') and marked[0][1] == {'save_memory': False, 'ttl': 600}
    assert out['success'] and saved[0]['no_memory'] is True


def test_an_existing_conversation_is_not_flagged_when_only_one_message_is_not_to_be_remembered():
    from backend.agent_runtime import notifier
    marked, saved = [], []
    with patch('backend.ephemeral_sessions.mark', lambda *a, **k: marked.append((a, k))), \
         patch.object(notifier, 'db') as fake_db, \
         patch('backend.agent_runtime.agent_runtime.handle_message',
               lambda *a, **k: saved.append(k.get('metadata')) or {'success': True}, create=True):
        fake_db.get_session_with_details.return_value = {'id': 'web-1', 'agent_id': 'asep', 'external_user_id': 'web_test', 'channel_id': None}
        notifier.notify_agent('asep', 'NOTICE', 'hello', session_id='web-1', dedup=False, trigger_llm=False, save_memory=False)
    assert all(k.get('save_memory') is True for _, k in marked)          # the session itself stays a normal, remembered one
    assert not marked or marked[0][1].get('ttl') is None


def test_a_session_that_is_not_remembered_cannot_save_to_memory_or_the_knowledge_base():
    from backend import ephemeral_sessions as es
    background = {'user_id': '__agent__oneshot__briefer_ab12', 'session_id': 's-bg'}
    ordinary = {'user_id': 'web_test', 'session_id': 's-chat'}
    with patch.object(es, 'is_memory_blocked', lambda sid: sid == 's-flagged'):
        for tool in ('remember', 'memorize', 'save_memory', 'evomem', 'forget_memory'):
            assert 'not remembered' in es.refuse_memory_write(tool, {}, background)['error']
        assert es.refuse_memory_write('write_file', {'path': '/_self/kb/notes.md'}, background)
        assert es.refuse_memory_write('patch', {'file_path': '/workspace/_self/kb/x.md'}, background)
        assert es.refuse_memory_write('write_file', {'path': '/workspace/report.md'}, background) is None      # ordinary files are fine
        assert es.refuse_memory_write('recall', {}, background) is None                                          # reading is fine
        assert es.refuse_memory_write('remember', {}, {'user_id': 'web_test', 'session_id': 's-flagged'})        # flagged session
        assert es.refuse_memory_write('remember', {}, ordinary) is None                                          # normal chats keep memory
        assert es.refuse_memory_write('remember', {}, None) is None


def test_a_turn_started_by_a_message_that_is_not_to_be_remembered_cannot_write_memory_either():
    from backend import ephemeral_sessions as es
    ordinary_chat = {'user_id': 'web_test', 'session_id': 's-chat'}
    with patch.object(es, 'is_memory_blocked', lambda sid: False):
        assert es.refuse_memory_write('remember', {}, {**ordinary_chat, 'no_memory_turn': True})['error']
        assert es.refuse_memory_write('write_file', {'path': '/_self/kb/x.md'}, {**ordinary_chat, 'no_memory_turn': True})
        assert es.refuse_memory_write('remember', {}, ordinary_chat) is None                    # the next turn is normal again
        assert es.refuse_memory_write('recall', {}, {**ordinary_chat, 'no_memory_turn': True}) is None
