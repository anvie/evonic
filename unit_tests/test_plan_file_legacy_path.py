"""The legacy plan fallback may only reach files under <project root>/plan/."""
import os

import pytest

from backend import agent_state
from backend.agent_state import legacy_plan_path


@pytest.mark.parametrize("plan_file", [
    ".env", "backend/agent_state.py", "../evonic-x/plan/a.md", "plan/../.env",
    "plan", "/etc/passwd", "plans/a.md",
])
def test_rejects_paths_outside_plan_dir(plan_file):
    assert legacy_plan_path(plan_file) is None


def test_accepts_plan_dir_files():
    path = legacy_plan_path("plan/my-plan.md")
    assert path == os.path.join(agent_state._PROJECT_ROOT, "plan", "my-plan.md")
    assert legacy_plan_path("plan/sub/x.md")


def test_symlink_escape_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "link").symlink_to(tmp_path.parent)
    monkeypatch.setattr(agent_state, "_PROJECT_ROOT", str(tmp_path))
    assert legacy_plan_path("plan/link/x.md") is None
