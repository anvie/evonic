"""Tests for evomem_client.py -- CLI subprocess wrapper."""

import os
import subprocess
import pytest
from unittest.mock import patch, MagicMock

from backend.agent_runtime.evomem_client import (
    get_engine, is_available, _run,
    init_evomem, capture, search, think, sync, _get_evomem_dir,
)


class TestGetEngine:
    def test_default_is_evomem_when_available(self, monkeypatch):
        monkeypatch.delenv("EVONIC_MEMORY_ENGINE", raising=False)
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        assert get_engine() == "evomem"

    def test_default_downgrades_to_fts5_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("EVONIC_MEMORY_ENGINE", raising=False)
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: False)
        assert get_engine() == "fts5"

    def test_evomem_when_set(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        assert get_engine() == "evomem"

    def test_fts5_when_set(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        assert get_engine() == "fts5"

    def test_invalid_defaults_to_evomem_when_available(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "bogus")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        assert get_engine() == "evomem"


class TestIsAvailable:
    def test_returns_false_when_binary_missing(self, monkeypatch):
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client._EVOMEM_BINARY",
            "/nonexistent/evomem"
        )
        assert is_available() is False

    def test_returns_true_when_binary_exists(self, tmp_path):
        bin_path = tmp_path / "evomem"
        bin_path.write_text("fake binary")
        bin_path.chmod(0o755)
        import backend.agent_runtime.evomem_client as ec
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(ec, "_EVOMEM_BINARY", str(bin_path))
        try:
            assert ec.is_available() is True
        finally:
            monkeypatch.undo()


class TestEvomemDir:
    def test_returns_agents_kb_path(self):
        path = _get_evomem_dir("test-agent")
        assert path == "agents/test-agent/kb"


class TestRunSubprocess:
    def test_returns_parsed_json_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"ok": true}'
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _run("/tmp/brain", ["search", "query"])
            assert result == {"ok": True}

    def test_returns_none_on_nonzero_exit(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error occurred"
        with patch("subprocess.run", return_value=mock_result):
            result = _run("/tmp/brain", ["search", "query"])
            assert result is None

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            result = _run("/tmp/brain", ["search", "query"])
            assert result is None

    def test_returns_none_on_invalid_json(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json at all"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _run("/tmp/brain", ["search", "query"])
            assert result is None

    def test_returns_none_on_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = _run("/tmp/brain", ["search", "query"])
            assert result is None


class TestInitBrain:
    def test_returns_false_when_unavailable(self):
        with patch("backend.agent_runtime.evomem_client.is_available", return_value=False), \
             patch("os.makedirs"):
            result = init_evomem("test-agent")
            assert result is False

    def test_returns_true_when_already_initialized(self, tmp_path):
        brain_dir = tmp_path / "brain"
        brain_dir.mkdir()
        (brain_dir / ".evomem.db").write_text("fake db")
        with patch(
            "backend.agent_runtime.evomem_client._get_evomem_dir",
            return_value=str(brain_dir)
        ), patch("backend.agent_runtime.evomem_client.is_available", return_value=True):
            result = init_evomem("test-agent")
            assert result is True

    def test_succeeds_when_db_created_despite_nonjson_init_output(self, tmp_path):
        # `init` prints plain text even with --json (so _run returns None); success
        # must be judged by the db file appearing, not by a parsed result.
        brain_dir = tmp_path / "brain"

        def fake_run(bd, args, timeout=None, expect_json=True):
            os.makedirs(bd, exist_ok=True)
            open(os.path.join(bd, ".evomem.db"), "w").close()
            return None  # non-JSON init output

        with patch("backend.agent_runtime.evomem_client._get_evomem_dir",
                   return_value=str(brain_dir)), \
             patch("backend.agent_runtime.evomem_client.is_available", return_value=True), \
             patch("backend.agent_runtime.evomem_client._run", side_effect=fake_run):
            assert init_evomem("test-agent") is True


class TestCapture:
    def test_returns_none_when_brain_not_initialized(self):
        with patch("backend.agent_runtime.evomem_client.init_evomem", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = capture("test-agent", "test fact")
            assert result is None


class TestSearch:
    def test_returns_none_when_brain_missing(self):
        with patch("os.path.isdir", return_value=False):
            result = search("test-agent", "query")
            assert result is None

    def test_returns_structured_result(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch(
                 "backend.agent_runtime.evomem_client._run",
                 return_value={"query": "test", "hits": [], "cached": False}
             ):
            result = search("test-agent", "query")
            assert result is not None
            assert result["query"] == "test"
            assert result["hits"] == []


class TestThink:
    def test_returns_none_when_brain_missing(self):
        with patch("os.path.isdir", return_value=False):
            result = think("test-agent", "query")
            assert result is None


class TestSync:
    def test_returns_false_when_brain_missing(self):
        # Brain dir missing and init fails → sync returns False (no binary dance).
        with patch("os.path.isdir", return_value=False), \
             patch("backend.agent_runtime.evomem_client.init_evomem", return_value=False):
            result = sync("test-agent")
            assert result is False


class TestGraphQuery:
    def test_returns_none_when_brain_missing(self):
        from backend.agent_runtime.evomem_client import graph_query
        with patch("os.path.isdir", return_value=False):
            assert graph_query("test-agent", "entities/x") is None

    def test_builds_command_with_hops_and_edge(self):
        from backend.agent_runtime.evomem_client import graph_query
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("backend.agent_runtime.evomem_client._run",
                   return_value={"start": "entities/x", "edges": []}) as mock_run:
            graph_query("test-agent", "entities/x", edge="works_at", hops=3)
            args = mock_run.call_args[0][1]
            assert "graph-query" in args and "entities/x" in args
            assert "--hops" in args and "3" in args
            assert "--edge" in args and "works_at" in args


class TestLeadingDashQuery:
    """A query that starts with "-" must reach evomem as a value, not be read as a flag."""

    def _args(self, fn, *a, **kw):
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("backend.agent_runtime.evomem_client._run", return_value={}) as mock_run:
            fn("test-agent", *a, **kw)
            return mock_run.call_args[0][1]

    def test_search_puts_the_query_after_a_separator(self):
        args = self._args(search, "- update the server")
        assert args[-2:] == ["--", "- update the server"]

    def test_think_puts_the_query_after_a_separator(self):
        from backend.agent_runtime.evomem_client import think
        assert self._args(think, "-x question")[-2:] == ["--", "-x question"]

    def test_graph_query_puts_the_start_after_a_separator(self):
        from backend.agent_runtime.evomem_client import graph_query
        args = self._args(graph_query, "-start", edge="works_at")
        assert args[-2:] == ["--", "-start"] and args.index("--edge") < args.index("--")


class TestModeThreading:
    def test_search_threads_mode(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("backend.agent_runtime.evomem_client._run",
                   return_value={"hits": []}) as mock_run:
            search("test-agent", "q", limit=5, mode="tokenmax")
            args = mock_run.call_args[0][1]
            assert "--mode" in args and "tokenmax" in args

    def test_think_threads_mode(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("backend.agent_runtime.evomem_client._run",
                   return_value={"facts": []}) as mock_run:
            think("test-agent", "q", mode="conservative")
            args = mock_run.call_args[0][1]
            assert "--mode" in args and "conservative" in args
