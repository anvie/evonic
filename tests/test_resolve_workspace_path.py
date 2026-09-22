"""Regression tests for shared workspace path resolution."""

from backend.tools._workspace import resolve_workspace_path, scratch_dir


def test_scratch_dir_uses_direct_tmp_path():
    assert scratch_dir("linus") == "/tmp/evonic-linus-scratchpad"


def test_scratch_dir_uses_default_identifier_when_empty():
    assert scratch_dir("") == "/tmp/evonic-default-scratchpad"


def test_subagent_relative_path_uses_canonical_scratchpad():
    agent = {"id": "linus", "is_subagent": True}
    assert resolve_workspace_path(agent, "work.py", "/workspace") == "/tmp/evonic-linus-scratchpad/work.py"


def test_preserves_absolute_path_already_inside_agent_workspace():
    agent = {"workspace": "/workspace/backend"}
    file_path = "/workspace/backend/channels/example.py"

    assert resolve_workspace_path(agent, file_path, "/workspace") == file_path


def test_rebases_virtual_workspace_path_to_agent_workspace():
    agent = {"workspace": "/srv/project"}

    assert (
        resolve_workspace_path(agent, "/workspace/channels/example.py", "/workspace")
        == "/srv/project/channels/example.py"
    )


def test_rebases_virtual_workspace_path_to_fallback_workspace():
    assert (
        resolve_workspace_path(None, "/workspace/channels/example.py", "/srv/project")
        == "/srv/project/channels/example.py"
    )


def test_virtual_workspace_root_resolves_to_agent_workspace():
    agent = {"workspace": "/srv/project"}

    assert resolve_workspace_path(agent, "/workspace", "/workspace") == "/srv/project"


def test_similar_absolute_prefix_is_not_treated_as_virtual_workspace():
    agent = {"workspace": "/srv/project"}
    file_path = "/workspace2/channels/example.py"

    assert resolve_workspace_path(agent, file_path, "/workspace") == file_path


def test_virtual_workspace_traversal_does_not_escape_agent_workspace():
    agent = {"workspace": "/srv/project"}
    file_path = "/workspace/../outside.txt"

    assert resolve_workspace_path(agent, file_path, "/workspace") == file_path
