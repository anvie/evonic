"""
Unit tests for backend/tools/lib/backends/bwrap_backend.py

All tests are pure-host (no bwrap execution), so they run on macOS too:
hostname sanitization, path mapping, bwrap argv construction, availability
guard, host-side file I/O round-trips, and registry selection via the
SANDBOX_BACKEND config.
"""

import os
import sys
import time

import pytest

from backend.tools.lib.backends import bwrap_backend
from backend.tools.lib.backends.bwrap_backend import BwrapBackend, _sanitize_hostname
from backend.tools._workspace import scratch_dir


@pytest.fixture(autouse=True)
def clean_keeper_pool():
    """Keeper state is module-level — isolate it per test."""
    bwrap_backend._keepers.clear()
    yield
    bwrap_backend._keepers.clear()


class FakeProc:
    def __init__(self, pid=1000, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def make_keeper_info(inner_pid=4242, workspace='/ws', last_used=None):
    return {'proc': FakeProc(), 'inner_pid': inner_pid, 'status_fd': -1,
            'created_at': time.time(), 'last_used': last_used or time.time(),
            'workspace': workspace, 'hostname': 'agent'}


# ---------------------------------------------------------------------------
# _sanitize_hostname
# ---------------------------------------------------------------------------

def test_sanitize_hostname_basic():
    assert _sanitize_hostname('My Agent') == 'my-agent'
    assert _sanitize_hostname('agent_01!') == 'agent-01'


def test_sanitize_hostname_unicode_and_empty():
    assert _sanitize_hostname('ägent ünïcode') == 'gent-n-code'
    assert _sanitize_hostname('') == 'agent'
    assert _sanitize_hostname('___') == 'agent'


def test_sanitize_hostname_truncates_to_63():
    assert len(_sanitize_hostname('a' * 100)) == 63


# ---------------------------------------------------------------------------
# resolve_path / _to_host
# ---------------------------------------------------------------------------

def test_resolve_path_maps_workspace(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws)
    assert b.resolve_path(os.path.join(ws, 'a', 'b.txt')) == '/workspace/a/b.txt'
    assert b.resolve_path('/etc/hosts') == '/etc/hosts'


def test_to_host_mappings(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws)
    assert b._to_host('/workspace/x/y.txt') == os.path.join(ws, 'x/y.txt')
    assert b._to_host('/workspace') == ws
    assert b._to_host('/home/agent/.npmrc') == os.path.join(ws, '.home', '.npmrc')
    assert b._to_host('/home/agent') == os.path.join(ws, '.home')
    assert b._to_host('/etc/hosts') == '/etc/hosts'


def test_file_io_round_trip(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws)
    assert b.write_file('/workspace/a.txt', 'hello') == {'ok': True}
    assert (tmp_path / 'a.txt').read_text() == 'hello'
    assert b.read_file('/workspace/a.txt') == {'content': 'hello'}
    assert b.file_exists('/workspace/a.txt') is True
    stat = b.file_stat('/workspace/a.txt')
    assert stat['exists'] is True and stat['size'] == 5

    # Home-view paths land in <ws>/.home on the host
    assert b.write_file('/home/agent/.persistent', 'hi') == {'ok': True}
    assert (tmp_path / '.home' / '.persistent').read_text() == 'hi'

    assert b.delete_file('/workspace/a.txt') == {'ok': True}
    assert not (tmp_path / 'a.txt').exists()


# ---------------------------------------------------------------------------
# _bwrap_argv
# ---------------------------------------------------------------------------

def test_bwrap_argv_core_flags(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws, agent_name='Test Agent')
    argv = b._bwrap_argv()
    for flag in ('--unshare-pid', '--unshare-uts', '--unshare-ipc',
                 '--unshare-user', '--die-with-parent'):
        assert flag in argv
    assert argv[argv.index('--hostname') + 1] == 'test-agent'
    ws_bind = argv.index('--bind')
    assert argv[ws_bind + 1:ws_bind + 3] == [ws, '/workspace']
    assert os.path.join(ws, '.home') in argv and '/home/agent' in argv
    assert bwrap_backend._HELPERS_DIR in argv
    # workdir is set per-exec by the nsenter trampoline, not on the keeper
    assert '--chdir' not in argv


def test_workdir_subagent(tmp_path):
    assert BwrapBackend(session_id='s1', workspace=str(tmp_path))._workdir() == '/workspace'
    assert BwrapBackend(session_id='s1', workspace=str(tmp_path), agent_id='a_sub_1',
                        is_subagent=True)._workdir() == scratch_dir('a_sub_1')


def test_bwrap_argv_binds_resolv_conf_target(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    real_realpath = os.path.realpath

    def fake_realpath(path, **kw):
        if path == '/etc/resolv.conf':
            return '/run/systemd/resolve/stub-resolv.conf'
        return real_realpath(path, **kw)

    monkeypatch.setattr(bwrap_backend.os.path, 'realpath', fake_realpath)
    argv = b._bwrap_argv()
    i = argv.index('/run/systemd/resolve/stub-resolv.conf')
    assert argv[i - 1] == '--ro-bind-try' and argv[i + 1] == argv[i]

    # Plain-file resolv.conf (no symlink) needs no extra bind
    monkeypatch.setattr(bwrap_backend.os.path, 'realpath',
                        lambda p, **kw: p if p == '/etc/resolv.conf' else real_realpath(p, **kw))
    assert '/run/systemd/resolve/stub-resolv.conf' not in b._bwrap_argv()


def test_bwrap_argv_unshare_net_follows_config(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    monkeypatch.setattr(bwrap_backend, 'SANDBOX_NETWORK', 'bridge')
    assert '--unshare-net' not in b._bwrap_argv()
    monkeypatch.setattr(bwrap_backend, 'SANDBOX_NETWORK', 'none')
    assert '--unshare-net' in b._bwrap_argv()


# ---------------------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------------------

def test_availability_error_non_linux():
    if sys.platform == 'linux':
        return  # message depends on bwrap presence; covered by the guard test below
    err = bwrap_backend._availability_error()
    assert err is not None and 'Linux-only' in err


def test_run_bash_returns_error_when_unavailable(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    monkeypatch.setattr(bwrap_backend, '_availability_error', lambda: 'bwrap missing')
    result = b.run_bash('echo hi', timeout=5, env={})
    assert result == {'error': 'bwrap missing', 'exit_code': -1, 'execution_time': 0}
    result = b.run_python('print(1)', timeout=5, env={})
    assert result['error'] == 'bwrap missing'


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def test_base_env_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv('EVONIC_SECRET_TOKEN', 'leak-me-not')
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path), agent_id='botagent', agent_name='bot')
    env = b._base_env({'FOO': 'bar'})
    assert 'EVONIC_SECRET_TOKEN' not in env
    assert env['HOME'] == '/home/agent'
    assert env['HOSTNAME'] == 'bot'
    assert env['SCRATCH'] == scratch_dir('botagent')
    assert env['FOO'] == 'bar'


# ---------------------------------------------------------------------------
# Registry selection via SANDBOX_BACKEND
# ---------------------------------------------------------------------------

def test_registry_selects_bwrap(tmp_path, monkeypatch):
    import config
    from backend.tools.lib.exec_backend import registry
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'bwrap')
    backend = registry.get_backend('sess-bwrap', {
        'sandbox_enabled': 1, 'workspace': str(tmp_path),
        'agent_id': 'a1', 'name': 'My Agent',
    })
    assert isinstance(backend, BwrapBackend)
    assert backend._hostname == 'my-agent'


def test_registry_defaults_to_docker(tmp_path, monkeypatch):
    import config
    from backend.tools.lib.exec_backend import registry
    from backend.tools.lib.backends.docker_backend import DockerBackend
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'docker')
    backend = registry.get_backend('sess-docker', {
        'sandbox_enabled': 1, 'workspace': str(tmp_path), 'agent_id': 'a1',
    })
    assert isinstance(backend, DockerBackend)

