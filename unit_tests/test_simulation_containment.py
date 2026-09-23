"""Containment tests for agent-template simulation runs (task #24).

A simulation runs a template's REAL tools (fidelity) but must stay contained:

* the backend registry must force an isolating backend (docker/bwrap) even when
  the template ships ``sandbox_enabled: false`` (the DB stores absent as ``0``),
  sets ``run_as_user``, assigns a workplace, or when ``sshc`` installed an
  explicit SSH backend override;
* every write must land under
  ``/tmp/evonic_agent_template_simulation/<sim_id>/`` and never under the live
  ``<BASE_DIR>/agents/<id>/`` or ``<BASE_DIR>/shared/agents/<id>/`` trees.

The activation key is ``agent['simulation_id']`` (see
``backend/tools/lib/simulation_scope.py``).
"""

import os
import sys
import shutil
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.tools.lib.backends.docker_backend as docker_backend
from backend.tools.lib.exec_backend import registry
from backend.tools.lib import simulation_scope as sim
from backend.tools import _workspace as workspace
from backend.tools import save_artifact, list_artifacts

from config import BASE_DIR

_LIVE_SHARED = os.path.join(BASE_DIR, "shared", "agents")
_LIVE_AGENTS = os.path.join(BASE_DIR, "agents")
_SANDBOX_BACKENDS = {"DockerBackend", "BwrapBackend"}


def _sim_agent(sim_id, **extra):
    agent = {"id": sim_id, "session_id": sim_id, "simulation_id": sim_id}
    agent.update(extra)
    return agent


def _sim_root(sim_id):
    return os.path.join(sim.SIM_ROOT_BASE, sim_id)


@pytest.fixture
def sim_id():
    sid = f"testsim-{uuid.uuid4().hex[:10]}"
    try:
        yield sid
    finally:
        sim.cleanup_simulation(sid)


# ---------------------------------------------------------------------------
# 3/4. Force an isolating sandbox regardless of the template
# ---------------------------------------------------------------------------

def test_get_backend_forces_sandbox_when_template_disables_it(sim_id):
    """sandbox_enabled: false in the template must NOT yield LocalBackend."""
    agent = _sim_agent(sim_id, sandbox_enabled=0)
    backend = registry.get_backend(sim_id, agent)
    assert type(backend).__name__ in _SANDBOX_BACKENDS
    assert type(backend).__name__ != "LocalBackend"


def test_get_backend_forces_sandbox_with_run_as_user(sim_id):
    agent = _sim_agent(sim_id, sandbox_enabled=0, run_as_user="nobody")
    backend = registry.get_backend(sim_id, agent)
    assert type(backend).__name__ in _SANDBOX_BACKENDS


def test_get_backend_ignores_workplace_for_simulation(sim_id, monkeypatch):
    """A workplace could resolve to a remote/SSH backend; sims must skip it."""
    called = {}

    def _boom(*args, **kwargs):
        called["hit"] = True
        raise AssertionError("workplace backend must not be used in a simulation")

    import backend.workplaces.manager as wpm
    monkeypatch.setattr(wpm.workplace_manager, "get_backend", _boom)

    agent = _sim_agent(sim_id, workplace_id="wp-remote", sandbox_enabled=1)
    backend = registry.get_backend(sim_id, agent)
    assert "hit" not in called
    assert type(backend).__name__ in _SANDBOX_BACKENDS


def test_get_backend_drops_explicit_ssh_override(sim_id):
    """An SSHBackend installed by sshc must be dropped for a simulation."""

    class _FakeSSH:
        instances = []
        destroyed = False

        def __init__(self):
            _FakeSSH.instances.append(self)

        def destroy(self):
            _FakeSSH.destroyed = True
            return {"result": "connection_closed"}

        def status(self):
            return {"backend": "ssh"}

    fake = _FakeSSH()
    registry.set_backend(sim_id, fake)
    try:
        backend = registry.get_backend(sim_id, _sim_agent(sim_id))
        assert type(backend).__name__ in _SANDBOX_BACKENDS
        assert _FakeSSH.destroyed is True, "SSH override should be destroyed"
    finally:
        registry.clear_backend(sim_id)


