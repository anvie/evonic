"""Regression tests for Explorer reuse of its parent's Docker container."""

import os
from unittest.mock import patch

from backend.tools.lib.backends import docker_backend
from backend.tools.lib.backends.docker_backend import DockerBackend
from backend.tools.lib.exec_backend import BackendRegistry


def test_registry_gives_explorer_parent_container_identity(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'docker')
    parent_workspace = str(tmp_path)
    explorer_workspace = str(tmp_path / 'src')
    backend = BackendRegistry().get_backend('explorer-session', {
        'id': 'agent_explorer_1',
        'sandbox_enabled': 1,
        'workspace': explorer_workspace,
        'is_subagent': True,
        'is_explorer': True,
        '_sandbox_parent_session_id': 'parent-session',
        '_sandbox_parent_workspace': parent_workspace,
    })

    assert isinstance(backend, DockerBackend)
    assert backend._session_id == 'explorer-session'
    assert backend._container_session_id == 'parent-session'
    assert backend._workspace == explorer_workspace
    assert backend._container_workspace == parent_workspace
    assert backend.destroy() == {'result': 'shared_container_retained'}
    assert backend.resolve_path(explorer_workspace) == '/workspace/src'


def test_registry_does_not_share_container_for_ordinary_subagent(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'docker')
    backend = BackendRegistry().get_backend('subagent-session', {
        'id': 'agent_sub_1',
        'sandbox_enabled': 1,
        'workspace': str(tmp_path),
        'is_subagent': True,
        '_sandbox_parent_session_id': 'parent-session',
        '_sandbox_parent_workspace': '/parent/workspace',
    })

    assert backend._session_id == 'subagent-session'
    assert backend._container_session_id == 'subagent-session'
    assert backend._container_workspace == str(tmp_path)


def test_explorer_backend_looks_up_parent_pool_entry(tmp_path):
    parent_workspace = str(tmp_path)
    backend = DockerBackend(
        'explorer-session', agent_id='agent_explorer_1',
        workspace=os.path.join(parent_workspace, 'src'), is_subagent=True,
        is_explorer=True, container_session_id='parent-session',
        container_workspace=parent_workspace,
    )
    calls = []

    def fake_get(session_id, agent_id="", workspace=None, persistent=False):
        calls.append((session_id, agent_id, workspace, persistent))
        return "parent-container", None

    result = {"stdout": "", "stderr": "", "exit_code": 0, "execution_time": 0}
    with patch.object(docker_backend, "_get_or_create_container", side_effect=fake_get), \
         patch.object(backend, "_run_code", return_value=result):
        assert backend.run_python("print(1)", 30, {}) == result

    assert calls == [("parent-session", "agent_explorer_1", parent_workspace, False)]
    assert "explorer-session" not in docker_backend._containers
