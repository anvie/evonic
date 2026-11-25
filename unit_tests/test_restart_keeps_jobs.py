"""graceful_shutdown stops detached background jobs, except when the shutdown is a restart."""
import pytest

from backend import restart
from backend.agent_runtime import background_jobs as bgmod
from backend.agent_runtime.background_jobs import background_jobs
from backend.agent_runtime.runtime import AgentRuntime
from backend.tools.lib import exec_backend


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    background_jobs._jobs.clear()
    background_jobs._counter = 0
    background_jobs._loaded = False
    restart.restarting.clear()
    monkeypatch.setattr(AgentRuntime, '_bg_executor', type('E', (), {'shutdown': lambda *a, **k: None})())
    shutdown_mgr = AgentRuntime._shutdown_mgr
    yield
    shutdown_mgr._event.clear()
    background_jobs._jobs.clear()
    background_jobs._loaded = False
    restart.restarting.clear()


def _run_shutdown(monkeypatch):
    stopped = []
    monkeypatch.setattr(background_jobs, 'stop_for_session',
                        lambda sid, runner: stopped.append(sid) or [])
    monkeypatch.setattr(exec_backend.registry, 'get_backend', lambda sid, ctx: object())
    from backend.channels.registry import channel_manager
    monkeypatch.setattr(channel_manager, 'stop_all', lambda: None)
    background_jobs.register('sess-a', 'tmux-a', '', '', 'sleep 100', kind='tmux')
    AgentRuntime.graceful_shutdown()
    return stopped


def test_plain_shutdown_stops_jobs(monkeypatch):
    assert _run_shutdown(monkeypatch) == ['sess-a']


def test_restart_leaves_jobs_running(monkeypatch):
    restart.restarting.set()
    assert _run_shutdown(monkeypatch) == []
    assert [j.job_id for j in background_jobs.running_for_session('sess-a')] == ['job1']
