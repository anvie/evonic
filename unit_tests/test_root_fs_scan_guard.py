"""Tests for the root filesystem scan guard (config flag + bash gate).

The guard blocks `find /` / `tree /` behind a manual approval prompt. It is a
performance control that lives OUTSIDE the safety pipeline, so the per-agent
`safety_checker_enabled` toggle does not govern it. Its own switch is the
`root_fs_scan_guard_enabled` DB setting (System -> Settings UI), defaulting to
`config.ROOT_FS_SCAN_GUARD_ENABLED` and force-disabled by the RFS_GUARD_DISABLED=1
env var.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_setting() caches in-process; the test DB is per-test, so clear it."""
    from models.db import db
    db.invalidate_settings_cache()
    yield
    db.invalidate_settings_cache()


def _flag_from_subprocess(env_extra=None):
    """Read config.ROOT_FS_SCAN_GUARD_ENABLED in a clean interpreter."""
    env = {k: v for k, v in os.environ.items() if k != 'RFS_GUARD_DISABLED'}
    env.update(env_extra or {})
    out = subprocess.run(
        [sys.executable, '-c', 'import config; print(config.ROOT_FS_SCAN_GUARD_ENABLED)'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip() == 'True'


# --------------------------------------------------------------------------
# Config flag
# --------------------------------------------------------------------------

def test_flag_defaults_on():
    """Without the env var the guard is enabled (fail-safe default)."""
    assert _flag_from_subprocess() is True


def test_env_var_disables_flag():
    """RFS_GUARD_DISABLED=1 force-disables the guard."""
    assert _flag_from_subprocess({'RFS_GUARD_DISABLED': '1'}) is False


def test_flag_truthy_values():
    """Only 1/true/yes/on are treated as truthy (via _get_env_bool)."""
    assert _flag_from_subprocess({'RFS_GUARD_DISABLED': 'true'}) is False
    assert _flag_from_subprocess({'RFS_GUARD_DISABLED': 'yes'}) is False


def test_flag_garbage_value_keeps_guard_on():
    """Unrecognised values are falsy -> guard stays enabled."""
    assert _flag_from_subprocess({'RFS_GUARD_DISABLED': 'maybe'}) is True


# --------------------------------------------------------------------------
# bash.py helper
# --------------------------------------------------------------------------

def test_helper_honours_config(monkeypatch):
    import config
    from backend.tools import bash

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', False, raising=False)
    assert bash._root_fs_scan_guard_enabled() is False

    # Config on -> falls through to the DB setting, which defaults to '1'.
    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)
    assert bash._root_fs_scan_guard_enabled() is True


def test_helper_follows_db_setting(monkeypatch):
    """With the env var unset, System > Settings (the DB setting) decides."""
    import config
    from backend.tools import bash
    from models.db import db

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)
    db.set_setting('root_fs_scan_guard_enabled', '0')
    assert bash._root_fs_scan_guard_enabled() is False

    db.set_setting('root_fs_scan_guard_enabled', '1')
    assert bash._root_fs_scan_guard_enabled() is True


def test_env_force_disable_beats_db_setting(monkeypatch):
    """RFS_GUARD_DISABLED=1 wins even when the DB setting says enabled."""
    import config
    from backend.tools import bash
    from models.db import db

    db.set_setting('root_fs_scan_guard_enabled', '1')
    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', False, raising=False)
    assert bash._root_fs_scan_guard_enabled() is False


def test_helper_fails_safe_when_db_unavailable(monkeypatch):
    """A DB error must leave the guard enabled (fail safe)."""
    import config
    from backend.tools import bash
    from models.db import db

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)

    def _boom(*args, **kwargs):
        raise RuntimeError('simulated db failure')

    monkeypatch.setattr(db, 'get_setting', _boom)
    assert bash._root_fs_scan_guard_enabled() is True


