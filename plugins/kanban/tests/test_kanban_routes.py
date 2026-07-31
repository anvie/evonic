"""Regression tests for Kanban task-creator API routes."""

from unittest.mock import patch

from flask import Flask

from plugins.kanban import routes


class _FakeLLMClient:
    def chat_completion(self, **_kwargs):
        return {
            "success": True,
            "response": {
                "choices": [{
                    "message": {
                        "content": (
                            "---TITLE---\n"
                            "Improve task creator\n"
                            "---DESCRIPTION---\n"
                            "Make the assignee list resilient."
                        ),
                    },
                }],
            },
        }


class _FakeDB:
    def get_agents(self):
        return [
            {"id": "enabled-agent", "name": "Enabled Agent", "enabled": 1},
            {"id": "disabled-agent", "name": "Disabled Agent", "enabled": 0},
        ]


def _client():
    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    return app.test_client()


def test_enhance_accepts_response_without_end_delimiter():
    with patch("backend.llm_client.get_llm_client", return_value=_FakeLLMClient()):
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "Fix the form."},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "title": "Improve task creator",
        "description": "Make the assignee list resilient.",
    }


def test_all_agents_returns_enabled_agents_when_skill_lookup_fails():
    fake_db = _FakeDB()
    with patch("models.db.db", fake_db), patch(
        "plugins.kanban.handler._get_kanban_skill_agents",
        side_effect=RuntimeError("skill metadata is unavailable"),
    ):
        response = _client().get("/api/kanban/all-agents")

    assert response.status_code == 200
    assert response.get_json() == {
        "agents": [{
            "id": "enabled-agent",
            "name": "Enabled Agent",
            "has_kanban": False,
            "avatar_path": "",
        }],
    }
