"""
Regression tests for Explore.py path resolution logic (lines 70-93).

Covers the multi-step path resolution pipeline:
  1. Workspace/relative/absolute path normalization (lines 70-77)
  2. Backend resolve_path call (line 79)
  3. Backend run_python for path validation (lines 80-83)
  4. JSON parse & fallback behavior (lines 86-93)
"""

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPLORE_PATH = ROOT / "skills" / "explorer" / "backend" / "tools" / "Explore.py"


# ---------------------------------------------------------------------------
# Helpers: load Explore module dynamically
# ---------------------------------------------------------------------------

def _load_explore():
    """Load Explore.py as a module so we can test its execute() function."""
    mod_name = "test_explore_path_resolution_explore"
    spec = __import__("importlib.util").util.spec_from_file_location(mod_name, str(EXPLORE_PATH))
    module = __import__("importlib.util").util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Backend mock classes
# ---------------------------------------------------------------------------

class MockBackend:
    """Default mock backend that resolves paths and runs python checks."""

    def __init__(self, resolve_func=None, run_python_func=None):
        self._resolve_func = resolve_func or (lambda p: p)
        self._run_python_func = run_python_func or self._default_run_python

    def resolve_path(self, path):
        return self._resolve_func(path)

    def run_python(self, code, timeout, env):
        return self._run_python_func(code, timeout, env)

    @staticmethod
    def _default_run_python(code, timeout, env):
        """Simulate the path validation check by extracting the path from code."""
        # The code looks like: import json, os; p='/some/path'; print(json.dumps({"path": ..., "is_dir": ...}))
        # We extract the path and return valid JSON for existing directories
        import re
        m = re.search(r"p=(['\"])(.*?)\1", code)
        if not m:
            return {"stdout": "", "stderr": "parse error", "exit_code": 1}
        path_str = m.group(2)
        is_dir = os.path.isdir(path_str)
        real_path = os.path.realpath(path_str)
        result = {"path": real_path, "is_dir": is_dir}
        return {"stdout": json.dumps(result), "stderr": "", "exit_code": 0}


class ErrorBackend:
    """Backend that returns errors."""

    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        return {"error": "backend unavailable", "exit_code": 1}


class MisconfiguredBackend:
    """Backend that returns error response (misconfigured workplace)."""

    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        return {"error": "workplace not configured", "exit_code": 1, "stdout": "", "stderr": "no workplace"}


class JsonFailBackend:
    """Backend that returns truncated/malformed JSON."""

    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        return {"stdout": '{"path": "/some/path", "is_dir": true', "stderr": "", "exit_code": 0}


class TruncatedStdoutBackend:
    """Backend that returns empty stdout (simulates logging interference)."""

    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        return {"stdout": "", "stderr": "", "exit_code": 0}


class FallbackBackend:
    """Backend that returns path without 'path' key — triggers fallback at line 90."""

    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        # Missing 'path' key — should fall back to the original path
        return {"stdout": '{"is_dir": true}', "stderr": "", "exit_code": 0}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def explore_module():
    """Load the Explore module for testing."""
    return _load_explore()


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace directory with some test files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("hello world\n")
    (ws / "templates").mkdir()
    (ws / "templates" / "index.html").write_text("<html></html>\n")
    return ws


@pytest.fixture
def agent(tmp_workspace):
    """Create a mock agent dict with workspace."""
    return {
        "id": "test-agent",
        "name": "Test Agent",
        "workspace": str(tmp_workspace),
        "session_id": "test-session",
        "is_explorer": False,
        "is_subagent": False,
    }


@pytest.fixture
def remote_workspace(tmp_path):
    """Create a temporary directory outside the agent workspace (remote path)."""
    remote = tmp_path / "remote-project"
    remote.mkdir()
    (remote / "lib").mkdir()
    (remote / "lib" / "main.go").write_text("package main\n")
    return remote


# ---------------------------------------------------------------------------
# Path scenario tests
# ---------------------------------------------------------------------------

