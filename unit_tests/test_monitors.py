"""Tests for the opt-in monitor subsystem (backend/agent_runtime/monitors.py)."""
import time

import pytest

from backend.agent_runtime import monitors
from backend.agent_runtime.background_jobs import background_jobs


AGENT = {'agent_id': 'mon_test_agent', 'session_id': 'sess-mon', 'user_id': 'u1'}


@pytest.fixture(autouse=True)
def clean_registry():
    background_jobs._jobs.clear()
    yield
    background_jobs._jobs.clear()


def _wrapper_job(session_id='sess-mon'):
    return background_jobs.register(
        session_id=session_id, session_name='evonic_build_1',
        log_file='/tmp/evonic_build_1.log', pid_file='/tmp/evonic_build_1.pid',
        command='make -j4', kind='wrapper')


def _tmux_job(session_id='sess-mon'):
    return background_jobs.register(
        session_id=session_id, session_name='mybuild', log_file='',
        pid_file='', command='tmux new-session -d -s mybuild', kind='tmux')


# ---------------------------------------------------------------------------
# attach() validation — all of these must fail before touching the scheduler
# ---------------------------------------------------------------------------

def test_attach_requires_a_condition():
    assert 'error' in monitors.attach(AGENT, {'job_id': 'job1'}, {})


def test_attach_rejects_unknown_condition():
    r = monitors.attach(AGENT, {'job_id': 'job1'}, {'when_done': True})
    assert 'Unknown condition' in r['error']


def test_attach_rejects_bad_regex():
    r = monitors.attach(AGENT, {'log_file': '/tmp/x.log'}, {'log_match': '(['})
    assert 'Invalid log_match regex' in r['error']


def test_attach_rejects_unknown_job_id():
    r = monitors.attach(AGENT, {'job_id': 'nope'}, {'on_exit': True})
    assert 'Unknown job_id' in r['error']


def test_attach_rejects_job_from_another_session():
    job = _wrapper_job(session_id='other-session')
    r = monitors.attach(AGENT, {'job_id': job.job_id}, {'on_exit': True})
    assert 'another session' in r['error']


def test_attach_rejects_on_exit_without_job():
    r = monitors.attach(AGENT, {'log_file': '/tmp/x.log'}, {'on_exit': True})
    assert 'on_exit/on_failure need' in r['error']


def test_attach_rejects_on_failure_for_tmux_job():
    job = _tmux_job()
    r = monitors.attach(AGENT, {'job_id': job.job_id}, {'on_failure': True})
    assert 'not observable for tmux' in r['error']


def test_attach_rejects_log_match_without_log():
    job = _tmux_job()  # tmux spawns have no log file
    r = monitors.attach(AGENT, {'job_id': job.job_id}, {'log_match': 'ERROR'})
    assert 'log_match needs a log file' in r['error']


def test_attach_requires_agent_context():
    assert 'error' in monitors.attach({}, {}, {'shell': 'true'})


# ---------------------------------------------------------------------------
# build_probe_script — only emit what the conditions need
# ---------------------------------------------------------------------------

def test_probe_emits_only_needed_lines():
    only_shell = monitors.build_probe_script('', '', False, '', 'test -f /tmp/x')
    assert 'H:1' in only_shell
    assert 'S:' not in only_shell and 'M:1' not in only_shell
    assert 'tail -n' not in only_shell          # no log → no tail

    full = monitors.build_probe_script(
        'echo RUNNING', '/tmp/a.log', True, 'ERR', '')
    assert "sed 's/^/S:/'" in full
    assert 'EXIT_CODE' in full
    assert 'grep -Eq' in full
    assert 'echo T:' in full
    assert 'H:1' not in full


def test_probe_quotes_paths_and_patterns():
    script = monitors.build_probe_script(
        '', "/tmp/we ird.log", False, "a|b'c", '')
    assert "'/tmp/we ird.log'" in script
    assert "; rm -rf /" not in script


# ---------------------------------------------------------------------------
# parse_probe_output
# ---------------------------------------------------------------------------

