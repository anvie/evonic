"""
Unit tests for backend/tools/lib/backends/bwrap_backend.py

All tests are pure-host (no bwrap execution), so they run on macOS too:
hostname sanitization, path mapping, bwrap argv construction, availability
guard, host-side file I/O round-trips, and registry selection via the
SANDBOX_BACKEND config.
"""

import io
import os
import subprocess
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
    def __init__(self, pid=1000, returncode=None, stderr=''):
        self.pid = pid
        self.returncode = returncode
        self.stderr = io.StringIO(stderr)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def make_keeper_info(inner_pid=4242, workspace='/ws', last_used=None):
    return {'proc': FakeProc(), 'inner_pid': inner_pid, 'status_fd': -1,
            'created_at': time.time(), 'last_used': last_used or time.time(),
            'workspace': workspace, 'hostname': 'agent',
            'layout_version': bwrap_backend._KEEPER_LAYOUT_VERSION}


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


def test_scratch_file_io_routes_through_sandbox(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path), agent_id='agent_s')
    path = f"{scratch_dir('agent_s')}/attachments/session_a/report.txt"
    calls = []

    def fake_run_python(code, timeout, env):
        calls.append(code)
        if "op = 'write_bytes'" in code:
            return {'stdout': '{"ok": true}', 'exit_code': 0}
        if "op = 'stat'" in code:
            return {'stdout': '{"exists": true, "size": 4, "is_binary": false}', 'exit_code': 0}
        if "op = 'read_bytes'" in code:
            return {'stdout': '{"data": "ZGF0YQ=="}', 'exit_code': 0}
        return {'stdout': 'true', 'exit_code': 0}

    monkeypatch.setattr(b, 'run_python', fake_run_python)

    assert b.write_file_bytes(path, b'data') == {'ok': True}
    assert b.file_stat(path) == {'exists': True, 'size': 4, 'is_binary': False}
    assert b.cat_file_bytes(path) == {'bytes': b'data'}
    assert not (tmp_path / 'attachments').exists()
    assert all(scratch_dir('agent_s') in call for call in calls)


def test_non_agent_scratch_path_uses_existing_host_file_io(tmp_path):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path), agent_id='agent_s')
    path = tmp_path / 'other-scratch' / 'file.txt'
    assert b.write_file(str(path), 'host') == {'ok': True}
    assert path.read_text() == 'host'


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
    helper_source = argv[argv.index(bwrap_backend._HELPERS_MOUNT) - 1]
    assert helper_source.startswith(bwrap_backend._HELPERS_RUNTIME_ROOT + os.sep)
    assert bwrap_backend._HELPERS_DIR not in argv
    # workdir is set per-exec by the nsenter trampoline, not on the keeper
    assert '--chdir' not in argv


def test_bwrap_argv_uses_open_fds_for_private_bind_sources(tmp_path):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))

    argv = b._bwrap_argv(71, 72)

    assert argv[argv.index('--bind-fd') + 1:argv.index('--bind-fd') + 3] == ['71', '/workspace']
    second = argv.index('--bind-fd', argv.index('--bind-fd') + 1)
    assert argv[second + 1:second + 3] == ['72', '/home/agent']
    assert str(tmp_path) not in argv and str(tmp_path / '.home') not in argv