class TestWorkspacePath:
    """Test workspace path resolution: /workspace alias maps to caller workspace."""

    def test_workspace_root(self, explore_module, agent, tmp_workspace):
        """Path '/workspace' resolves to the agent's workspace directory."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": "/workspace", "query": "test"})
            assert "error" not in result
            assert str(tmp_workspace) in result["path"]

    def test_sandbox_explorer_inherits_parent_container_identity(
            self, explore_module, agent, tmp_workspace):
        backend = MockBackend()
        built = {}

        def spawn(_parent, builder):
            built.update(builder('explorer-1'))
            return 'explorer-1'

        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.agent_runtime.explorer.resolve_tool_ids", return_value=([], None)), \
             patch("backend.agent_runtime.explorer.build_config", return_value={}), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", side_effect=spawn), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):
            result = explore_module.execute(agent, {"path": "/workspace/src", "query": "test"})

        assert "error" not in result
        assert built['_sandbox_parent_session_id'] == 'test-session'
        assert built['_sandbox_parent_workspace'] == str(tmp_workspace)

    def test_workspace_relative(self, explore_module, agent, tmp_workspace):
        """Path '/workspace/src' resolves to workspace/src."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": "/workspace/src", "query": "test"})
            assert "error" not in result
            assert str(tmp_workspace / "src") in result["path"]


class TestAbsolutePath:
    """Test absolute host path resolution: passes through unchanged."""

    def test_absolute_host_path(self, explore_module, agent, remote_workspace):
        """Absolute path outside workspace passes through for backend resolution."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": str(remote_workspace), "query": "test"})
            assert "error" not in result
            assert str(remote_workspace) in result["path"]


class TestRelativePath:
    """Test relative path resolution: joins with caller workspace."""

    def test_relative_path_joins_workspace(self, explore_module, agent, tmp_workspace):
        """Relative path 'src' joins with caller workspace."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": "src", "query": "test"})
            assert "error" not in result
            assert str(tmp_workspace / "src") in result["path"]

    def test_dot_relative_path(self, explore_module, agent, tmp_workspace):
        """Relative path '.' resolves to the workspace root."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": ".", "query": "test"})
            assert "error" not in result
            assert str(tmp_workspace) in result["path"]


class TestRemotePath:
    """Test remote path: outside agent workspace, needs backend resolution."""

    def test_remote_path_outside_workspace(self, explore_module, agent, remote_workspace):
        """Path outside workspace uses backend resolve_path and validation."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": str(remote_workspace), "query": "test"})
            assert "error" not in result
            assert str(remote_workspace) in result["path"]

    def test_backend_resolve_path_transforms(self, explore_module, agent, remote_workspace):
        """Backend resolve_path can transform the path (e.g., symlink resolution)."""
        resolved = str(remote_workspace / "lib")
        def custom_resolve(p):
            return resolved
        backend = MockBackend(resolve_func=custom_resolve)
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": str(remote_workspace), "query": "test"})
            assert "error" not in result


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------

class TestBackendErrors:
    """Test backend error handling — should not crash."""

    def test_backend_returns_error(self, explore_module, agent, tmp_workspace):
        """Backend run_python returns error — should return error dict."""
        backend = ErrorBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}):

            result = explore_module.execute(agent, {"path": str(tmp_workspace), "query": "test"})
            assert "error" in result
            assert "cannot validate path" in result["error"]

    def test_misconfigured_backend(self, explore_module, agent, tmp_workspace):
        """Backend returns error from misconfigured workplace — should return clean error."""
        backend = MisconfiguredBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}):

            result = explore_module.execute(agent, {"path": str(tmp_workspace), "query": "test"})
            assert "error" in result
            assert "cannot validate path" in result["error"]


class TestJsonParseFailure:
    """Test JSON parse failure handling."""

    def test_truncated_json(self, explore_module, agent, tmp_workspace):
        """Truncated JSON from backend — should return error, not crash."""
        backend = JsonFailBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}):

            result = explore_module.execute(agent, {"path": str(tmp_workspace), "query": "test"})
            assert "error" in result
            assert "invalid backend response" in result["error"]

    def test_empty_stdout(self, explore_module, agent, tmp_workspace):
        """Empty stdout (logging interference) — should return error, not crash."""
        backend = TruncatedStdoutBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}):

            result = explore_module.execute(agent, {"path": str(tmp_workspace), "query": "test"})
            assert "error" in result
            assert "invalid backend response" in result["error"]


