"""Workplace switch note: wording, and holding it while a turn is running."""

from backend.agent_runtime.runtime import AgentRuntime, _SessionStore
from backend.workplaces.switch_note import build_switch_note, describe_workplace


def test_describe_tunnel_workplace_has_device_and_path_but_no_ip():
    wp = {'id': 'w1', 'name': 'demo-workplace', 'type': 'tunnel', 'config': '{}'}
    connector = {'device_name': 'demo-connector', 'ip_addresses': '203.0.113.10, 2001:db8::1'}

    text = describe_workplace(wp, '/srv/workspace', connector)

    assert text == '"demo-workplace" (Evonet tunnel · device demo-connector · working directory /srv/workspace)'
    assert '203.0.113.10' not in text


def test_describe_remote_workplace_omits_ssh_host():
    wp = {'id': 'w2', 'name': 'build-box', 'type': 'remote',
          'config': '{"host": "10.0.0.5", "username": "ci", "port": 2222}'}

    text = describe_workplace(wp, '/srv/ci')

    assert text == '"build-box" (SSH · working directory /srv/ci)'
    assert '10.0.0.5' not in text and 'ci@' not in text


def test_describe_no_workplace_is_local_workspace():
    assert describe_workplace(None, '/Users/me/ws') == 'the local workspace (working directory /Users/me/ws)'


def test_switch_note_marks_earlier_results_as_stale():
    note = build_switch_note('"demo-workplace" (Evonet tunnel)', 'the local workspace (working directory /ws)',
                             'the local workspace')

    assert note.startswith('[SYSTEM] Workplace switched from "demo-workplace" (Evonet tunnel) to the local workspace')
    assert 'run on the local workspace' in note
    assert 'came from the previous workplace' in note


def _runtime(busy, live=True):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt._session_store = _SessionStore()
    rt._session_store._busy['s1'] = busy
    rt._pending_session_notes = {}
    rt._has_live_turn = lambda agent_id, session_id: live
    rt.written = []
    rt._write_session_note = lambda session_id, *note: rt.written.append((session_id, note[1]))
    return rt


def test_note_is_written_immediately_when_session_is_idle():
    rt = _runtime(busy=False)

    rt.add_session_note('a1', 's1', 'note', {})

    assert rt.written == [('s1', 'note')]


def test_note_is_held_during_a_running_turn_and_flushed_after():
    rt = _runtime(busy=True)

    rt.add_session_note('a1', 's1', 'note', {})
    assert rt.written == []

    rt._set_busy('s1', False)  # what the turn's finally does before flushing
    rt._flush_session_notes('s1')
    assert rt.written == [('s1', 'note')]
    assert rt._pending_session_notes == {}


def test_stale_busy_flag_does_not_strand_the_note():
    rt = _runtime(busy=True, live=False)

    rt.add_session_note('a1', 's1', 'note', {})

    assert rt.written == [('s1', 'note')]