def test_helper_fails_safe_when_config_unimportable(monkeypatch):
    """Any error while reading config must leave the guard enabled."""
    import builtins
    from backend.tools import bash

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == 'config':
            raise ImportError('simulated config failure')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', _boom)
    assert bash._root_fs_scan_guard_enabled() is True


# --------------------------------------------------------------------------
# bash.execute() integration
# --------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self):
        self.calls = []

    def run_bash(self, script, timeout, env):
        self.calls.append(script)
        return {'exit_code': 0, 'stdout': 'FAKE-RAN', 'stderr': ''}


class _FakeRegistry:
    def __init__(self, backend):
        self._backend = backend

    def get_backend(self, session_id, agent_context):
        return self._backend


@pytest.fixture
def fake_backend(monkeypatch):
    from backend.tools import bash
    backend = _FakeBackend()
    monkeypatch.setattr(bash, 'registry', _FakeRegistry(backend))
    return backend


def _trusted_agent():
    # is_super + safety_checker_enabled=0 keeps the HMADS pipeline out of the
    # way so the assertion is about the root-scan guard alone.
    return {'session_id': 'test-rfs-guard', 'is_super': True, 'safety_checker_enabled': 0}


@pytest.mark.parametrize('script', ['find /', 'find / -name "*.env"', 'tree / -L 2'])
def test_guard_blocks_root_scan_when_enabled(script, fake_backend, monkeypatch):
    import config
    from backend.tools import bash
    from models.db import db

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)
    db.set_setting('root_fs_scan_guard_enabled', '1')
    result = bash.execute(_trusted_agent(), {'script': script})

    assert result.get('level') == 'dangerous', result
    assert 'approval_info' not in result, 'root scan must be auto-rejected, not sent for approval'
    assert result.get('error', '').startswith('REJECTED'), result
    assert result.get('blocked_patterns') == ['root_filesystem_scan'], result
    assert fake_backend.calls == [], 'root scan must not reach the execution backend'


def test_guard_allows_root_scan_when_disabled(fake_backend, monkeypatch):
    import config
    from backend.tools import bash

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', False, raising=False)
    result = bash.execute(_trusted_agent(), {'script': 'find /'})

    assert result.get('stdout') == 'FAKE-RAN', result
    assert fake_backend.calls == ['find /']


def test_guard_disabled_via_db_setting_allows_root_scan(fake_backend, monkeypatch):
    """Toggling the System > Settings switch off lets root scans run."""
    import config
    from backend.tools import bash
    from models.db import db

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)
    db.set_setting('root_fs_scan_guard_enabled', '0')
    result = bash.execute(_trusted_agent(), {'script': 'find /'})

    assert result.get('stdout') == 'FAKE-RAN', result
    assert fake_backend.calls == ['find /']


def test_skip_safety_bypasses_guard_when_enabled(fake_backend, monkeypatch):
    """Post-approval re-execution sets _skip_safety and must not re-prompt."""
    import config
    from backend.tools import bash

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)
    agent = _trusted_agent()
    agent['_skip_safety'] = True
    result = bash.execute(agent, {'script': 'find /'})

    assert result.get('stdout') == 'FAKE-RAN', result
    assert fake_backend.calls == ['find /']


def test_non_root_scan_unaffected(fake_backend, monkeypatch):
    import config
    from backend.tools import bash

    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', True, raising=False)
    result = bash.execute(_trusted_agent(), {'script': 'find /tmp -name "*.log"'})

    assert result.get('stdout') == 'FAKE-RAN', result


# --------------------------------------------------------------------------
# Detector reports an auto-reject (dangerous) verdict
# --------------------------------------------------------------------------

def test_detector_unchanged():
    from backend.tools.lib.heuristic_safety import check_root_filesystem_scan

    hit = check_root_filesystem_scan('find / -type f')
    assert hit is not None
    assert hit['level'] == 'dangerous'
    assert hit['requires_approval'] is False
    assert check_root_filesystem_scan('find /tmp') is None