# ---------------------------------------------------------------------------
# Keeper: nsenter argv, child-pid parsing, pool logic
# ---------------------------------------------------------------------------

def test_nsenter_argv_contents():
    argv = bwrap_backend._nsenter_argv(4242, '/workspace/.scratch', ['bash', '-s'])
    assert argv[0] == 'nsenter'
    for flag in ('--preserve-credentials', '-U', '-m', '-u', '-i', '-p'):
        assert flag in argv
    assert argv[argv.index('-t') + 1] == '4242'
    trampoline = argv[argv.index('-c') + 1]
    assert 'cd /workspace/.scratch' in trampoline and 'exec "$@"' in trampoline
    assert 'ulimit' not in trampoline
    assert argv[-2:] == ['bash', '-s']

    with_limit = bwrap_backend._nsenter_argv(1, '/workspace', ['python3', '-'], ulimit_v_kb=1024)
    assert 'ulimit -v 1024' in with_limit[with_limit.index('-c') + 1]
    assert with_limit[-2:] == ['python3', '-']


def test_read_child_pid_parses_json():
    r, w = os.pipe()
    try:
        os.write(w, b'{"child-pid": 4242}\n')
        assert bwrap_backend._read_child_pid(r, None, timeout=2) == 4242
    finally:
        os.close(r); os.close(w)

    # Garbage lines tolerated, pid line split across writes
    r, w = os.pipe()
    try:
        os.write(w, b'not-json\n{"other": 1}\n{"child-pi')
        os.write(w, b'd": 77}\n')
        assert bwrap_backend._read_child_pid(r, None, timeout=2) == 77
    finally:
        os.close(r); os.close(w)

    # EOF without a child-pid line -> None
    r, w = os.pipe()
    os.write(w, b'{"other": 1}\n')
    os.close(w)
    try:
        assert bwrap_backend._read_child_pid(r, None, timeout=2) is None
    finally:
        os.close(r)

    # bwrap died before reporting -> None (no data ever arrives)
    r, w = os.pipe()
    try:
        assert bwrap_backend._read_child_pid(r, FakeProc(returncode=1), timeout=2) is None
    finally:
        os.close(r); os.close(w)


