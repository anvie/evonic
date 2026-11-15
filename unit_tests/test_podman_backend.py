"""
Unit tests for backend/tools/lib/backends/podman_backend.py

All tests are pure-host (no Podman execution), so they run on macOS and in CI
without a container runtime: container argv construction, CLI binding into the
shared Docker backend module, pool isolation, runtime-exclusivity guard, error
classification, path mapping and backend selection via SANDBOX_BACKEND.
"""

import os
import subprocess

import pytest

import config
from backend.tools.lib.backends import docker_backend, podman_backend
from backend.tools.lib.backends.podman_backend import (
    PodmanBackend,
    build_container_command,
    is_container_gone,
)


_MISSING = object()

@pytest.fixture(autouse=True)
def isolated_state():
    """Run every test as a Podman process, then restore the parent module.

    Import alone no longer binds the hooks: binding follows the runtime *claim*,
    so a Docker process (or another test module) that merely imported this
    module keeps its own pool and CLI. The fixture therefore claims Podman, binds,
    and on teardown restores every parent attribute it touched -- including the
    runtime claim -- so nothing leaks into another test module.
    """
    saved_parent = {
        attr: docker_backend.__dict__.get(attr, _MISSING)
        for attr, _ in podman_backend._BOUND_ATTRS
    }
    saved_claim = docker_backend.get_active_runtime()
    parent_pool = docker_backend._containers
    saved_pool_contents = dict(parent_pool)

    # Start from a claim-free pool: another module in the same process may have
    # left a live container behind, and a claim can only move on an empty pool.
    podman_backend._containers.clear()
    with docker_backend._pool_lock:
        docker_backend._containers.clear()
        docker_backend._ACTIVE_RUNTIME = ''
    docker_backend._claim_runtime('podman')
    podman_backend.bind(True)
    try:
        yield
    finally:
        for attr, value in saved_parent.items():
            if value is _MISSING:
                docker_backend.__dict__.pop(attr, None)
            else:
                setattr(docker_backend, attr, value)
        docker_backend.__dict__['_containers'] = parent_pool
        parent_pool.clear()
        parent_pool.update(saved_pool_contents)
        # Leave no claim behind: with an empty pool the next test may switch.
        docker_backend._ACTIVE_RUNTIME = saved_claim
        docker_backend._RUNTIME_NAME = saved_parent.get('_RUNTIME_NAME', 'docker')


# ---------------------------------------------------------------------------
# Container argv
# ---------------------------------------------------------------------------

def test_build_container_command_core_flags():
    argv = build_container_command(
        name='evonic-sess-1', workspace='/ws', scratch='/tmp/evonic-a-scratchpad',
        persistent=False, created_at=1700000000.0,
    )
    assert argv[0:2] == ['run', '-d']
    assert '--rm' in argv
    assert '--restart=no' in argv
    assert argv[argv.index('--name') + 1] == 'evonic-sess-1'
    assert '--label' in argv
    assert 'evonic.managed=1' in argv
    assert 'evonic.runtime=podman' in argv
    assert 'evonic.persistent=0' in argv
    assert '--userns=keep-id:uid=1000,gid=1000' in argv
    assert '-v' in argv
    assert '/ws:/workspace:rw' in argv
    assert 'SCRATCH=/tmp/evonic-a-scratchpad' in argv
    assert argv[-3:] == ['sh', '-c', 'mkdir -p /tmp/evonic-a-scratchpad && exec sleep infinity']
    assert argv[-4] == podman_backend.SANDBOX_PODMAN_IMAGE


def test_userns_default_maps_image_user_onto_host_uid():
    # Rootless Podman otherwise maps the container to a subuid, which makes the
    # workspace bind mount unwritable for the sandbox user.
    assert podman_backend._userns_arg() == 'keep-id:uid=1000,gid=1000'


