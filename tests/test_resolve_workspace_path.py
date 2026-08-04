"""Regression tests for shared workspace path resolution."""

from backend.tools._workspace import resolve_workspace_path


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