def test_keeper_pool_reuse_and_workspace_change(tmp_path, monkeypatch):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='sess-1', workspace=ws)
    monkeypatch.setattr(bwrap_backend, '_pid_alive', lambda pid: True)
    spawned = []

    def fake_spawn(self):
        info = make_keeper_info(inner_pid=1000 + len(spawned), workspace=self._cwd())
        spawned.append(info)
        return info, None

    monkeypatch.setattr(BwrapBackend, '_spawn_keeper', fake_spawn)

    pid1, err = b._get_or_create_keeper()
    assert err is None and pid1 == 1000 and len(spawned) == 1
    before = bwrap_backend._keepers['sess-1']['last_used']
    time.sleep(0.01)
    pid2, _ = b._get_or_create_keeper()
    assert pid2 == pid1 and len(spawned) == 1
    assert bwrap_backend._keepers['sess-1']['last_used'] > before

    # Same session, different workspace -> destroy + recreate
    destroys = []
    real_destroy = bwrap_backend._destroy_keeper
    monkeypatch.setattr(bwrap_backend, '_destroy_keeper',
                        lambda sid: (destroys.append(sid), real_destroy(sid))[1])
    ws2 = str(tmp_path / 'other')
    os.makedirs(ws2, exist_ok=True)
    b2 = BwrapBackend(session_id='sess-1', workspace=ws2)
    pid3, err = b2._get_or_create_keeper()
    assert err is None and pid3 == 1001 and destroys == ['sess-1']


def test_keeper_gone_retry(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='sess-1', workspace=str(tmp_path))
    monkeypatch.setattr(bwrap_backend, '_availability_error', lambda: None)
    monkeypatch.setattr(BwrapBackend, '_get_or_create_keeper', lambda self: (4242, None))
    destroys = []
    monkeypatch.setattr(bwrap_backend, '_destroy_keeper', lambda sid: destroys.append(sid))
    results = [
        {'stdout': '', 'stderr': 'nsenter: cannot open /proc/4242/ns/user: No such process',
         'exit_code': 1, 'execution_time': 0.01},
        {'stdout': 'ok', 'stderr': '', 'exit_code': 0, 'execution_time': 0.01},
    ]
    calls = []
    monkeypatch.setattr(BwrapBackend, '_exec',
                        lambda self, cmd, data, timeout, env: calls.append(cmd) or results[len(calls) - 1])
    result = b.run_bash('echo ok', timeout=5, env={})
    assert result['stdout'] == 'ok' and len(calls) == 2 and destroys == ['sess-1']


def test_is_keeper_gone():
    gone = {'stderr': 'nsenter: cannot open /proc/1/ns/user: No such process', 'exit_code': 1}
    assert bwrap_backend._is_keeper_gone(gone) is True
    ordinary_fail = {'stdout': '', 'stderr': 'ls: cannot access /nope: No such file', 'exit_code': 2}
    assert bwrap_backend._is_keeper_gone(ordinary_fail) is False
    ok = {'stdout': 'fine', 'stderr': '', 'exit_code': 0}
    assert bwrap_backend._is_keeper_gone(ok) is False


def test_stale_sessions_exempts_workplaces():
    old = time.time() - bwrap_backend.SANDBOX_IDLE_TIMEOUT - 100
    bwrap_backend._keepers['workplace-bwrap-abc'] = make_keeper_info(last_used=old)
    bwrap_backend._keepers['sess-y'] = make_keeper_info(last_used=old)
    bwrap_backend._keepers['sess-fresh'] = make_keeper_info()
    assert bwrap_backend._stale_sessions(time.time()) == ['sess-y']


def test_destroy_pops_pool_and_kills(tmp_path, monkeypatch):
    kills = []
    monkeypatch.setattr(bwrap_backend.os, 'killpg', lambda pgid, sig: kills.append((pgid, sig)))
    monkeypatch.setattr(bwrap_backend.os, 'close', lambda fd: None)
    bwrap_backend._keepers['sess-1'] = make_keeper_info()
    b = BwrapBackend(session_id='sess-1', workspace=str(tmp_path))
    result = b.destroy()
    assert result['result'] == 'sandbox_destroyed'
    assert 'sess-1' not in bwrap_backend._keepers
    assert kills and kills[0][1] == bwrap_backend.signal.SIGTERM

    # Destroy with no keeper is a graceful no-op
    assert b.destroy()['result'] == 'no_keeper'


def test_status_reports_keeper(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='sess-1', workspace=str(tmp_path))
    assert b.status()['keeper'] == 'not_started'
    monkeypatch.setattr(bwrap_backend, '_pid_alive', lambda pid: True)
    bwrap_backend._keepers['sess-1'] = make_keeper_info(inner_pid=555)
    st = b.status()
    assert st['keeper'] == 'running' and st['inner_pid'] == 555 and st['uptime_s'] >= 0
