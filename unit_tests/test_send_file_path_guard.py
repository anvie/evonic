"""Regression tests for core send_file policy and extension hooks."""

from backend.plugin_hooks import (
    _attachment_policies,
    check_attachment_policies,
    register_attachment_policy,
    unregister_attachment_policy,
)
from backend.tools.send_file import (
    _check_path_policy, _check_self_request_policy, execute,
)


def test_empty_regex_preserves_existing_behavior():
    assert _check_path_policy({}, "/internal/secret.txt") is None


def test_matching_canonical_path_is_allowed():
    agent = {"send_file_allowed_path_regex": r"^/workspace/artifacts/"}
    assert _check_path_policy(agent, "/workspace/artifacts/report.pdf") is None


def test_non_matching_path_is_rejected_without_path_disclosure():
    agent = {"send_file_allowed_path_regex": r"^/workspace/artifacts/"}
    result = _check_path_policy(agent, "/agents/agent/SYSTEM.md")
    assert result == {"error": "File attachment is not permitted by the configured policy."}
    assert "SYSTEM" not in result["error"]


def test_invalid_regex_fails_closed_when_policy_is_enabled():
    assert _check_path_policy({"send_file_allowed_path_regex": "["}, "/tmp/report.pdf")


def test_self_request_is_rejected_when_policy_denies_virtual_prefix():
    agent = {"send_file_allowed_path_regex": r"^(?!/_self).*$"}
    result = _check_self_request_policy(agent, "/_self/SYSTEM.md")
    assert result == {"error": "File attachment is not permitted by the configured policy."}


def test_self_request_is_allowed_when_policy_accepts_virtual_prefix():
    agent = {"send_file_allowed_path_regex": r"^/_self/artifacts/"}
    assert _check_self_request_policy(agent, "/_self/artifacts/report.pdf") is None


def test_canonical_only_policy_is_not_applied_to_virtual_request():
    agent = {"send_file_allowed_path_regex": r"^/agents/agent/artifacts/"}
    assert _check_self_request_policy(agent, "/_self/artifacts/report.pdf") is None


def test_empty_policy_preserves_self_request_behavior():
    assert _check_self_request_policy({}, "/_self/SYSTEM.md") is None


def test_self_request_policy_invalid_regex_fails_closed():
    agent = {"send_file_allowed_path_regex": r"/_self["}
    assert _check_self_request_policy(agent, "/_self/report.pdf")


def test_execute_rejects_denied_self_request_before_resolution(monkeypatch):
    agent = {
        "id": "agent",
        "session_id": "session",
        "send_file_allowed_path_regex": r"^(?!/_self).*$",
    }

    def fail_resolution(*args, **kwargs):
        raise AssertionError("denied virtual path must not be resolved")

    monkeypatch.setattr("backend.tools._workspace.resolve_self_path", fail_resolution)

    result = execute(agent, {"file_path": "/_self/SYSTEM.md"})

    assert result == {
        "error": "File attachment is not permitted by the configured policy."
    }


def test_traversal_is_checked_after_canonicalization(tmp_path):
    allowed = tmp_path / "artifacts"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    agent = {"send_file_allowed_path_regex": rf"^{allowed / ''}"}
    assert _check_path_policy(agent, str(secret.resolve()))


def test_symlink_alias_is_checked_as_resolved_path(tmp_path):
    allowed = tmp_path / "artifacts"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    alias = allowed / "alias.txt"
    alias.symlink_to(secret)
    agent = {"send_file_allowed_path_regex": rf"^{allowed / ''}"}
    assert _check_path_policy(agent, str(alias.resolve()))


def test_generic_attachment_policy_hook_can_reject():
    def reject(agent, canonical_path):
        return {"error": "blocked"} if canonical_path.endswith(".secret") else None

    register_attachment_policy(reject)
    try:
        assert check_attachment_policies({}, "/tmp/value.secret") == {"error": "blocked"}
        assert _check_path_policy({}, "/tmp/value.secret") == {"error": "blocked"}
    finally:
        unregister_attachment_policy(reject)
        assert reject not in _attachment_policies
