"""Tests for the CMP navigation builtins (switch_path / new_path)."""

from unittest.mock import patch

from backend.agent_runtime.cmp import store
from backend.agent_state import AgentState
from backend.tools.registry import (
    _builtin_new_path_factory,
    _builtin_read_transcript_factory,
    _builtin_switch_path_factory,
)


def _ctx(ms=None, enable_cmp=True):
    return {'id': 'a1', 'session_id': 's1', 'enable_cmp': enable_cmp,
            'agent_state': ms if ms is not None else AgentState()}


def _executors(ctx):
    return (_builtin_switch_path_factory(ctx)[1],
            _builtin_new_path_factory(ctx)[1])


# ── Gates ────────────────────────────────────────────────────────────────────

def test_tools_hidden_without_flag():
    from backend.agent_runtime.context import build_tools
    agent = {'id': 'a1', 'is_super': False, 'builtin_tools_enabled': True}
    names = {t['function']['name'] for t in build_tools(agent)
             if t.get('function', {}).get('name')}
    assert 'switch_path' not in names and 'new_path' not in names

    agent.update({'enable_cmp': 1, 'enable_agent_state': 1})
    names = {t['function']['name'] for t in build_tools(agent)
             if t.get('function', {}).get('name')}
    assert 'switch_path' in names and 'new_path' in names


def test_executors_defend_when_disabled():
    switch, new = _executors(_ctx(enable_cmp=False))
    assert 'error' in switch({'path_id': 'A1'})
    assert 'error' in new({'title': 'x'})
    switch, new = _executors({'id': 'a1', 'enable_cmp': True})  # no agent_state
    assert 'error' in switch({'path_id': 'A1'})
    assert 'error' in new({'title': 'x'})


# ── new_path ─────────────────────────────────────────────────────────────────

def test_new_path_auto_inits_first_path_from_current_work():
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'done', 'dag': {'root_goal': 'build the todoweb app'}}
    _, new = _executors(_ctx(ms))
    result = new({'title': 'Invoice for client A', 'goal': 'make invoice',
                  'depends_on': ['A1']})
    assert result.get('path_id') == 'B1'
    # P1 adopted the ongoing work's identity and snapshot
    p1 = ms.cmp['paths']['A1']
    assert p1['title'] == 'build the todoweb app'
    assert p1['atg']['status'] == 'done'
    # fresh plan cycle for the new path
    assert ms.mode == 'plan' and ms.atg is None and ms.plan_file is None
    assert ms.auto_trivial is False


def test_new_path_starts_trivial_task_in_execute_mode():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='earlier task', now_ts=1000)
    _, new = _executors(_ctx(ms))
    with patch('backend.task_classifier.classify_task', return_value='trivial'):
        result = new({'title': 'Push origin dev',
                      'goal': 'now please push to origin dev'})
    assert result['path_id'] == 'A2'
    assert 'execute mode' in result['result']
    assert ms.mode == 'execute' and ms.auto_trivial is True
    assert ms.plan_file is None and ms.atg is None


def test_new_path_invalid_dependency():
    ms = AgentState()
    _, new = _executors(_ctx(ms))
    result = new({'title': 'x', 'depends_on': ['Z7']})
    assert 'Z7' in result['error']


def test_new_path_requires_title():
    _, new = _executors(_ctx(AgentState()))
    assert 'title' in new({'title': '  '})['error']


# ── switch_path ──────────────────────────────────────────────────────────────

def test_switch_path_round_trip():
    ms = AgentState(mode='execute')
    ms.plan_file = 'plan/one.md'
    _, new = _executors(_ctx(ms))
    new({'title': 'second task'})
    switch, _ = _executors(_ctx(ms))
    result = switch({'path_id': 'A1'})
    assert 'Switched to A1' in result['result']
    assert result['path']['id'] == 'A1'
    assert ms.cmp['active_id'] == 'A1'
    assert ms.plan_file == 'plan/one.md'  # restored


def test_switch_path_invalid_target_lists_valid_ids():
    ms = AgentState()
    switch, new = _executors(_ctx(ms))
    assert 'new_path' in switch({'path_id': 'A1'})['error']  # no paths yet
    new({'title': 'second'})
    err = switch({'path_id': 'Z9'})['error']
    assert 'A1' in err  # grounding: valid ids listed


def test_read_transcript_preserves_plural_attachment_metadata():
    ms = AgentState()
    ms.cmp = store.new_cmp(ms, title='image task', now_ts=1000)
    entries = [{
        'type': 'user', 'content': 'Compare the uploads.',
        'metadata': {'attachment_infos': [
            {'attachment_id': 117, 'filename': 'one.png', 'mime_type': 'image/png',
             'size_bytes': 10, 'file_path': 'data/one.png'},
            {'attachment_id': 118, 'filename': 'two.png', 'mime_type': 'image/png',
             'size_bytes': 20, 'file_path': 'data/two.png'},
        ]},
    }]
    fake_log = type('FakeLog', (), {
        'get_entries_after_ts': lambda self, start: entries,
        'get_entries_between_ts': lambda self, start, end: entries,
    })()
    with patch('models.chatlog.chatlog_manager.get', return_value=fake_log):
        read = _builtin_read_transcript_factory(_ctx(ms))[1]
        result = read({'path_id': 'A1'})['result']
    assert '[Attachment #1: one.png' in result
    assert 'Attachment ID: 117' in result
    assert '[Attachment #2: two.png' in result
    assert 'Attachment ID: 118' in result