def test_userns_flag_overrides(monkeypatch):
    monkeypatch.setattr(podman_backend, '_PODMAN_USERNS', 'keep-id')
    assert podman_backend._userns_arg() == 'keep-id'
    monkeypatch.setattr(podman_backend, '_PODMAN_USERNS', 'keep-id:uid=2000,gid=2000')
    assert podman_backend._userns_arg() == 'keep-id:uid=2000,gid=2000'


def test_userns_missing_in_argv_when_disabled(monkeypatch):
    monkeypatch.setattr(podman_backend, '_PODMAN_USERNS_TEMPLATE', '{uid}{gid}')
    monkeypatch.setattr(podman_backend, '_PODMAN_USERNS', '')
    monkeypatch.setattr(podman_backend, '_PODMAN_USER', '')
    monkeypatch.setattr(podman_backend, '_userns_arg', lambda: '')
    argv = build_container_command(name='n', workspace='/ws', scratch='/tmp/s')
    assert not any(a.startswith('--userns') for a in argv)


def test_workspace_writable_ok(monkeypatch):
    class Proc:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr(podman_backend, '_runtime', lambda *a, **k: Proc())
    assert podman_backend.workspace_writable('cid') == (True, 'ok')


def test_workspace_writable_reports_permission_denied(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ''
        stderr = "sh: 1: cannot create /workspace/.evonic-write-probe: Permission denied"

    monkeypatch.setattr(podman_backend, '_runtime', lambda *a, **k: Proc())
    ok, why = podman_backend.workspace_writable('cid')
    assert ok is False
    assert 'Permission denied' in why


def test_get_or_create_rejects_read_only_workspace(monkeypatch):
    class RunProc:
        returncode = 0
        stdout = 'deadbeef\n'
        stderr = ''

    recorded = []

    def fake(*args, **kwargs):
        recorded.append(args)
        return RunProc()

    monkeypatch.setattr(podman_backend, '_runtime', fake)
    monkeypatch.setattr(podman_backend, 'workspace_writable',
                        lambda cid, *a, **k: (False, 'Permission denied'))
    container_id, err = podman_backend._get_or_create_container(
        'sess-ro', agent_id='a-ro', workspace='/ws')
    assert container_id is None
    assert 'not writable' in err
    # The unusable container must be removed, never pooled.
    assert podman_backend._containers == {}
    assert any(call[:2] == ('rm', '-f') for call in recorded)


def test_userns_absent_from_argv_when_disabled(monkeypatch):
    monkeypatch.setattr(podman_backend, '_userns_arg', lambda: '')
    argv = build_container_command(name='n', workspace='/ws', scratch='/tmp/s')
    assert not any(a.startswith('--userns') for a in argv)


def test_build_container_command_persistent_has_no_rm():
    argv = build_container_command(
        name='evonic-agent', workspace='/ws', scratch='/tmp/s', persistent=True,
        created_at=1700000000.0,
    )
    # Rootless Podman refuses `unless-stopped`, and a persistent container must
    # survive Evonic restarts, so it is never created with --rm.
    assert '--rm' not in argv
    assert '--restart=no' in argv
    assert 'evonic.persistent=1' in argv


def test_build_container_command_image_override():
    argv = build_container_command(
        name='n', workspace='/ws', scratch='/tmp/s', image='quay.io/example/sandbox:1',
    )
    assert argv[-4] == 'quay.io/example/sandbox:1'


def test_build_container_command_includes_artifact_mounts():
    argv = build_container_command(
        name='n', workspace='/ws', scratch='/tmp/s',
        artifacts_mounts=['-v', '/host/art:/workspace/shared/agents/a/artifacts:rw'],
    )
    assert '/host/art:/workspace/shared/agents/a/artifacts:rw' in argv


# ---------------------------------------------------------------------------
# Runtime hooks bound into the shared Docker backend module
# ---------------------------------------------------------------------------

def test_bind_points_parent_at_podman_pool_and_cli():
    assert podman_backend.is_bound()
    assert docker_backend._containers is podman_backend._containers
    assert docker_backend._docker is podman_backend._runtime
    assert docker_backend._docker_popen is podman_backend._runtime_popen
    assert docker_backend._RUNTIME_NAME == 'podman'
    assert docker_backend._effective_image() == podman_backend.SANDBOX_PODMAN_IMAGE
    assert docker_backend._restart_policy(True) == 'no'
    assert docker_backend.get_pool_status()['backend'] == 'podman'


def test_unbind_restores_docker_defaults():
    podman_backend.bind(False)
    assert docker_backend._docker.__name__ == '_docker'
    assert docker_backend._RUNTIME_NAME == 'docker'
    assert docker_backend._effective_image() == docker_backend.SANDBOX_IMAGE
    assert docker_backend._restart_policy(True) == 'unless-stopped'
    assert not podman_backend.is_bound()


def test_sibling_names_include_podman_containers_for_docker_sweep():
    podman_backend._containers['sess-a'] = {
        'container_id': 'c0ffee', 'container_name': 'evonic-sess-a',
        'agent_id': '', 'last_used': 0.0, 'created_at': 0.0, 'first_call': False,
        'workspace': '/ws', 'persistent': False, 'pool_key': 'sess-a',
    }
    # While Podman is bound, the parent is Podman itself -> no siblings.
    assert docker_backend._sibling_container_names() == set()
    podman_backend.bind(False)
    # Restored to Docker: it must see the Podman container as a sibling.
    assert 'evonic-sess-a' in docker_backend._sibling_container_names()


def test_pool_status_reports_podman_backend():
    podman_backend._containers['s'] = {
        'container_id': 'abc123def456', 'container_name': 'evonic-s',
        'agent_id': 'a1', 'last_used': 1.0, 'created_at': 1.0, 'first_call': False,
        'workspace': '/ws', 'persistent': True, 'pool_key': 'a1',
    }
    status = podman_backend.get_pool_status()
    assert status['backend'] == 'podman'
    assert status['pool_size'] == 1
    assert status['persistent_count'] == 1


# ---------------------------------------------------------------------------
# Runtime exclusivity guard
# ---------------------------------------------------------------------------

def test_claim_runtime_is_idempotent():
    docker_backend._claim_runtime('podman')
    docker_backend._claim_runtime('podman')
    assert docker_backend._ACTIVE_RUNTIME == 'podman'


def test_claim_runtime_refuses_switch_with_live_pool():
    docker_backend._claim_runtime('docker')
    docker_backend._containers['live'] = {'container_id': 'x'}
    with pytest.raises(RuntimeError) as excinfo:
        docker_backend._claim_runtime('podman')
    assert 'container pool' in str(excinfo.value)


def test_claim_runtime_allows_switch_on_empty_pool():
    docker_backend._claim_runtime('docker')
    docker_backend._claim_runtime('podman')
    assert docker_backend._ACTIVE_RUNTIME == 'podman'


def test_podman_backend_construct_claims_runtime():
    PodmanBackend('sess-1', agent_id='a1')
    assert docker_backend._ACTIVE_RUNTIME == 'podman'


def test_import_does_not_hijack_a_docker_process():
    """A Docker process that merely imported this module keeps its own hooks.

    Importing is what a shared deployment, a plugin or another test module does
    without asking for Podman; taking the pool from under it would make the
    parent cleanup run ``podman rm`` against Docker containers.
    """
    # Start from the unbound module, then let Docker claim the pool.
    podman_backend.bind(False)
    docker_backend._ACTIVE_RUNTIME = 'docker'
    try:
        original_cli = docker_backend.__dict__.get('_docker')
        podman_backend.bind(True)
        # The refused bind leaves Docker hooks and pool untouched.
        assert docker_backend.__dict__.get('_docker') is original_cli
        assert docker_backend._RUNTIME_NAME == 'docker'
        assert not podman_backend.is_bound()
    finally:
        docker_backend._ACTIVE_RUNTIME = 'podman'


def test_release_hands_the_parent_its_own_pool_object_back():
    parent_pool = podman_backend._PARENT_POOL
    podman_backend._containers['sess-rel'] = {
        'container_id': 'deadbeef0000', 'container_name': 'evonic-sess-rel',
        'agent_id': '', 'last_used': 0.0, 'created_at': 0.0, 'first_call': False,
        'workspace': '/ws', 'persistent': False, 'pool_key': 'sess-rel',
    }
    podman_backend.bind(False)
    # The object other modules captured at import time is back in place, and the
    # Podman container stays in this module's pool (the parent would otherwise
    # hand it to a runtime that does not own it).
    assert docker_backend._containers is parent_pool
    assert 'sess-rel' not in parent_pool
    assert 'sess-rel' in podman_backend._containers
    # Restore the bound state for the tests that follow.
    docker_backend._claim_runtime('podman')
    podman_backend.bind(True)
    podman_backend._containers.clear()


def test_release_is_skipped_while_another_runtime_owns_the_pool():
    podman_backend._containers['sess-x'] = {
        'container_id': 'abc000000000', 'container_name': 'evonic-sess-x',
        'agent_id': '', 'last_used': 0.0, 'created_at': 0.0, 'first_call': False,
        'workspace': '/ws', 'persistent': False, 'pool_key': 'sess-x',
    }
    docker_backend._ACTIVE_RUNTIME = 'docker'
    try:
        podman_backend.bind(False)
        # Nothing was restored: the pool still points at the Podman module.
        assert docker_backend._containers is podman_backend._containers
    finally:
        docker_backend._ACTIVE_RUNTIME = 'podman'
    podman_backend._containers.pop('sess-x', None)


# ---------------------------------------------------------------------------
# Error classification and availability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('result,expected', [
    ({'exit_code': 0, 'stdout': 'ok'}, False),
    ({'error': 'Error: no such container: evonic-x'}, True),
    ({'error': 'container not found'}, True),
    ({'exit_code': 1, 'stderr': 'Error response from daemon: container abc is not running'}, True),
    ({'exit_code': 1, 'stderr': 'traceback'}, False),
])
def test_is_container_gone(result, expected):
    assert is_container_gone(result) is expected


