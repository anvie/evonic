"""
Regression tests for /stop propagation out of a blocking sync Explore().

Before the fix, sync Explore blocked on a single ``done.wait(timeout)`` that only
watched the explorer's ``final_answer`` event — a /stop on the CALLER's session
went unnoticed until the explorer timeout (default 300s) expired, and the
explorer itself (a different session id) kept running.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPLORE_PATH = ROOT / "skills" / "explorer" / "backend" / "tools" / "Explore.py"

PARENT_SESSION = "parent-session"
EXPLORER_SESSION = "explorer-session"


def _load_explore():
    mod_name = "test_explore_stop_propagation_explore"
    spec = __import__("importlib.util").util.spec_from_file_location(mod_name, str(EXPLORE_PATH))
    module = __import__("importlib.util").util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class MockBackend:
    """Resolves paths as-is and reports the target as an existing directory."""

    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        import json
        import re
        m = re.search(r"p=(['\"])(.*?)\1", code)
        path_str = m.group(2) if m else ""
        return {"stdout": json.dumps({"path": path_str, "is_dir": True}),
                "stderr": "", "exit_code": 0}


@pytest.fixture
def explore_module():
    return _load_explore()


@pytest.fixture
def agent(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return {
        "id": "test-agent",
        "name": "Test Agent",
        "workspace": str(ws),
        "session_id": PARENT_SESSION,
        "is_explorer": False,
        "is_subagent": False,
    }


def _sync_explore(explore_module, agent, stop_after: float, stop_calls: list):
    """Run a sync Explore() with /stop landing on the parent after `stop_after`s."""
    stopped_at = time.monotonic() + stop_after

    def fake_is_stop_requested(session_id):
        return session_id == PARENT_SESSION and time.monotonic() >= stopped_at

    with patch("backend.tools.lib.exec_backend.registry.get_backend", return_value=MockBackend()), \
         patch("models.db.db.get_agent", return_value=agent), \
         patch("backend.agent_runtime.explorer.worker_skill_enabled", return_value=True), \
         patch("backend.skills_manager.skills_manager.get_skill_config",
               return_value={"tool_ids": "", "sync": True, "timeout": 300}), \
         patch("backend.subagent_manager.subagent_manager.spawn_explorer", return_value="explorer-1"), \
         patch("backend.agent_report_to.resolve_report_to_for_subagent_spawn",
               return_value=("test-agent", None, None)), \
         patch("backend.agent_runtime.notifier.notify_agent",
               return_value={"success": True, "session_id": EXPLORER_SESSION}), \
         patch("backend.agent_runtime.agent_runtime.is_stop_requested",
               side_effect=fake_is_stop_requested), \
         patch("backend.agent_runtime.agent_runtime.request_stop",
               side_effect=stop_calls.append):

        started = time.monotonic()
        result = explore_module.execute(
            agent, {"path": str(agent["workspace"]), "query": "who calls foo()?"})
        return result, time.monotonic() - started


class TestSyncStopPropagation:

    def test_stop_returns_immediately_instead_of_waiting_for_timeout(
            self, explore_module, agent):
        """/stop on the caller ends the blocking wait in well under the 300s timeout."""
        stop_calls = []
        result, elapsed = _sync_explore(explore_module, agent, 0.3, stop_calls)

        assert result.get("stopped") is True
        assert "stopped by user" in result.get("error", "").lower()
        assert "findings" not in result
        assert elapsed < 5, f"sync Explore took {elapsed:.1f}s to honour /stop"

    def test_stop_is_propagated_to_the_explorer_session(self, explore_module, agent):
        """The explorer's OWN session is stopped — not just the caller's."""
        stop_calls = []
        _sync_explore(explore_module, agent, 0.3, stop_calls)

        assert stop_calls == [EXPLORER_SESSION]

    def test_findings_returned_when_no_stop(self, explore_module, agent):
        """Without a /stop, a finishing explorer still returns its findings."""
        from backend.event_stream import event_stream

        stop_calls = []

        def _finish():
            time.sleep(0.3)
            event_stream.emit('final_answer', {
                'agent_id': 'explorer-1',
                'answer': 'foo() is called from bar.py',
                'tool_trace': [],
            })

        finisher = threading.Timer(0, _finish)
        finisher.start()
        try:
            # stop_after far beyond the explorer's completion — never fires
            result, elapsed = _sync_explore(explore_module, agent, 60, stop_calls)
        finally:
            finisher.join(timeout=5)

        assert result.get("findings") == 'foo() is called from bar.py'
        assert result.get("stopped") is None
        assert stop_calls == []
        assert elapsed < 5


class TestWaitHelper:

    def test_timeout_outcome(self, explore_module):
        with patch("backend.agent_runtime.agent_runtime.is_stop_requested",
                   return_value=False):
            outcome = explore_module._wait_for_explorer(
                threading.Event(), 0, PARENT_SESSION)
        assert outcome == 'timeout'

    def test_done_wins_over_stop(self, explore_module):
        """An explorer that already finished reports 'done', not 'stopped'."""
        done = threading.Event()
        done.set()
        with patch("backend.agent_runtime.agent_runtime.is_stop_requested",
                   return_value=True):
            outcome = explore_module._wait_for_explorer(done, 300, PARENT_SESSION)
        assert outcome == 'done'
