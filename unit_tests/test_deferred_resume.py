"""Auto-resume of user requests rejected while the agent was focus-busy.

Rejection records the session in AgentRuntime._deferred_resume_pending; the
debounced free hook drains it via resume_session once the agent is idle AND
unfocused, superseding the generic free-notification for the same session.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.agent_runtime as ar
from backend.agent_runtime import AgentRuntime, agent_runtime


@pytest.fixture(autouse=True)
def _clean_registries():
    """Isolate the class-level registries and timers between tests."""
    with AgentRuntime._deferred_resume_lock:
        AgentRuntime._deferred_resume_pending.clear()
    with AgentRuntime._free_notify_lock:
        AgentRuntime._free_notify_pending.clear()
    yield
    with ar._free_notify_timers_lock:
        for t in ar._free_notify_timers.values():
            t.cancel()
        ar._free_notify_timers.clear()
    with AgentRuntime._deferred_resume_lock:
        AgentRuntime._deferred_resume_pending.clear()
    with AgentRuntime._free_notify_lock:
        AgentRuntime._free_notify_pending.clear()


def _mock_chatlog(last_entry):
    """A ChatLog class mock whose context manager returns the given tail entry."""
    clog = MagicMock()
    clog.__enter__.return_value.get_last_entry.return_value = last_entry
    cls = MagicMock(return_value=clog)
    return cls


# --- registry -----------------------------------------------------------------

def test_busy_rejection_records_deferral():
    with patch.object(AgentRuntime, '_check_notify_opt_in', return_value=False), \
         patch('backend.agent_runtime.runtime.get_busy_message', return_value='Busy!'), \
         patch('backend.agent_runtime.runtime._db_retry'), \
         patch('backend.agent_runtime.runtime.chatlog_manager'):
        agent_runtime._handle_busy_rejection(
            'a1', SimpleNamespace(focus_reason='task'), 's1', 'u1', None, 'hello')
    assert AgentRuntime._deferred_resume_pending['a1']['s1'] == {
        'external_user_id': 'u1', 'channel_id': None}


def test_deferral_dedups_by_session():
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    AgentRuntime._queue_deferred_resume('a1', 's2', 'u2', 'ch9')
    assert len(AgentRuntime._deferred_resume_pending['a1']) == 2


# --- drain --------------------------------------------------------------------

def test_drain_resumes_with_channel_delivery():
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', 'ch9')
    tail = {'type': 'final', 'metadata': {'busy_rejection': True}}
    with patch('models.db.db.get_agent', return_value={'id': 'a1', 'enabled': True}), \
         patch('models.chatlog.ChatLog', _mock_chatlog(tail)), \
         patch.object(agent_runtime, 'resume_session') as resume:
        resumed = ar._drain_deferred_resumes('a1')
    resume.assert_called_once_with(
        {'id': 'a1', 'enabled': True}, 's1', 'u1', 'ch9', send_via_channel=True)
    assert resumed == {'s1'}
    assert 'a1' not in AgentRuntime._deferred_resume_pending  # registry drained


def test_drain_web_session_no_channel_delivery():
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    tail = {'type': 'user', 'metadata': {}}
    with patch('models.db.db.get_agent', return_value={'id': 'a1', 'enabled': True}), \
         patch('models.chatlog.ChatLog', _mock_chatlog(tail)), \
         patch.object(agent_runtime, 'resume_session') as resume:
        ar._drain_deferred_resumes('a1')
    resume.assert_called_once_with(
        {'id': 'a1', 'enabled': True}, 's1', 'u1', None, send_via_channel=False)


def test_drain_skips_session_already_answered():
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    tail = {'type': 'final', 'metadata': {}}  # a real answer landed meanwhile
    with patch('models.db.db.get_agent', return_value={'id': 'a1', 'enabled': True}), \
         patch('models.chatlog.ChatLog', _mock_chatlog(tail)), \
         patch.object(agent_runtime, 'resume_session') as resume:
        resumed = ar._drain_deferred_resumes('a1')
    resume.assert_not_called()
    assert resumed == set()


# --- free hook ----------------------------------------------------------------

def test_focused_agent_rearms_timer_and_keeps_registry():
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    with patch.object(agent_runtime, 'is_agent_busy', return_value=False), \
         patch.object(agent_runtime, '_restore_agent_state',
                      return_value=SimpleNamespace(focus=True)), \
         patch.object(agent_runtime, 'resume_session') as resume:
        ar._send_free_notification('a1')
    resume.assert_not_called()
    assert 's1' in AgentRuntime._deferred_resume_pending['a1']  # untouched
    with ar._free_notify_timers_lock:
        assert 'a1' in ar._free_notify_timers  # retry armed


def test_generic_notification_superseded_by_resume():
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    AgentRuntime._queue_free_notification('a1', 's1', 'u1', None)
    tail = {'type': 'user', 'metadata': {}}
    with patch.object(agent_runtime, 'is_agent_busy', return_value=False), \
         patch.object(agent_runtime, '_restore_agent_state', return_value=None), \
         patch('models.db.db.get_agent', return_value={'id': 'a1', 'enabled': True}), \
         patch('models.chatlog.ChatLog', _mock_chatlog(tail)), \
         patch.object(agent_runtime, 'resume_session') as resume, \
         patch('models.db.db.add_chat_message') as add_msg:
        ar._send_free_notification('a1')
    resume.assert_called_once()
    add_msg.assert_not_called()  # generic "I'm done" suppressed
    assert 'a1' not in AgentRuntime._free_notify_pending  # consumed


def test_busy_change_arms_timer_for_deferred_only():
    # No free-notification pending — deferral alone must arm the debounce timer.
    AgentRuntime._queue_deferred_resume('a1', 's1', 'u1', None)
    ar._on_agent_busy_changed({'agent_id': 'a1', 'busy': False})
    with ar._free_notify_timers_lock:
        assert 'a1' in ar._free_notify_timers


def test_resume_session_default_stays_channel_silent():
    import inspect
    sig = inspect.signature(agent_runtime.resume_session)
    assert sig.parameters['send_via_channel'].default is False
