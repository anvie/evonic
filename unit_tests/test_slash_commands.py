"""Focused regression coverage for built-in slash commands."""

from unittest.mock import patch

from backend.slash_commands import execute_command


def test_investigate_rejects_current_agent_before_database_lookup():
    with patch(
        "models.db.db.get_agent",
        side_effect=AssertionError("self-investigation must not query the database"),
    ):
        response = execute_command(
            "investigate",
            "CURRENT-AGENT inspect this session",
            "session-123",
            "current-agent",
            "user-123",
        )

    assert response == "Cannot investigate the current agent. Choose a different agent."
