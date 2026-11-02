"""Focused regression coverage for built-in slash commands."""

import json
from unittest.mock import patch

from backend.slash_commands import COMMAND_SUPPRESSED, execute_command


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


def test_exec_preserves_cmp_and_unrelated_session_state():
    from backend.agent_state import AgentState

    state = AgentState(mode="plan")
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
    assert saved_state["cmp"] == state.cmp
    assert saved_state["workspace_marker"] == "preserve"


def test_plan_preserves_cmp_and_session_data_while_resetting_only_global_focus():
    from backend.agent_state import AgentState

    state = AgentState(mode="execute", plan_file="plan/current.md")
    state.cmp = {"version": 1, "active_id": "A1", "paths": {"A1": {}}}
    session_state = json.loads(state.serialize())
    session_state["workspace_marker"] = "preserve"
    global_state = {
        "focus": True,
        "focus_reason": "exclusive task",
        "active_fallback_model_id": "fallback-model",
    }
    with patch("models.chat.agent_chat_manager.get") as get_chat:
        chat_db = get_chat.return_value
        chat_db.get_session_state.return_value = json.dumps(session_state)
        chat_db.get_agent_state.return_value = json.dumps(global_state)

        response = execute_command("plan", "", "session-123", "agent-123", "user-123")

    assert response == "Switched to plan mode."
    saved_session = json.loads(chat_db.upsert_session_state.call_args.args[1])
    assert saved_session["mode"] == "plan"
    assert saved_session["plan_file"] == "plan/current.md"
    assert saved_session["cmp"] == state.cmp
    assert saved_session["workspace_marker"] == "preserve"

    saved_global = json.loads(chat_db.upsert_agent_state.call_args.args[0])
    assert saved_global["focus"] is False
    assert saved_global["focus_reason"] is None
    assert saved_global["active_fallback_model_id"] == "fallback-model"


def test_agent_mode_transition_still_requires_plan_file():
    from backend.agent_state import AgentState

    state = AgentState(mode="plan")
    result = state.set_mode("execute")

    assert "error" in result
    assert state.mode == "plan"


def test_goal_persists_goal_state_and_returns_system_event():
    """`/goal` stores a structured goal and yields an agent-visible event.

    The handler must never return the raw ``/goal ...`` literal (which would be
    saved as the assistant reply), and the stored state must carry a bounded
    title/summary rather than only the unbounded user text.
    """
    from backend.agent_state import AgentState

    session_state = json.loads(AgentState(mode="plan").serialize())
    session_state["workspace_marker"] = "preserve"
    with patch("models.db.db.get_agent", return_value={"enable_agent_state": True}), \
         patch("models.chat.agent_chat_manager.get") as get_chat:
        chat_db = get_chat.return_value
        chat_db.get_session_state.return_value = json.dumps(session_state)

        response = execute_command(
            "goal", "Deliver the requested report", "session-123", "agent-123", "user-123"
        )

    assert isinstance(response, str)
    assert response.startswith("[SYSTEM] Active goal updated.")
    assert "/goal" not in response
    saved_state = json.loads(chat_db.upsert_session_state.call_args.args[1])
    assert saved_state["mode"] == "execute"
    assert saved_state["workspace_marker"] == "preserve"
    goal = saved_state["active_goal"]
    assert goal["title"] == "Deliver the requested report"
    assert goal["summary"] == "Deliver the requested report"
    assert goal["raw_instruction"] == "Deliver the requested report"
    assert goal["nudges_used"] == 0
    assert goal["last_nudge"] is None


def test_goal_title_is_bounded_for_long_instructions():
    from backend.active_goal import (
        MAX_GOAL_SUMMARY_CHARS, MAX_GOAL_TITLE_CHARS, create_active_goal,
    )

    instruction = "Laporan mingguan " + ("sangat detail " * 200)
    goal = create_active_goal(instruction)

    assert len(goal["title"]) <= MAX_GOAL_TITLE_CHARS
    assert len(goal["summary"]) <= MAX_GOAL_SUMMARY_CHARS
    assert goal["raw_instruction"] == instruction.strip()


def test_goal_event_never_exposes_slash_command_literal():
    from backend.active_goal import create_active_goal, render_active_goal_event

    event = render_active_goal_event(create_active_goal("Summarise the session within 2000 characters"))

    assert event.startswith("[SYSTEM] Active goal updated.")
    assert "/goal" not in event
    assert "2000 characters" in event


def test_legacy_goal_text_is_upgraded_to_title_and_summary():
    from backend.active_goal import normalize_active_goal

    normalized = normalize_active_goal({"text": "Summarise this session", "nudges_used": 2})

    assert normalized is not None
    assert normalized["title"] == "Summarise this session"
    assert normalized["summary"] == "Summarise this session"
    assert normalized["raw_instruction"] == "Summarise this session"
    assert normalized["nudges_used"] == 2


def test_goal_requires_nonempty_query():
    response = execute_command("goal", "   ", "session-123", "agent-123", "user-123")

    assert response == "Usage: /goal <what the agent should complete>"


# ==================== /help suppression (help_enabled) ====================


def test_help_suppressed_when_help_enabled_off():
    """/help returns COMMAND_SUPPRESSED when the agent has help_enabled=0."""
    with patch("models.db.db.get_agent", return_value={"help_enabled": 0}):
        response = execute_command("help", "", "session-123", "agent-123", "user-123")

    assert response is COMMAND_SUPPRESSED


def test_help_responds_when_help_enabled_on():
    """/help returns the command list when help_enabled=1 (default behavior)."""
    with patch("models.db.db.get_agent", return_value={"help_enabled": 1}), \
         patch("models.db.db.get_super_agent", return_value=None), \
         patch("models.db.db.get_agent_skills", return_value=[]):
        response = execute_command("help", "", "session-123", "agent-123", "user-123")

    assert response is not COMMAND_SUPPRESSED
    assert response.startswith("**Available commands:**")


def test_help_responds_when_help_enabled_missing():
    """Missing help_enabled defaults to enabled for backward compatibility."""
    with patch("models.db.db.get_agent", return_value={}), \
         patch("models.db.db.get_super_agent", return_value=None), \
         patch("models.db.db.get_agent_skills", return_value=[]):
        response = execute_command("help", "", "session-123", "agent-123", "user-123")

    assert response is not COMMAND_SUPPRESSED
    assert response.startswith("**Available commands:**")


def test_list_available_commands_omits_help_when_disabled():
    """help_enabled=0 removes /help from list_available_commands()."""
    from backend.slash_commands import list_available_commands

    with patch("models.db.db.get_agent", return_value={
        "help_enabled": 0, "disabled_slash_commands": "", "workplace_id": None,
        "is_super": False,
    }), \
         patch("models.db.db.get_super_agent", return_value=None), \
         patch("models.db.db.get_agent_skills", return_value=[]):
        names = {cmd.name for cmd in list_available_commands("agent-123")}

    assert "help" not in names


def test_list_available_commands_includes_help_when_enabled():
    """help_enabled=1 keeps /help in list_available_commands()."""
    from backend.slash_commands import list_available_commands

    with patch("models.db.db.get_agent", return_value={
        "help_enabled": 1, "disabled_slash_commands": "", "workplace_id": None,
        "is_super": False,
    }), \
         patch("models.db.db.get_super_agent", return_value=None), \
         patch("models.db.db.get_agent_skills", return_value=[]):
        names = {cmd.name for cmd in list_available_commands("agent-123")}

    assert "help" in names
