"""agent_oneshot.ask: a key names the session, so calls with the same key share it; without one each call is new."""
import threading

import pytest

from backend import agent_oneshot
from backend.agent_runtime import notifier
from backend.tools import agent_messaging as am


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_oneshot.db, 'get_agent', lambda agent_id: {'id': agent_id, 'enabled': True})

    def notify(**kw):
        calls.append(kw)
        reply_to = kw['metadata']['reply_to_id']
        threading.Timer(0.01, lambda: am._WAIT_REGISTRY[reply_to].put('{"a": 1}')).start()
        return {'success': True, 'session_id': 's-' + kw['external_user_id']}
    monkeypatch.setattr(notifier, 'notify_agent', notify)
    return calls


def test_a_key_names_the_session_and_is_reused(sent):
    for _ in range(2):
        out = agent_oneshot.ask('worker', 'hi', sender='privntreq', key='item123', timeout=5)
        assert out['success'] and out['reply'] == '{"a": 1}'
    assert [c['external_user_id'] for c in sent] == ['__agent__oneshot__privntreq_item123'] * 2
    assert agent_oneshot.is_oneshot_external_id(sent[0]['external_user_id'])


def test_without_a_key_every_call_gets_its_own_session(sent):
    agent_oneshot.ask('worker', 'hi', sender='privntreq', timeout=5)
    agent_oneshot.ask('worker', 'hi', sender='privntreq', timeout=5)
    first, second = (c['external_user_id'] for c in sent)
    assert first != second and first.startswith('__agent__oneshot__privntreq_')


@pytest.mark.parametrize('key', ['', 'a b', 'x/y', 'k' * 65])
def test_a_key_that_cannot_name_a_session_is_refused(sent, key):
    out = agent_oneshot.ask('worker', 'hi', sender='privntreq', key=key, timeout=5)
    assert not out['success'] and 'session key' in out['error'] and sent == []
