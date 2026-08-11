"""Tests for background job parsing and the per-turn context block."""
import pytest

from backend.agent_runtime import background_jobs as bgmod
from backend.agent_runtime.background_jobs import (
    background_jobs, build_context_block, parse_manual_spawn)


@pytest.fixture(autouse=True)
def clean_registry():
    background_jobs._jobs.clear()
    yield
    background_jobs._jobs.clear()


@pytest.fixture(autouse=True)
def no_monitors(monkeypatch):
    """Keep the scheduler out of these tests — monitors are covered elsewhere."""
    from backend.agent_runtime import monitors
    monkeypatch.setattr(monitors, 'monitored_job_ids', lambda a, s: set())


_counter = iter(range(1000))


def _job(command='sleep 100', session_id='sess-bg'):
    # Distinct session_name per job — register() dedups on it (same tmux
    # session name means the same spawn re-run).
    return background_jobs.register(
        session_id=session_id, session_name=f'tmux-{next(_counter)}',
        log_file='', pid_file='', command=command, kind='tmux')


# ---------------------------------------------------------------------------
# parse_manual_spawn — shell metacharacters inside quotes are part of the command
# ---------------------------------------------------------------------------

def test_tmux_command_keeps_quoted_metacharacters():
    script = ('tmux new-session -d -s entry "export PATH=/opt/bin:$PATH; '
              'cd /srv/app && npm start 2>&1 | tee /tmp/app.log"')
    spawn = parse_manual_spawn(script)
    assert spawn['session_name'] == 'entry'
    assert spawn['command'] == script  # nothing lost


def test_tmux_command_still_stops_at_unquoted_separator():
    spawn = parse_manual_spawn('tmux new-session -d -s foo "echo hi"; echo after')
    assert spawn['command'] == 'tmux new-session -d -s foo "echo hi"'


def test_tmux_command_stops_at_newline_and_ampersand():
    assert parse_manual_spawn(
        'tmux new-session -d -s foo "a"\necho after')['command'].endswith('"a"')
    assert parse_manual_spawn(
        'tmux new-session -d -s foo "a" && echo x')['command'].endswith('"a"')


def test_screen_command_keeps_quoted_semicolon():
    spawn = parse_manual_spawn("screen -dmS job bash -c 'x; y'")
    assert spawn['command'] == "screen -dmS job bash -c 'x; y'"


def test_non_detached_tmux_is_not_a_background_spawn():
    assert parse_manual_spawn('tmux new-session -s foo "echo hi"') is None


# ---------------------------------------------------------------------------
# build_context_block
# ---------------------------------------------------------------------------

def test_no_running_jobs_costs_nothing():
    assert build_context_block('sess-bg', 'agent-1') == ''


def test_finished_jobs_are_not_listed():
    j = _job()
    background_jobs.mark_finished(j.job_id, 'done', 0)
    assert build_context_block('sess-bg', 'agent-1') == ''


def test_block_lists_running_jobs_with_id_and_flag():
    j = _job(command='tmux new-session -d -s build "npm run build"')
    block = build_context_block('sess-bg', 'agent-1')
    assert '## Background Processes' in block
    assert '1 process you started is still running' in block
    assert f'`{j.job_id}`' in block
    assert 'unmonitored' in block
    assert 'npm run build' in block


def test_other_sessions_are_excluded():
    _job(session_id='other')
    assert build_context_block('sess-bg', 'agent-1') == ''


def test_monitored_jobs_are_flagged(monkeypatch):
    from backend.agent_runtime import monitors
    j = _job()
    monkeypatch.setattr(monitors, 'monitored_job_ids', lambda a, s: {j.job_id})
    block = build_context_block('sess-bg', 'agent-1')
    assert 'monitored' in block
    assert 'unmonitored' not in block


def test_long_commands_are_truncated_to_one_line():
    _job(command='tmux new-session -d -s x "' + 'y' * 300 + '"')
    block = build_context_block('sess-bg', 'agent-1')
    line = next(ln for ln in block.splitlines() if ln.startswith('- `'))
    assert len(line) < 120
    assert line.endswith('…')


def test_job_list_is_capped():
    for i in range(12):
        _job(command=f'cmd-{i}')
    block = build_context_block('sess-bg', 'agent-1')
    assert block.count('\n- `') == bgmod._MAX_IN_CONTEXT
    assert '…and 4 more' in block
    assert '12 processes you started are still running' in block


def test_monitor_lookup_failure_does_not_break_the_block(monkeypatch):
    from backend.agent_runtime import monitors
    _job()

    def boom(a, s):
        raise RuntimeError('scheduler down')

    monkeypatch.setattr(monitors, 'monitored_job_ids', boom)
    assert '## Background Processes' in build_context_block('sess-bg', 'agent-1')