def test_cli_available_false_for_missing_binary(monkeypatch):
    monkeypatch.setattr(podman_backend, '_RUNTIME_CLI', 'definitely-not-a-binary-xyz')
    assert podman_backend.cli_available() is False
    ok, detail = podman_backend.runtime_reachable()
    assert ok is False
    assert 'not found on PATH' in detail


def test_status_without_runtime(monkeypatch):
    monkeypatch.setattr(podman_backend, '_RUNTIME_CLI', 'definitely-not-a-binary-xyz')
    backend = PodmanBackend('sess-2', agent_id='a1')
    status = backend.status()
    assert status['backend'] == 'podman'
    assert status['runtime_available'] is False
    assert status['image'] == podman_backend.SANDBOX_PODMAN_IMAGE


def test_status_reports_bound_container():
    backend = PodmanBackend('sess-3', agent_id='a1')
    podman_backend._containers['sess-3'] = {
        'container_id': 'deadbeefcafe', 'container_name': 'evonic-sess-3',
        'agent_id': 'a1', 'last_used': 2.0, 'created_at': 1.0, 'first_call': False,
        'workspace': '/ws', 'persistent': False, 'pool_key': 'sess-3',
    }
    status = backend.status()
    # status() truncates the ID to 12 chars; the fixture already is 12.
    assert status['container_id'] == 'deadbeefcafe'
    assert status['workspace'] == '/ws'


