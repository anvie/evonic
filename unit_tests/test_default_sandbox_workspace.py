"""Sandbox workspace default."""

import os


def test_default_sandbox_workspace_is_per_agent_not_repo_root(monkeypatch, tmp_path):
    from backend.tools import _workspace

    monkeypatch.delenv('SANDBOX_WORKSPACE', raising=False)
    monkeypatch.setattr(_workspace, '_SHARED_AGENTS_DIR', str(tmp_path))
    path = _workspace.default_sandbox_workspace('agent-1')
    assert path == str(tmp_path / 'agent-1')
    assert os.path.isdir(path)
    assert _workspace.default_sandbox_workspace('../etc') == str(tmp_path / '___etc')
    assert _workspace.default_sandbox_workspace('') == str(tmp_path / 'default')


def test_explicit_sandbox_workspace_env_is_respected(monkeypatch, tmp_path):
    from backend.tools import _workspace

    monkeypatch.setenv('SANDBOX_WORKSPACE', str(tmp_path))
    assert _workspace.default_sandbox_workspace('agent-1') == str(tmp_path)