def test_non_simulation_defaults_unchanged(monkeypatch):
    """Regression guard: without a simulation id the old behaviour is kept."""
    from backend.tools.lib.backends.local_backend import LocalBackend
    live = {"id": "rina", "session_id": "live-sess", "sandbox_enabled": 0}
    backend = registry.get_backend("live-sess", live)
    assert isinstance(backend, LocalBackend)


# ---------------------------------------------------------------------------
# 1. Per-run artifact registry root (no leak into the live registry)
# ---------------------------------------------------------------------------

def test_backend_artifacts_root_points_into_sim_root(sim_id):
    agent = _sim_agent(sim_id, sandbox_enabled=0)
    backend = registry.get_backend(sim_id, agent)
    expected = os.path.join(sim.SIM_ROOT_BASE, sim_id, "shared", "agents")
    assert getattr(backend, "_artifacts_root", None) == expected


def test_docker_container_binds_sim_artifacts_root(monkeypatch, tmp_path):
    calls = []

    class _Result:
        returncode = 0
        stdout = b"cid"
        stderr = b""

    def _fake_docker(*args, **kwargs):
        calls.append(list(args))
        return _Result()

    monkeypatch.setattr(docker_backend, "_docker", _fake_docker)
    monkeypatch.setattr(docker_backend, "_MOUNT_LAYOUT_VERSION", 99)
    monkeypatch.setattr(docker_backend, "_ensure_reaper_running", lambda: None)
    monkeypatch.setattr(docker_backend, "_ensure_monitor_running", lambda: None)

    sim_root = str(tmp_path / "simroot")
    artifacts_root = os.path.join(sim_root, "shared", "agents")

    def _mounts(sess, workspace):
        calls.clear()
        cid, err = docker_backend._get_or_create_container(
            sess, agent_id="simagent", workspace=workspace,
            persistent=False, artifacts_root=artifacts_root,
        )
        assert err is None and cid == b"cid"
        run_cmd = calls[0]
        return [run_cmd[i + 1] for i, a in enumerate(run_cmd) if a == "-v"]

    # (a) workspace == simulation root: the workspace mount already exposes
    #     <sim_root>/shared/agents, so the self-bind is correctly skipped --
    #     the important guarantee is that the LIVE registry is never bound.
    sess_a = f"testsim-bind-a-{uuid.uuid4().hex[:8]}"
    try:
        mounts_a = _mounts(sess_a, sim_root)
        assert not any(_LIVE_SHARED in m for m in mounts_a), (
            "simulation must not bind the live shared/agents registry")
    finally:
        docker_backend._containers.pop(sess_a, None)

    # (b) workspace != simulation root: the registry is NOT reachable through
    #     the workspace mount, so an explicit bind is emitted -- it must point
    #     at the SIMULATION registry, never the live one.
    sess_b = f"testsim-bind-b-{uuid.uuid4().hex[:8]}"
    try:
        mounts_b = _mounts(sess_b, str(tmp_path / "otherws"))
        expected = (f"{artifacts_root}/simagent/artifacts"
                    f":/workspace/shared/agents/simagent/artifacts:rw")
        assert expected in mounts_b
        assert not any(_LIVE_SHARED in m for m in mounts_b)
    finally:
        docker_backend._containers.pop(sess_b, None)


# ---------------------------------------------------------------------------
# 2. /_self/ resolution is simulation aware
# ---------------------------------------------------------------------------

def test_resolve_self_path_redirects_to_sim_root(sim_id):
    agent = _sim_agent(sim_id)
    p = workspace.resolve_self_path(sim_id, "/_self/kb/notes.md", agent)
    assert p is not None
    assert os.path.realpath(p).startswith(os.path.realpath(_sim_root(sim_id)) + os.sep)
    assert not os.path.realpath(p).startswith(os.path.realpath(_LIVE_AGENTS) + os.sep)


