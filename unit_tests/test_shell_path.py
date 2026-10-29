"""Tests for backend/tools/lib/shell_path.py (agent PATH auto-resolution)."""

import pytest

from backend.tools.lib import shell_path


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class SSHBackend:
    """Named to match shell_path's remote-backend detection; never connects."""

    def __init__(self, stdout='', exc=None):
        self.stdout = stdout
        self.exc = exc
        self.calls = []

    def run_bash(self, script, timeout, env, on_output=None):
        self.calls.append({'script': script, 'timeout': timeout, 'env': env})
        if self.exc is not None:
            raise self.exc
        return {'stdout': self.stdout, 'stderr': '', 'exit_code': 0}


class LocalBackend:
    def __init__(self, stdout=''):
        self.stdout = stdout
        self.calls = []

    def run_bash(self, script, timeout, env, on_output=None):
        self.calls.append((script, timeout, env))
        return {'stdout': self.stdout, 'stderr': '', 'exit_code': 0}


class FakeDB:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.writes = []

    def get_agent_variables_dict(self, agent_id):
        return dict(self.stored)

    def set_agent_variable(self, agent_id, key, value, is_secret=False):
        self.writes.append((agent_id, key, value))
        self.stored[key] = value


def _probe_output(login='', interactive=''):
    return (f'\n<<P>>L{login}<<E>>\n'
            f'\n<<P>>I{interactive}<<E>>\n')


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    shell_path._stale_workplaces.clear()
    shell_path._memo.clear()
    shell_path._last_attempt.clear()
    yield
    shell_path._stale_workplaces.clear()
    shell_path._memo.clear()
    shell_path._last_attempt.clear()


@pytest.fixture
def fake_db(monkeypatch):
    # Patch methods on the real db object (replacing the object itself breaks
    # the session-scoped teardown in conftest).
    from models.db import db as real_db
    db = FakeDB()
    monkeypatch.setattr(real_db, 'get_agent_variables_dict', db.get_agent_variables_dict)
    monkeypatch.setattr(real_db, 'set_agent_variable', db.set_agent_variable)
    return db


AGENT = {'id': 'a1', 'workplace_id': 'w1'}


# ---------------------------------------------------------------------------
# Parsing / merging
# ---------------------------------------------------------------------------

def test_parse_ignores_rc_noise():
    noisy = ('hello from .bashrc\n'
             '<<P>>L/usr/bin:/bin<<E>>\n'
             'fetch: mars\n'
             '<<P>>I/home/u/bin:/usr/bin:/bin<<E>>\n')
    login, interactive = shell_path.parse_probe_output(noisy)
    assert login == '/usr/bin:/bin'
    assert interactive == '/home/u/bin:/usr/bin:/bin'


def test_parse_last_match_wins():
    out = '<<P>>L/a<<E>>\n<<P>>L/b<<E>>\n'
    assert shell_path.parse_probe_output(out)[0] == '/b'


def test_merge_login_first_then_interactive_additions():
    merged = shell_path.merge_paths('/usr/bin:/bin', '/home/u/bin:/usr/bin:/sbin')
    assert merged == '/usr/bin:/bin:/home/u/bin:/sbin'


def test_is_usable_path_requires_standard_dirs():
    assert shell_path._is_usable_path('/usr/bin:/bin')
    assert not shell_path._is_usable_path('garbage')
    assert not shell_path._is_usable_path('')
    assert not shell_path._is_usable_path('/home/u/bin')


# ---------------------------------------------------------------------------
# ensure_path_env
# ---------------------------------------------------------------------------

def test_non_remote_backend_untouched(fake_db):
    backend = LocalBackend(_probe_output('/usr/bin:/bin'))
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert env == {}
    assert backend.calls == []


def test_declared_path_wins(fake_db):
    backend = SSHBackend(_probe_output('/usr/bin:/bin'))
    env = shell_path.ensure_path_env(AGENT, backend, {'PATH': '/custom:/usr/bin'})
    assert env['PATH'] == '/custom:/usr/bin'
    assert backend.calls == []


