"""Regression tests for the opt-in per-agent send_file path guard."""

from unittest.mock import patch

from backend.tools.send_file import _check_path_policy


@patch("backend.plugin_manager.plugin_manager")
def test_empty_regex_preserves_existing_behavior(manager):
    manager.get_agent_plugin_settings.return_value = {"allowed_path_regex": ""}
    assert _check_path_policy("agent", "/internal/secret.txt") is None


@patch("backend.plugin_manager.plugin_manager")
def test_matching_canonical_path_is_allowed(manager):
    manager.get_agent_plugin_settings.return_value = {
        "allowed_path_regex": r"^/workspace/artifacts/"
    }
    assert _check_path_policy("agent", "/workspace/artifacts/report.pdf") is None


@patch("backend.plugin_manager.plugin_manager")
def test_non_matching_path_is_rejected_without_path_disclosure(manager):
    manager.get_agent_plugin_settings.return_value = {
        "allowed_path_regex": r"^/workspace/artifacts/"
    }
    result = _check_path_policy("agent", "/agents/agent/SYSTEM.md")
    assert result == {
        "error": "File attachment is not permitted by the configured policy."
    }
    assert "SYSTEM" not in result["error"]


@patch("backend.plugin_manager.plugin_manager")
def test_invalid_regex_fails_closed_when_guard_is_enabled(manager):
    manager.get_agent_plugin_settings.return_value = {
        "allowed_path_regex": "["
    }
    assert _check_path_policy("agent", "/workspace/artifacts/report.pdf")


@patch("backend.plugin_manager.plugin_manager")
def test_traversal_is_checked_after_canonicalization(manager, tmp_path):
    allowed = tmp_path / "artifacts"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    manager.get_agent_plugin_settings.return_value = {
        "allowed_path_regex": rf"^{allowed / ''}"
    }
    # The caller must provide the resolved path; traversal cannot match the allow-list.
    assert _check_path_policy("agent", str(secret.resolve()))


@patch("backend.plugin_manager.plugin_manager")
def test_symlink_alias_is_checked_as_resolved_path(manager, tmp_path):
    allowed = tmp_path / "artifacts"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    alias = allowed / "alias.txt"
    alias.symlink_to(secret)
    manager.get_agent_plugin_settings.return_value = {
        "allowed_path_regex": rf"^{allowed / ''}"
    }
    assert _check_path_policy("agent", str(alias.resolve()))