def test_get_or_create_reports_missing_cli(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError('podman')
    monkeypatch.setattr(podman_backend, '_runtime', boom)
    container_id, err = podman_backend._get_or_create_container('sess-x', agent_id='a1')
    assert container_id is None
    assert 'Podman CLI not found' in err
    assert podman_backend._containers == {}


def test_get_or_create_parses_container_id(monkeypatch):
    class Proc:
        returncode = 0
        stdout = 'abcdef123456\n'
        stderr = ''

    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return Proc()

    monkeypatch.setattr(podman_backend, '_runtime', fake)
    monkeypatch.setattr(podman_backend, 'ensure_reaper_running', lambda: None)
    monkeypatch.setattr(podman_backend, 'ensure_monitor_running', lambda: None)
    container_id, err = podman_backend._get_or_create_container(
        'sess-y', agent_id='a1', workspace='/ws', persistent=True)
    assert err is None
    assert container_id == 'abcdef123456'
    assert calls[0][0] == 'run'
    entry = podman_backend._containers['a1']
    assert entry['persistent'] is True
    assert entry['runtime'] == 'podman'
    # A second call reuses the pooled container instead of creating another.
    again, err2 = podman_backend._get_or_create_container(
        'sess-y', agent_id='a1', workspace='/ws', persistent=True)
    assert (again, err2) == (container_id, None)
    # One `run` plus the one-off workspace writability probe; reuse must not add
    # another container.
    assert sum(1 for c in calls if c[0] == 'run') == 1


def test_build_container_command_keeps_image_after_mounts():
    """The image must be the first non-flag token, never a `-v` target.

    Rootless Podman silently treats a truncated argv as
    ``-v <image>:/workspace:rw <workspace>``, which mounts an image-named empty
    volume over /workspace and makes the sandbox read-only.
    """
    argv = build_container_command(
        name='evonic-sess-img', workspace='/ws',
        scratch='/tmp/evonic-a-scratchpad', persistent=False, created_at=0.0,
    )
    image_index = argv.index(podman_backend.SANDBOX_PODMAN_IMAGE)
    assert argv[image_index + 1:image_index + 3] == ['sh', '-c']
    for i, arg in enumerate(argv):
        if arg == '-v':
            assert podman_backend.SANDBOX_PODMAN_IMAGE not in argv[i + 1]
    assert '-w' in argv and argv[argv.index('-w') + 1] == '/workspace'


def test_container_create_args_carry_userns_and_security_opt():
    args = podman_backend._container_create_args()
    assert '--userns=keep-id:uid=1000,gid=1000' in args
    assert '--security-opt=label=disable' in args


def test_container_create_args_honour_overrides(monkeypatch):
    monkeypatch.setattr(podman_backend, '_PODMAN_USERNS', 'auto')
    monkeypatch.setattr(podman_backend, '_PODMAN_SECURITY_OPT', '')
    assert podman_backend._container_create_args() == ['--userns=auto']


def test_parent_creation_path_routes_through_podman_builder(monkeypatch):
    """``run_bash`` reaches the parent module, which must use Podman's argv.

    Regression test: the inherited creation path built its own Docker argv, so
    a Podman container was created without ``--userns``/``--security-opt`` and
    the bind-mounted workspace came up read-only.
    """
    created = []

    class Proc:
        returncode = 0
        stdout = 'abcdef123456\n'
        stderr = ''

    def fake(*args, **kwargs):
        created.append(args)
        return Proc()

    monkeypatch.setattr(podman_backend, '_runtime', fake)
    monkeypatch.setattr(podman_backend, 'ensure_reaper_running', lambda: None)
    monkeypatch.setattr(podman_backend, 'ensure_monitor_running', lambda: None)
    podman_backend._containers.clear()

    container_id, err = docker_backend._get_or_create_container(
        'sess-parent', agent_id='a1', workspace='/ws', persistent=False)

    assert err is None and container_id == 'abcdef123456'
    run_args = created[0]
    assert '--userns=keep-id:uid=1000,gid=1000' in run_args
    assert '--security-opt=label=disable' in run_args
    assert '--restart=no' in run_args
    assert 'evonic.runtime=podman' in run_args


def test_destroy_container_uses_podman_rm(monkeypatch):
    recorded = []

    class Proc:
        returncode = 0
        stdout = ''
        stderr = ''

    def fake(*args, **kwargs):
        recorded.append(args)
        return Proc()

    podman_backend._containers['sess-z'] = {
        'container_id': 'cid123', 'container_name': 'evonic-sess-z',
        'agent_id': '', 'last_used': 0.0, 'created_at': 0.0, 'first_call': False,
        'workspace': '/ws', 'persistent': False, 'pool_key': 'sess-z',
    }
    monkeypatch.setattr(podman_backend, '_runtime', fake)
    result = podman_backend._destroy_container('sess-z')
    assert result['result'] == 'container_destroyed'
    assert recorded[0][:2] == ('rm', '-f')
    assert podman_backend._containers == {}


# ---------------------------------------------------------------------------
# Path mapping (inherited from DockerBackend)
# ---------------------------------------------------------------------------

def test_resolve_path_maps_workspace(tmp_path):
    backend = PodmanBackend('sess-4', agent_id='a1', workspace=str(tmp_path))
    assert backend.resolve_path(os.path.join(str(tmp_path), 'x', 'y.txt')) == '/workspace/x/y.txt'


def test_resolve_path_maps_artifact_registry(tmp_path):
    artifacts_root = tmp_path / 'shared' / 'agents'
    registry = artifacts_root / 'a1' / 'artifacts'
    registry.mkdir(parents=True)
    backend = PodmanBackend('sess-5', agent_id='a1', workspace=str(tmp_path),
                            artifacts_root=str(artifacts_root))
    resolved = backend.resolve_path(str(registry / 'report.md'))
    assert resolved == '/workspace/shared/agents/a1/artifacts/report.md'


# ---------------------------------------------------------------------------
# Backend selection via SANDBOX_BACKEND
# ---------------------------------------------------------------------------

def _selected_backend_name(monkeypatch, sandbox_backend):
    """Return which backend the registry picks, without starting a container.

    Constructing a real backend would create one, so the module references the
    registry imports from are replaced with recorders and only the choice is
    inspected.
    """
    import importlib
    import backend.tools.lib.exec_backend as exec_backend

    requested = []

    class _FakeBackend:
        def __init__(self, label):
            self.label = label

    def _recorder(label):
        class _Ctor(_FakeBackend):
            def __init__(self, *args, **kwargs):
                super().__init__(label)
                requested.append(label)
        return _Ctor

    for label, dotted in (
        ('docker', 'backend.tools.lib.backends.docker_backend.DockerBackend'),
        ('podman', 'backend.tools.lib.backends.podman_backend.PodmanBackend'),
        ('bwrap', 'backend.tools.lib.backends.bwrap_backend.BwrapBackend'),
    ):
        mod_name, cls_name = dotted.rsplit('.', 1)
        module = importlib.import_module(mod_name)
        monkeypatch.setattr(module, cls_name, _recorder(label), raising=False)
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', sandbox_backend, raising=False)
    exec_backend.BackendRegistry().get_backend('sess-sel', {
        'sandbox_enabled': 1, 'agent_id': 'a1', 'workspace': None,
    })
    return requested[0] if requested else ''


# ---------------------------------------------------------------------------
# Backend selection via SANDBOX_BACKEND
# ---------------------------------------------------------------------------

def test_registry_selects_podman_backend(monkeypatch):
    monkeypatch.setenv('SANDBOX_BACKEND', 'podman')
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'podman', raising=False)
    assert _selected_backend_name(monkeypatch, 'podman') == 'podman'


def test_registry_selects_docker_backend_by_default(monkeypatch):
    monkeypatch.setenv('SANDBOX_BACKEND', 'docker')
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'docker', raising=False)
    assert _selected_backend_name(monkeypatch, 'docker') == 'docker'


def test_registry_selects_bwrap_backend(monkeypatch):
    monkeypatch.setenv('SANDBOX_BACKEND', 'bwrap')
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'bwrap', raising=False)
    assert _selected_backend_name(monkeypatch, 'bwrap') == 'bwrap'