def test_spawn_keeper_inherits_and_closes_bind_fds(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(cmd=cmd, pass_fds=kwargs['pass_fds'])
        return FakeProc()

    monkeypatch.setattr(bwrap_backend.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(bwrap_backend, '_read_child_pid', lambda fd, proc: 4242)
    monkeypatch.setattr(bwrap_backend, '_wait_for_nsenter_ready', lambda pid: (_probe(0), None))

    info, error = b._spawn_keeper()

    assert error is None and info['inner_pid'] == 4242
    assert len(captured['pass_fds']) == 3
    bind_fds = captured['pass_fds'][1:]
    assert all(['--bind-fd', str(fd)] in [captured['cmd'][i:i + 2]
               for i in range(len(captured['cmd']) - 1)] for fd in bind_fds)
    for fd in bind_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    os.close(info['status_fd'])


def test_spawn_keeper_runs_bwrap_as_workspace_owner_when_service_is_root(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(bwrap_backend.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(bwrap_backend.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(bwrap_backend, '_read_child_pid', lambda fd, proc: 4242)
    monkeypatch.setattr(bwrap_backend, '_wait_for_nsenter_ready', lambda pid: (_probe(0), None))

    info, error = b._spawn_keeper()

    workspace_stat = os.stat(tmp_path)
    assert error is None
    assert captured['user'] == workspace_stat.st_uid
    assert captured['group'] == workspace_stat.st_gid
    assert captured['extra_groups'] == ()
    os.close(info['status_fd'])


def test_spawn_keeper_does_not_switch_user_for_non_root_service(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(bwrap_backend.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(bwrap_backend.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(bwrap_backend, '_read_child_pid', lambda fd, proc: 4242)
    monkeypatch.setattr(bwrap_backend, '_wait_for_nsenter_ready', lambda pid: (_probe(0), None))

    info, error = b._spawn_keeper()

    assert error is None
    assert not {'user', 'group', 'extra_groups'} & captured.keys()
    os.close(info['status_fd'])


def test_spawn_keeper_closes_bind_fds_when_popen_fails(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    inherited = []

    def fail_popen(cmd, **kwargs):
        inherited.extend(kwargs['pass_fds'][1:])
        raise OSError('spawn failed')

    monkeypatch.setattr(bwrap_backend.subprocess, 'Popen', fail_popen)

    info, error = b._spawn_keeper()

    assert info is None and 'spawn failed' in error
    for fd in inherited:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_stage_helpers_works_below_private_parent(tmp_path):
    private = tmp_path / 'private'
    source = private / 'helpers'
    runtime = tmp_path / 'runtime' / 'helpers'
    (source / 'bin').mkdir(parents=True)
    (source / '__init__.py').write_text('value = 1\n')
    executable = source / 'bin' / 'rg'
    executable.write_text('#!/bin/sh\n')
    executable.chmod(0o755)
    private.chmod(0o700)

    staged = bwrap_backend._stage_helpers(str(source), str(runtime))

    assert staged.startswith(str(runtime) + os.sep)
    assert open(os.path.join(staged, '__init__.py')).read() == 'value = 1\n'
    assert os.stat(runtime.parent).st_mode & 0o777 == 0o755
    assert os.stat(runtime).st_mode & 0o777 == 0o755
    assert os.stat(os.path.join(staged, 'bin', 'rg')).st_mode & 0o111


def test_stage_helpers_is_content_addressed(tmp_path):
    source = tmp_path / 'source'
    runtime = tmp_path / 'runtime' / 'helpers'
    source.mkdir()
    helper = source / 'display.py'
    helper.write_text('first\n')
    first = bwrap_backend._stage_helpers(str(source), str(runtime))
    assert bwrap_backend._stage_helpers(str(source), str(runtime)) == first
    helper.write_text('second\n')
    second = bwrap_backend._stage_helpers(str(source), str(runtime))
    assert second != first and open(os.path.join(second, 'display.py')).read() == 'second\n'


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


def _probe(returncode, stderr=''):
    return subprocess.CompletedProcess([], returncode, '', stderr)


def test_nsenter_readiness_retries_transient_startup_failure(monkeypatch):
    results = [
        _probe(1, 'nsenter: failed to execute /usr/bin/bash: No such file or directory'),
        _probe(0),
    ]
    calls = []
    monkeypatch.setattr(bwrap_backend.subprocess, 'run',
                        lambda *args, **kwargs: calls.append(args) or results.pop(0))
    monkeypatch.setattr(bwrap_backend.time, 'sleep', lambda _: None)

    probe, failure = bwrap_backend._wait_for_nsenter_ready(4242, timeout=1)

    assert probe.returncode == 0 and failure is None and len(calls) == 2


def test_nsenter_readiness_exhaustion_has_initialization_diagnostic(monkeypatch):
    result = _probe(1, 'nsenter: failed to execute /usr/bin/bash: No such file or directory')
    clock = iter((0.0, 0.0, 0.2, 1.1))
    monkeypatch.setattr(bwrap_backend.subprocess, 'run', lambda *args, **kwargs: result)
    monkeypatch.setattr(bwrap_backend.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(bwrap_backend.time, 'sleep', lambda _: None)

    probe, failure = bwrap_backend._wait_for_nsenter_ready(4242, timeout=1)
    message = bwrap_backend._nsenter_failure_message(probe, failure)

    assert failure == 'readiness' and 'did not finish initializing' in message
    assert 'WSL2' not in message and 'incompatible' not in message


def test_nsenter_readiness_does_not_retry_permission_failure(monkeypatch):
    calls = []
    result = _probe(1, 'nsenter: reassociate to namespace ns/user failed: Operation not permitted')
    monkeypatch.setattr(bwrap_backend.subprocess, 'run',
                        lambda *args, **kwargs: calls.append(args) or result)

    probe, failure = bwrap_backend._wait_for_nsenter_ready(4242, timeout=1)
    message = bwrap_backend._nsenter_failure_message(probe, failure)

    assert len(calls) == 1 and failure is None
    assert 'cannot join' in message and 'WSL2' in message


def test_nsenter_other_failure_avoids_compatibility_claim(monkeypatch):
    result = _probe(1, 'nsenter: failed to execute /usr/bin/bash: Input/output error')
    monkeypatch.setattr(bwrap_backend.subprocess, 'run', lambda *args, **kwargs: result)

    probe, failure = bwrap_backend._wait_for_nsenter_ready(4242, timeout=1)
    message = bwrap_backend._nsenter_failure_message(probe, failure)

    assert failure is None and 'failed while checking' in message
    assert 'WSL2' not in message and 'incompatible' not in message


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


def _mock_keeper_spawn(monkeypatch, proc, probe, failure='readiness'):
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(bwrap_backend.os, 'pipe', lambda: (r_fd, w_fd))
    monkeypatch.setattr(bwrap_backend.subprocess, 'Popen', lambda *args, **kwargs: proc)
    monkeypatch.setattr(bwrap_backend, '_read_child_pid', lambda fd, child: 4242)
    monkeypatch.setattr(bwrap_backend, '_wait_for_nsenter_ready',
                        lambda pid: (probe, failure))
    destroyed = []
    monkeypatch.setattr(bwrap_backend, '_destroy_probe',
                        lambda child, fd: destroyed.append((child, fd)))
    return r_fd, destroyed


def test_spawn_keeper_readiness_failure_cleans_up(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='sess-1', workspace=str(tmp_path))
    proc = FakeProc(pid=1000)
    failed_probe = _probe(1, 'nsenter: failed to execute /usr/bin/bash: No such file or directory')
    r_fd, destroyed = _mock_keeper_spawn(monkeypatch, proc, failed_probe)

    info, error = b._spawn_keeper()

    assert info is None and 'failed readiness check' in error
    assert destroyed == [(proc, r_fd)]
    os.close(r_fd)


def test_spawn_keeper_prefers_bwrap_stderr_after_early_exit(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='sess-1', workspace=str(tmp_path))
    proc = FakeProc(pid=1000, returncode=1,
                    stderr='bwrap: setup failed for a primary reason\n')
    failed_probe = _probe(1, 'nsenter: cannot open /proc/4242/ns/user: No such file or directory')
    r_fd, destroyed = _mock_keeper_spawn(monkeypatch, proc, failed_probe, failure=None)

    info, error = b._spawn_keeper()

    assert info is None and 'setup failed for a primary reason' in error
    assert 'nsenter' not in error and destroyed == [(proc, r_fd)]
    os.close(r_fd)


def test_spawn_keeper_helper_permission_error_is_actionable(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='sess-1', workspace=str(tmp_path))
    helper = '/srv/private/evonic/runpy_helpers'
    proc = FakeProc(pid=1000, returncode=1,
                    stderr=f"bwrap: Can't find source path {helper}: Permission denied\n")
    failed_probe = _probe(1, 'nsenter: cannot open /proc/4242/ns/user: No such file or directory')
    r_fd, destroyed = _mock_keeper_spawn(monkeypatch, proc, failed_probe, failure=None)

    info, error = b._spawn_keeper()

    assert info is None and helper in error and 'execute/traverse permission' in error
    assert 'every parent directory' in error and 'mode 0700' in error
    assert 'Do not weaken filesystem permissions automatically' in error
    assert 'WSL2' not in error and 'incompatible' not in error
    assert destroyed == [(proc, r_fd)]
    os.close(r_fd)


def test_exited_process_stderr_does_not_block_for_live_keeper():
    class LiveProc(FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired('bwrap', timeout)

    assert bwrap_backend._exited_process_stderr(LiveProc(stderr='unavailable')) == ''


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