def test_resolve_self_artifacts_redirects_to_sim_root(sim_id):
    agent = _sim_agent(sim_id)
    p = workspace.resolve_self_path(sim_id, "/_self/artifacts/chart.png", agent)
    assert p == os.path.join(
        _sim_root(sim_id), "shared", "agents", sim_id, "artifacts", "chart.png")


def test_live_self_resolution_unchanged_without_simulation():
    p = workspace.resolve_self_path("rina", "/_self/kb/notes.md")
    assert p == os.path.join(_LIVE_AGENTS, "rina", "kb", "notes.md")


def test_resolve_workspace_path_artifacts_redirects_to_sim_root(sim_id):
    agent = _sim_agent(sim_id)
    resolved = workspace.resolve_workspace_path(
        agent, f"/workspace/shared/agents/{sim_id}/artifacts/x.md", "/workspace")
    assert resolved == os.path.join(
        _sim_root(sim_id), "shared", "agents", sim_id, "artifacts", "x.md")


# ---------------------------------------------------------------------------
# 1/2. Tools write under the sim root only
# ---------------------------------------------------------------------------

def test_save_artifact_writes_under_sim_root_not_live(sim_id):
    agent = _sim_agent(sim_id)
    result = save_artifact.execute(agent, {"filename": "report.md", "content": "hello"})
    assert "error" not in result, result
    sim_file = os.path.join(
        _sim_root(sim_id), "shared", "agents", sim_id, "artifacts", "report.md")
    assert os.path.isfile(sim_file)
    assert not os.path.exists(os.path.join(_LIVE_SHARED, sim_id))
    assert not os.path.exists(os.path.join(_LIVE_AGENTS, sim_id))


def test_list_artifacts_reads_from_sim_root(sim_id):
    agent = _sim_agent(sim_id)
    save_artifact.execute(agent, {"filename": "a.md", "content": "# a"})
    result = list_artifacts.execute(agent, {})
    names = [f["filename"] for f in result.get("files", [])]
    assert "a.md" in names
    assert not os.path.exists(os.path.join(_LIVE_SHARED, sim_id))


def test_write_file_self_path_under_sim_root(sim_id):
    from backend.tools import write_file
    agent = _sim_agent(sim_id, safety_checker_enabled=0)
    # The /_self/ resolver is activated for the run via the simulation contextvar
    # (the resolver itself also accepts an explicit agent dict).
    token = sim.set_active_simulation(sim_id)
    try:
        result = write_file.execute(agent, {"file_path": "/_self/note.txt", "content": "hi"})
    finally:
        sim.clear_active_simulation(token)
    assert "error" not in result, result
    target = os.path.join(_sim_root(sim_id), "agents", sim_id, "note.txt")
    assert os.path.isfile(target)
    assert not os.path.exists(os.path.join(_LIVE_AGENTS, sim_id))


# ---------------------------------------------------------------------------
# 6. Teardown removes every trace (incl. defensive live-dir cleanup)
# ---------------------------------------------------------------------------

def test_cleanup_simulation_removes_all_traces():
    sid = f"testsim-cleanup-{uuid.uuid4().hex[:8]}"
    root = _sim_root(sid)
    scratch = f"/tmp/evonic-{sid}-scratchpad"
    leaked_agents = os.path.join(_LIVE_AGENTS, sid)
    leaked_shared = os.path.join(_LIVE_SHARED, sid)
    paths = [os.path.join(root, "agents", sid),
             os.path.join(root, "shared", "agents", sid, "artifacts"),
             scratch, leaked_agents, leaked_shared]
    for p in paths:
        os.makedirs(p, exist_ok=True)
    try:
        report = sim.cleanup_simulation(sid)
        assert report["simulation_id"] == sid
        assert not os.path.exists(root)
        assert not os.path.exists(scratch)
        assert not os.path.exists(leaked_agents)
        assert not os.path.exists(leaked_shared)
    finally:
        for p in [root, scratch, leaked_agents, leaked_shared]:
            shutil.rmtree(p, ignore_errors=True)
