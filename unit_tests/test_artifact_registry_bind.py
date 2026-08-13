"""Tests for the artifact registry bind-mount fix (artifact divergence bug).

Root cause: when an agent's workspace differs from BASE_DIR (the evonic root),
the sandbox /workspace mount does NOT include BASE_DIR/shared/agents/<id>/,
so /workspace/shared/agents/<id>/artifacts would silently resolve to a
DIFFERENT directory than the host registry the web UI / list_artifacts /
fetch_artifact serve. The fix bind-mounts the host registry into the sandbox
at the same relative path so bash/runpy writes land on the authoritative copy.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.tools.lib.backends.bwrap_backend as bwrap_backend
import backend.tools.lib.backends.docker_backend as docker_backend


# ---------------------------------------------------------------------------
# Docker backend: container cmd must include the artifacts bind mount
# ---------------------------------------------------------------------------

class _DockerResult:
    def __init__(self, rc=0, stdout=b"cid123", stderr=b""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_docker_container_cmd_includes_artifacts_bind(tmp_path, monkeypatch):
    """The docker run command must bind the host registry at
    /workspace/shared/agents/<id>/artifacts for a workspace != BASE_DIR."""
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(list(args))
        return _DockerResult()

    monkeypatch.setattr(docker_backend, "_docker", fake_docker)
    monkeypatch.setattr(docker_backend, "_list_persistent_stopped_containers", lambda: [])
    # Bump the layout version so the (empty) pool never reuses a stale entry.
    monkeypatch.setattr(docker_backend, "_MOUNT_LAYOUT_VERSION", 99)

    workspace = str(tmp_path)  # differs from the evonic root
    cid, err = docker_backend._get_or_create_container(
        "sess-x", agent_id="rina", workspace=workspace, persistent=False
    )
    assert err is None
    assert cid == b"cid123"
    assert calls, "docker run should have been invoked"

    run_cmd = calls[0]
    assert "-v" in run_cmd
    mounts = [run_cmd[i + 1] for i, a in enumerate(run_cmd) if a == "-v"]
    expected = f"{docker_backend._ARTIFACTS_ROOT}/rina/artifacts:/workspace/shared/agents/rina/artifacts:rw"
    assert expected in mounts, f"artifacts bind mount missing in {mounts}"


def test_docker_container_cmd_skips_artifacts_bind_for_no_agent(monkeypatch, tmp_path):
    """No artifacts bind when agent_id is empty (generic sessions)."""
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(list(args))
        return _DockerResult()

    monkeypatch.setattr(docker_backend, "_docker", fake_docker)
    monkeypatch.setattr(docker_backend, "_MOUNT_LAYOUT_VERSION", 99)

    cid, err = docker_backend._get_or_create_container(
        "sess-y", agent_id="", workspace=str(tmp_path), persistent=False
    )
    assert err is None and cid == b"cid123"
    run_cmd = calls[0]
    mounts = [run_cmd[i + 1] for i, a in enumerate(run_cmd) if a == "-v"]
    assert not any("/workspace/shared/agents/" in m for m in mounts)


def test_docker_pool_recreates_when_layout_version_changes(monkeypatch, tmp_path):
    """An existing pool entry with a stale mount_version must be recreated."""
    destroyed = []
    monkeypatch.setattr(docker_backend, '_destroy_container', lambda sid: destroyed.append(sid))
    monkeypatch.setattr(docker_backend, '_MOUNT_LAYOUT_VERSION', 7)
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(list(args))
        return _DockerResult()

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)

    workspace = str(tmp_path)
    docker_backend._containers['rina'] = {
        'container_id': 'old123',
        'container_name': 'evonic-rina',
        'agent_id': 'rina',
        'last_used': 1.0,
        'created_at': 1.0,
        'first_call': False,
        'workspace': workspace,
        'mount_version': 6,  # stale layout
        'persistent': True,
        'pool_key': 'rina',
    }
    try:
        cid, err = docker_backend._get_or_create_container(
            'sess-z', agent_id='rina', workspace=workspace, persistent=True
        )
        assert err is None and cid == b'cid123'
        new_entry = docker_backend._containers.get('rina')
        assert new_entry is not None, 'recreated container should be in the pool'
        assert new_entry['mount_version'] == 7
    finally:
        docker_backend._containers.pop('rina', None)
    assert destroyed == ['rina'], 'stale container should be destroyed before recreate'
    assert calls, 'a fresh docker run must follow the destroy'

def _make_bwrap_backend(workspace, agent_id="rina"):
    b = bwrap_backend.BwrapBackend(
        session_id="sess-t", workspace=workspace, agent_id=agent_id
    )
    return b


def test_bwrap_to_host_maps_artifacts_to_registry(tmp_path):
    ws = str(tmp_path)
    b = _make_bwrap_backend(ws, agent_id="rina")
    sandbox_path = "/workspace/shared/agents/rina/artifacts/location-variations.md"
    host = b._to_host(sandbox_path)
    expected = os.path.join(
        bwrap_backend._ARTIFACTS_ROOT, "rina", "artifacts", "location-variations.md"
    )
    assert host == expected
    # Directory itself
    assert b._to_host("/workspace/shared/agents/rina/artifacts") == os.path.join(
        bwrap_backend._ARTIFACTS_ROOT, "rina", "artifacts"
    )


def test_bwrap_to_host_keeps_other_workspace_paths(tmp_path):
    ws = str(tmp_path)
    b = _make_bwrap_backend(ws, agent_id="rina")
    assert b._to_host("/workspace/foo.txt") == os.path.join(ws, "foo.txt")
    assert b._to_host("/home/agent/x.md") == os.path.join(ws, ".home", "x.md")
    # Different agent id -> falls through to normal workspace mapping
    b2 = _make_bwrap_backend(ws, agent_id="other")
    assert b2._to_host("/workspace/shared/agents/rina/artifacts/a.md") == os.path.join(
        ws, "shared/agents/rina/artifacts/a.md"
    )


def test_bwrap_resolve_path_roundtrip_registry(tmp_path):
    """resolve_path(host registry path) -> sandbox path -> _to_host returns the
    same host registry path (round-trip stability for file tools)."""
    ws = str(tmp_path)
    b = _make_bwrap_backend(ws, agent_id="rina")
    sandbox = "/workspace/shared/agents/rina/artifacts/x.md"
    host = b._to_host(sandbox)
    # File tools call resolve_path first on the host-resolved path; for bwrap
    # the registry path is outside the workspace, so resolve_path passes it
    # through unchanged and _to_host maps it to the registry host path.
    host2 = b._to_host(b.resolve_path(host))
    assert host2 == host


# ---------------------------------------------------------------------------
# resolve_workspace_path: /workspace/shared/agents/<id>/artifacts -> host registry
# ---------------------------------------------------------------------------

def test_resolve_workspace_path_artifacts_maps_to_host_registry():
    from backend.tools._workspace import resolve_workspace_path
    agent = {'id': 'rina', 'workspace': '/home/robin/dev/evonic/agents/rina',
             'sandbox_enabled': 1}
    resolved = resolve_workspace_path(
        agent, '/workspace/shared/agents/rina/artifacts/location-variations.md', '/workspace')
    assert resolved == '/home/robin/dev/evonic/shared/agents/rina/artifacts/location-variations.md'


def test_resolve_workspace_path_artifacts_subagent_uses_parent():
    from backend.tools._workspace import resolve_workspace_path
    sub = {'id': 'sub1', 'parent_id': 'rina', 'is_subagent': True,
           'workspace': '/tmp/scratch', 'sandbox_enabled': 1}
    resolved = resolve_workspace_path(
        sub, '/workspace/shared/agents/rina/artifacts/x.md', '/workspace')
    assert resolved == '/home/robin/dev/evonic/shared/agents/rina/artifacts/x.md'


def test_resolve_workspace_path_non_artifacts_untouched():
    from backend.tools._workspace import resolve_workspace_path
    agent = {'id': 'rina', 'workspace': '/home/robin/dev/evonic/agents/rina',
             'sandbox_enabled': 1}
    resolved = resolve_workspace_path(agent, '/workspace/pics/foo.png', '/workspace')
    assert resolved == '/home/robin/dev/evonic/agents/rina/pics/foo.png'
    # Other agents' artifacts must NOT be hijacked to rina's registry
    other = resolve_workspace_path(
        agent, '/workspace/shared/agents/siwa/artifacts/y.md', '/workspace')
    assert other == '/home/robin/dev/evonic/agents/rina/shared/agents/siwa/artifacts/y.md'


# ---------------------------------------------------------------------------
# Backend resolve_path round-trip: host registry -> sandbox view
# ---------------------------------------------------------------------------

def test_docker_resolve_path_translates_host_registry(tmp_path):
    backend = docker_backend.DockerBackend(session_id='s1', agent_id='rina',
                                           workspace=str(tmp_path))
    host = f'{docker_backend._ARTIFACTS_ROOT}/rina/artifacts/location-variations.md'
    view = backend.resolve_path(host)
    assert view == '/workspace/shared/agents/rina/artifacts/location-variations.md'


def test_bwrap_resolve_path_to_host_roundtrip(tmp_path):
    b = bwrap_backend.BwrapBackend(session_id='s1', agent_id='rina',
                                   workspace=str(tmp_path))
    host = f'{bwrap_backend._ARTIFACTS_ROOT}/rina/artifacts/location-variations.md'
    view = b.resolve_path(host)
    assert view == '/workspace/shared/agents/rina/artifacts/location-variations.md'
    # Reverse mapping via _to_host must land back on the host registry
    assert b._to_host(view) == host
    # Sandbox view of a workspace file still maps to the workspace
    ws_view = b.resolve_path(str(tmp_path) + '/pics/a.png')
    assert ws_view == '/workspace/pics/a.png'
    assert b._to_host(ws_view) == str(tmp_path) + '/pics/a.png'


def test_docker_skips_artifacts_bind_when_workspace_is_base(monkeypatch):
    """When workspace == SANDBOX_WORKSPACE (BASE_DIR), the registry is already
    reachable at /workspace/shared/agents/<id>/artifacts; no extra bind is
    added (mounting a directory onto itself would be redundant)."""
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(list(args))
        return _DockerResult()

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)
    monkeypatch.setattr(docker_backend, '_MOUNT_LAYOUT_VERSION', 99)

    base = docker_backend.SANDBOX_WORKSPACE  # real BASE_DIR
    cid, err = docker_backend._get_or_create_container(
        'sess-w', agent_id='rina', workspace=base, persistent=False
    )
    assert err is None and cid == b'cid123'
    run_cmd = calls[0]
    mounts = [run_cmd[i + 1] for i, a in enumerate(run_cmd) if a == '-v']
    assert not any('/workspace/shared/agents/' in m for m in mounts)


def test_bwrap_skips_artifacts_bind_when_workspace_is_base(tmp_path, monkeypatch):
    from backend.tools.lib.backends.bwrap_backend import BwrapBackend, _ARTIFACTS_ROOT
    b = BwrapBackend(session_id='s1', agent_id='rina', workspace=str(tmp_path))
    argv = b._bwrap_argv()
    assert _ARTIFACTS_ROOT not in argv or '/workspace/shared/agents/rina/artifacts' not in argv
    # workspace == BASE_DIR: registry already at that path -> no bind
    assert '--bind' not in argv or not any(
        '/workspace/shared/agents/rina/artifacts' == argv[i + 1]
        for i, a in enumerate(argv) if a == '--bind')
