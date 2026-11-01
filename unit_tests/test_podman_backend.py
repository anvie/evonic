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

from backend.tools.lib.backends import docker_backend, podman_backend
from backend.tools.lib.backends.podman_backend import (
    PodmanBackend,
    build_container_command,
    is_container_gone,
)


@pytest.fixture(autouse=True)
def isolated_state():
    """Keep the module-level pools and the parent hook bindings per test."""
    saved_docker_pool = dict(docker_backend._containers)
    saved_runtime = docker_backend._ACTIVE_RUNTIME
    saved_name = docker_backend._RUNTIME_NAME
    podman_backend._containers.clear()
    docker_backend._containers.clear()
    podman_backend.bind(True)
    yield
    podman_backend._containers.clear()
    docker_backend._containers.clear()
    docker_backend._containers.update(saved_docker_pool)
    docker_backend._ACTIVE_RUNTIME = saved_runtime
    podman_backend.bind(False)
    docker_backend._RUNTIME_NAME = saved_name


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
    assert '-v' in argv
    assert '/ws:/workspace:rw' in argv
    assert 'SCRATCH=/tmp/evonic-a-scratchpad' in argv
    assert argv[-3:] == ['sh', '-c', 'mkdir -p /tmp/evonic-a-scratchpad && exec sleep infinity']
    assert argv[-4] == podman_backend.SANDBOX_PODMAN_IMAGE


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
    assert len(calls) == 1


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

def test_registry_selects_podman_backend(monkeypatch):
    monkeypatch.setenv('SANDBOX_BACKEND', 'podman')
    import importlib
    import config
    importlib.reload(config)
    assert config.SANDBOX_BACKEND == 'podman'

    from backend.tools.lib.exec_backend import BackendRegistry
    backend = BackendRegistry().get_backend('sess-6', {
        'sandbox_enabled': 1, 'agent_id': 'a1', 'workspace': None,
    })
    assert isinstance(backend, PodmanBackend)


def test_registry_selects_docker_backend_by_default(monkeypatch):
    monkeypatch.setenv('SANDBOX_BACKEND', 'docker')
    import importlib
    import config
    importlib.reload(config)

    from backend.tools.lib.exec_backend import BackendRegistry
    backend = BackendRegistry().get_backend('sess-7', {
        'sandbox_enabled': 1, 'agent_id': 'a1', 'workspace': None,
    })
    assert type(backend).__name__ == 'DockerBackend'