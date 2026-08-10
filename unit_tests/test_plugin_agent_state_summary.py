from backend.plugin_hooks import (
    get_agent_state_summaries,
    register_agent_state_summary_provider,
    unregister_agent_state_summary_provider,
)


def test_plugin_agent_state_summary_is_display_only_and_error_isolated():
    register_agent_state_summary_provider(
        "example", lambda agent_id, session_id: {
            "state": "active",
            "data": {"agent_id": agent_id, "session_id": session_id},
        },
    )
    register_agent_state_summary_provider(
        "broken", lambda _agent_id, _session_id: 1 / 0,
    )
    try:
        assert get_agent_state_summaries("agent-1", "session-1") == {
            "example": {
                "state": "active",
                "data": {"agent_id": "agent-1", "session_id": "session-1"},
            }
        }
    finally:
        unregister_agent_state_summary_provider("example")
        unregister_agent_state_summary_provider("broken")


def test_chat_state_api_includes_plugin_summary():
    from app import app

    register_agent_state_summary_provider(
        "example", lambda _agent_id, _session_id: {
            "state": "active", "data": {"source": "plugin"},
        },
    )
    try:
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["authenticated"] = True
            response = client.get("/api/agents/plugin-state-test/chat/state")
        assert response.status_code == 200
        assert response.get_json()["states"]["example"] == {
            "state": "active", "data": {"source": "plugin"},
        }
    finally:
        unregister_agent_state_summary_provider("example")