def test_simulation_skipped(fake_db):
    backend = SSHBackend(_probe_output('/usr/bin:/bin'))
    agent = {**AGENT, 'simulation_id': 'sim1'}
    env = shell_path.ensure_path_env(agent, backend, {})
    assert 'PATH' not in env
    assert backend.calls == []


def test_first_run_probes_and_persists(fake_db):
    backend = SSHBackend(_probe_output('/usr/bin:/bin', '/home/u/bin:/usr/bin:/bin'))
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert env['PATH'] == '/usr/bin:/bin:/home/u/bin'
    assert len(backend.calls) == 1
    assert backend.calls[0]['env'] == {}
    assert fake_db.writes == [('a1', 'PATH', '/usr/bin:/bin:/home/u/bin')]


def test_noisy_output_still_resolves(fake_db):
    noisy = ('motd line\n' + _probe_output('/usr/bin:/bin', '/home/u/bin:/usr/bin'))
    backend = SSHBackend(noisy)
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert env['PATH'] == '/usr/bin:/bin:/home/u/bin'


def test_garbage_output_rejected(fake_db):
    backend = SSHBackend('<<P>>Lgarbage<<E>>\n')
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert 'PATH' not in env
    assert fake_db.writes == []


def test_probe_exception_is_swallowed(fake_db):
    backend = SSHBackend(exc=RuntimeError('ssh down'))
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert 'PATH' not in env
    assert fake_db.writes == []


def test_stored_path_injected_without_probe(fake_db):
    fake_db.stored['PATH'] = '/usr/bin:/bin:/home/u/bin'
    backend = SSHBackend(_probe_output('/other'))
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert env['PATH'] == '/usr/bin:/bin:/home/u/bin'
    assert backend.calls == []


def test_memo_prevents_second_probe(fake_db):
    backend = SSHBackend(_probe_output('/usr/bin:/bin'))
    shell_path.ensure_path_env(AGENT, backend, {})
    shell_path.ensure_path_env(AGENT, backend, {})
    assert len(backend.calls) == 1


def test_stale_workplace_reprobes_and_overwrites(fake_db):
    fake_db.stored['PATH'] = '/usr/bin:/bin'
    shell_path.mark_workplace_stale('w1')
    backend = SSHBackend(_probe_output('/usr/bin:/bin', '/home/u/bin:/usr/bin:/bin'))
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert env['PATH'] == '/usr/bin:/bin:/home/u/bin'
    assert fake_db.stored['PATH'] == '/usr/bin:/bin:/home/u/bin'
    assert 'w1' not in shell_path._stale_workplaces


def test_stale_probe_failure_keeps_stored_value(fake_db):
    fake_db.stored['PATH'] = '/usr/bin:/bin'
    shell_path.mark_workplace_stale('w1')
    backend = SSHBackend(exc=RuntimeError('down'))
    env = shell_path.ensure_path_env(AGENT, backend, {})
    assert env['PATH'] == '/usr/bin:/bin'
    assert fake_db.stored['PATH'] == '/usr/bin:/bin'


def test_failure_is_throttled(fake_db):
    backend = SSHBackend(exc=RuntimeError('down'))
    shell_path.ensure_path_env(AGENT, backend, {})
    shell_path.ensure_path_env(AGENT, backend, {})
    assert len(backend.calls) == 1


def test_missing_agent_id_is_skipped(fake_db):
    backend = SSHBackend(_probe_output('/usr/bin:/bin'))
    env = shell_path.ensure_path_env({'workplace_id': 'w1'}, backend, {})
    assert 'PATH' not in env
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def test_backend_detection():
    assert shell_path.is_remote_backend(SSHBackend())
    assert not shell_path.is_remote_backend(LocalBackend())
    assert not shell_path.is_remote_backend(None)
