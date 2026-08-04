"""Focused regression coverage for built-in slash commands."""

import json
from unittest.mock import patch

from backend.slash_commands import execute_command


def _execute_clear(args: str):
    """Execute /clear without touching persistent session or log state."""
    with patch("models.db.db.clear_session") as clear_session, \
         patch("models.db.db.upsert_session_state"), \
         patch("models.db.db.upsert_agent_state"), \
         patch("backend.slash_commands.os.path.exists", return_value=False), \
         patch("config.SESSION_ARCHIVE", True):
        response = execute_command("clear", args, "session-123", "agent-123", "user-123")

    return response, clear_session


def test_clear_does_not_archive_by_default():
    response, clear_session = _execute_clear("")

    assert response == "History cleared without archive"
    clear_session.assert_called_once_with("session-123", "agent-123", no_archive=True)


def test_clear_ar_archives_the_session():
    response, clear_session = _execute_clear("ar")

    assert response == "History cleared."
    clear_session.assert_called_once_with("session-123", "agent-123", no_archive=False)


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


def test_exec_bypasses_plan_file_requirement_for_explicit_user_command():
    from backend.agent_state import AgentState

    state = AgentState(mode="plan")
    with patch("models.db.db.get_agent", return_value={"enable_agent_state": True}), \
         patch("models.chat.agent_chat_manager.get") as get_chat:
        chat_db = get_chat.return_value
        chat_db.get_session_state.return_value = state.serialize()

        response = execute_command("exec", "", "session-123", "agent-123", "user-123")

    assert response == "Switched to execute mode."
    saved_state = chat_db.upsert_session_state.call_args.args[1]
    assert '"mode": "execute"' in saved_state


def test_exec_bypasses_plan_file_requirement_for_fresh_session():
    with patch("models.db.db.get_agent", return_value={"enable_agent_state": True}), \
         patch("models.chat.agent_chat_manager.get") as get_chat:
        chat_db = get_chat.return_value
        chat_db.get_session_state.return_value = None

        response = execute_command("exec", "", "session-123", "agent-123", "user-123")

    assert response == "Switched to execute mode."
    saved_state = chat_db.upsert_session_state.call_args.args[1]
    assert '"mode": "execute"' in saved_state


def test_exec_preserves_atg_cmp_and_unrelated_session_state():
    from backend.agent_state import AgentState

    state = AgentState(mode="plan")
    state.atg = {"status": "executing", "dag": {"nodes": {}}}
    state.cmp = {"version": 1, "paths": {}}
    session_state = json.loads(state.serialize())
    session_state["workspace_marker"] = "preserve"
    with patch("models.db.db.get_agent", return_value={"enable_agent_state": True}), \
         patch("models.chat.agent_chat_manager.get") as get_chat:
        chat_db = get_chat.return_value
        chat_db.get_session_state.return_value = json.dumps(session_state)

        response = execute_command("exec", "", "session-123", "agent-123", "user-123")

    assert response == "Switched to execute mode."
    saved_state = json.loads(chat_db.upsert_session_state.call_args.args[1])
    assert saved_state["atg"] == state.atg
    assert saved_state["cmp"] == state.cmp
    assert saved_state["workspace_marker"] == "preserve"


def test_agent_mode_transition_still_requires_plan_file():
    from backend.agent_state import AgentState

    state = AgentState(mode="plan")
    result = state.set_mode("execute")

    assert "error" in result
    assert state.mode == "plan"
