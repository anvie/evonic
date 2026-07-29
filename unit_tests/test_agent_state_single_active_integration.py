"""Integration coverage for single-active task state shown in Session State."""

from pathlib import Path

from backend.agent_runtime.llm_loop import _persist_agent_state_split
from backend.agent_state import AgentState


ROOT = Path(__file__).resolve().parents[1]


def test_chat_state_api_exposes_at_most_one_active_task():
    """Persisted task transitions reach the Session State API unchanged and valid."""
    from app import app
    from models.db import db

    agent_id = "single_active_api_agent"
    session_id = "single-active-session"
    db.create_agent({"id": agent_id, "name": "Single Active", "system_prompt": ""})

    state = AgentState(mode="execute")
    state.update_tasks("set", tasks=["First", "Second", "Third"])
    # Mirrors parallel tool calls collected together and then executed serially.
    for task_id in (1, 2, 3):
        state.update_tasks("in_progress", task_id=task_id)
    _persist_agent_state_split(state, agent_id, session_id)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["authenticated"] = True
        response = client.get(
            f"/api/agents/{agent_id}/chat/state?session_id={session_id}"
        )

    assert response.status_code == 200
    tasks = response.get_json()["tasks"]
    assert [task["id"] for task in tasks if task["status"] == "in_progress"] == [3]


def test_session_state_frontend_renders_statuses_from_api_payload():
    """The Session State renderer must not manufacture or hide active statuses."""
    source = (ROOT / "templates/sessions.html").read_text(encoding="utf-8")

    assert "for (const t of stateData.tasks)" in source
    assert "const active = t.status === 'in_progress';" in source
    assert "const icon = active ? spinnerSvg : (icons[t.status] || '\\u2610');" in source