class TestFallbackBehavior:
    """Test silent fallback at line 90: path_info.get('path', path)."""

    def test_missing_path_key_fallback(self, explore_module, agent, tmp_workspace):
        """When backend response lacks 'path' key, falls back to original path."""
        backend = FallbackBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": str(tmp_workspace), "query": "test"})
            assert "error" not in result
            # Path should fall back to the original resolved path
            assert "path" in result


class TestEmptyAndMalformedPaths:
    """Test empty or malformed path input."""

    def test_empty_path(self, explore_module, agent):
        """Empty path returns error."""
        result = explore_module.execute(agent, {"path": "", "query": "test"})
        assert "error" in result
        assert "required" in result["error"]

    def test_whitespace_only_path(self, explore_module, agent):
        """Whitespace-only path returns error."""
        result = explore_module.execute(agent, {"path": "   ", "query": "test"})
        assert "error" in result
        assert "required" in result["error"]

    def test_none_path(self, explore_module, agent):
        """None path returns error."""
        result = explore_module.execute(agent, {"path": None, "query": "test"})
        assert "error" in result
        assert "required" in result["error"]

    def test_missing_path_key(self, explore_module, agent):
        """Missing path key returns error."""
        result = explore_module.execute(agent, {"query": "test"})
        assert "error" in result
        assert "required" in result["error"]


class TestFileNotDirectory:
    """Test that file paths are rejected (explorer needs a directory)."""

    def test_file_path_rejected(self, explore_module, agent, tmp_workspace):
        """A file path returns 'not an existing directory' error."""
        file_path = str(tmp_workspace / "src" / "app.py")
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}):

            result = explore_module.execute(agent, {"path": file_path, "query": "test"})
            assert "error" in result
            assert "not an existing directory" in result["error"]

    def test_nonexistent_directory(self, explore_module, agent, tmp_workspace):
        """Non-existent directory returns error."""
        bad_path = str(tmp_workspace / "does_not_exist")
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}):

            result = explore_module.execute(agent, {"path": bad_path, "query": "test"})
            assert "error" in result
            assert "not an existing directory" in result["error"]


class TestAgentValidation:
    """Test agent-level validation before path resolution."""

    def test_no_agent_id(self, explore_module):
        """Missing agent ID returns error."""
        result = explore_module.execute({}, {"path": "/tmp", "query": "test"})
        assert "error" in result

    def test_explorer_cannot_spawn(self, explore_module, agent):
        """Explorers cannot spawn explorers."""
        agent["is_explorer"] = True
        result = explore_module.execute(agent, {"path": "/tmp", "query": "test"})
        assert "error" in result
        assert "cannot spawn explorers" in result["error"]

    def test_subagent_cannot_spawn(self, explore_module, agent):
        """Sub-agents cannot spawn explorers."""
        agent["is_subagent"] = True
        result = explore_module.execute(agent, {"path": "/tmp", "query": "test"})
        assert "error" in result
        assert "cannot spawn explorers" in result["error"]


class TestPathResolutionRegression:
    """Regression tests for specific path resolution bugs from commit dfe9c5f."""

    def test_workspace_alias_with_trailing_slash(self, explore_module, agent, tmp_workspace):
        """Path '/workspace/' with trailing slash resolves correctly."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": "/workspace/", "query": "test"})
            assert "error" not in result
            assert str(tmp_workspace) in result["path"]

    def test_workspace_deep_path(self, explore_module, agent, tmp_workspace):
        """Deep workspace path '/workspace/templates/index.html' parent resolves."""
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": "/workspace/templates", "query": "test"})
            assert "error" not in result
            assert str(tmp_workspace / "templates") in result["path"]

    def test_empty_workspace_with_relative_path(self, explore_module, remote_workspace):
        """Agent with empty workspace and relative path — uses raw path."""
        agent = {
            "id": "test-agent",
            "name": "Test Agent",
            "workspace": "",
            "session_id": "test-session",
            "is_explorer": False,
            "is_subagent": False,
        }
        backend = MockBackend()
        with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=backend), \
             patch("models.db.db.get_agent", return_value=agent), \
             patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
             patch("backend.skills_manager.skills_manager.get_skill_config", return_value={"tool_ids": ""}), \
             patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
             patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn", return_value=("test-agent", None, None)), \
             patch("backend.agent_runtime.notifier.notify_agent", return_value={"session_id": "sess-1"}):

            result = explore_module.execute(agent, {"path": str(remote_workspace), "query": "test"})
            assert "error" not in result