def test_parse_full_block():
    out = monitors.parse_probe_output(
        "__EVMON__\nS:DONE\nX:3\nM:0\nH:1\nT:\nline one\nline two\n")
    assert out['status'] == 'DONE'
    assert out['exit_code'] == 3
    assert out['matched'] is False
    assert out['shell_ok'] is True
    assert out['tail'] == 'line one\nline two'


def test_parse_ignores_preamble_before_marker():
    out = monitors.parse_probe_output("shell noise\n__EVMON__\nS:RUNNING\n")
    assert out['status'] == 'RUNNING'
    assert out['exit_code'] is None


def test_parse_returns_none_without_marker():
    assert monitors.parse_probe_output('bash: command not found') is None
    assert monitors.parse_probe_output('') is None


def test_parse_does_not_read_markers_inside_the_tail():
    out = monitors.parse_probe_output("__EVMON__\nS:RUNNING\nT:\nM:1\nS:DONE\n")
    assert out['status'] == 'RUNNING'
    assert out['matched'] is None
    assert out['tail'] == 'M:1\nS:DONE'


# ---------------------------------------------------------------------------
# run_monitor_poll — decision table
# ---------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, stdout):
        self.stdout = stdout
        self.calls = 0

    def run_bash(self, script, timeout, env):
        self.calls += 1
        return {'stdout': self.stdout, 'exit_code': 0}


@pytest.fixture
def poll(monkeypatch):
    """Run a poll against canned probe output; capture the notification."""
    sent = []

    def _notify(agent_id, tag, message, **kw):
        sent.append({'agent_id': agent_id, 'tag': tag, 'message': message, **kw})
        return {'success': True}

    monkeypatch.setattr('backend.agent_runtime.notifier.notify_agent', _notify)
    monkeypatch.setattr('models.db.db.get_agent', lambda *a, **k: {})

    def _run(stdout, when, **cfg):
        backend = _FakeBackend(stdout)
        monkeypatch.setattr(
            'backend.tools.lib.exec_backend.registry.get_backend',
            lambda *a, **k: backend)
        config = {
            'session_id': 'sess-mon', 'agent_id': 'mon_test_agent',
            'command': 'make -j4', 'log_file': '/tmp/a.log', 'when': when,
            'probe_script': 'echo', 'deadline_ts': time.time() + 3600,
        }
        config.update(cfg)
        result = monitors.run_monitor_poll(config)
        result['backend_calls'] = backend.calls
        return result, sent

    return _run


def test_poll_waits_while_running(poll):
    result, sent = poll("__EVMON__\nS:RUNNING\nT:\nbuilding\n", {'on_exit': True})
    assert result == {'done': False, 'state': 'waiting', 'backend_calls': 1}
    assert sent == []


def test_poll_fires_on_exit(poll):
    result, sent = poll("__EVMON__\nS:DONE\nX:0\nT:\nall good\n", {'on_exit': True})
    assert result['done'] and result['state'] == 'matched'
    assert result['backend_calls'] == 1          # one round-trip, even when firing
    assert 'exit code 0' in sent[0]['message']
    assert 'all good' in sent[0]['message']
    assert sent[0]['tag'] == 'MONITOR'


def test_poll_on_failure_ignores_success(poll):
    result, sent = poll("__EVMON__\nS:DONE\nX:0\n", {'on_failure': True})
    # Process is gone and can never fail now — stop watching, but say so.
    assert result['done'] and result['state'] == 'ended'
    assert 'without meeting the condition' in sent[0]['message']


def test_poll_on_failure_fires_on_nonzero(poll):
    result, sent = poll("__EVMON__\nS:DONE\nX:2\n", {'on_failure': True})
    assert result['state'] == 'matched'
    assert 'exit code 2' in sent[0]['message']


def test_poll_fires_on_log_match(poll):
    result, sent = poll("__EVMON__\nS:RUNNING\nM:1\nT:\nERROR: boom\n",
                        {'log_match': 'ERROR'})
    assert result['state'] == 'matched'
    assert 'log matched /ERROR/' in sent[0]['message']


def test_poll_fires_on_shell_predicate(poll):
    result, sent = poll("__EVMON__\nH:1\n", {'shell': 'curl -sf localhost'})
    assert result['state'] == 'matched'
    assert 'shell predicate succeeded' in sent[0]['message']


def test_poll_expires(poll):
    result, sent = poll("__EVMON__\nS:RUNNING\n", {'on_exit': True},
                        deadline_ts=time.time() - 1)
    assert result['done'] and result['state'] == 'expired'
    assert 'expired without firing' in sent[0]['message']
    # Expiry short-circuits before any sandbox round-trip.
    assert result['backend_calls'] == 0


def test_poll_retries_on_garbled_probe(poll):
    result, sent = poll('bash: tmux: not found', {'on_exit': True})
    assert result['done'] is False and result['state'] == 'probe_unparsed'
    assert sent == []


def test_poll_echoes_the_note(poll):
    _, sent = poll("__EVMON__\nS:DONE\nX:1\n", {'on_failure': True},
                   note='tell me if the build breaks')
    assert 'tell me if the build breaks' in sent[0]['message']


def test_poll_marks_the_job_finished(poll):
    job = _wrapper_job()
    poll("__EVMON__\nS:DONE\nX:1\n", {'on_exit': True}, job_id=job.job_id)
    assert background_jobs.get(job.job_id).status == 'done'
    assert background_jobs.get(job.job_id).exit_code == 1


# ---------------------------------------------------------------------------
# attach → list → detach round-trip against the real scheduler
# ---------------------------------------------------------------------------

def test_monitor_lifecycle(monkeypatch):
    from backend.scheduler import scheduler

    monkeypatch.setattr(scheduler, '_register_job', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_remove_job', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_update_next_run', lambda *a, **k: None)

    job = _wrapper_job()
    res = monitors.attach(AGENT, {'job_id': job.job_id},
                          {'on_failure': True, 'log_match': 'FAILED'},
                          note='watch the build', interval=5, expires_in=99999)
    assert res['monitor_id'].startswith('mon-')
    assert res['condition'] == 'process exits non-zero OR log matches /FAILED/'
    assert res['interval_seconds'] == 10          # clamped up from 5

    listed = monitors.list_for_session('mon_test_agent', 'sess-mon')
    assert [m['monitor_id'] for m in listed] == [res['monitor_id']]
    assert listed[0]['job_id'] == job.job_id
    assert listed[0]['note'] == 'watch the build'
    assert monitors.monitored_job_ids('mon_test_agent', 'sess-mon') == {job.job_id}

    # Another session's listing must not see it.
    assert monitors.list_for_session('mon_test_agent', 'other') == []

    assert monitors.detach('mon_test_agent', res['monitor_id'])['status'] == 'detached'
    assert monitors.list_for_session('mon_test_agent', 'sess-mon') == []
    assert 'error' in monitors.detach('mon_test_agent', res['monitor_id'])


def test_attach_caps_monitors_per_session(monkeypatch):
    from backend.scheduler import scheduler

    monkeypatch.setattr(scheduler, '_register_job', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_remove_job', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_update_next_run', lambda *a, **k: None)

    for _ in range(monitors._MAX_PER_SESSION):
        assert 'error' not in monitors.attach(AGENT, {}, {'shell': 'true'})
    assert 'the limit' in monitors.attach(AGENT, {}, {'shell': 'true'})['error']

    # ...and the cap is per session, not per agent.
    other = {**AGENT, 'session_id': 'sess-other'}
    assert 'error' not in monitors.attach(other, {}, {'shell': 'true'})


def test_detach_enforces_ownership(monkeypatch):
    from backend.scheduler import scheduler

    monkeypatch.setattr(scheduler, '_register_job', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_remove_job', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, '_update_next_run', lambda *a, **k: None)

    res = monitors.attach(AGENT, {}, {'shell': 'true'})
    assert 'error' not in res
    assert 'error' in monitors.detach('someone_else', res['monitor_id'])
    assert monitors.detach('mon_test_agent', res['monitor_id'])['status'] == 'detached'
