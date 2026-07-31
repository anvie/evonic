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


def _failure(error_type="api_error"):
    return {"success": False, "error_type": error_type, "error_detail": "provider failed"}


def test_enhance_retries_with_global_fallback_after_primary_failure():
    fallback_result = _FakeLLMClient()
    primary = type("Primary", (), {"chat_completion": lambda self, **kwargs: _failure()})()
    with patch("backend.llm_client.get_llm_client", return_value=primary), patch(
        "backend.llm_client.LLMClient", return_value=fallback_result
    ) as fallback_client, patch("models.db.db.get_setting", return_value="fallback-id"), patch(
        "models.db.db.get_model_by_id", return_value={"id": "fallback-id", "enabled": 1}
    ):
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "Use fallback."},
        )

    assert response.status_code == 200
    assert fallback_client.call_count == 1


def test_enhance_does_not_retry_after_primary_success():
    primary = _FakeLLMClient()
    with patch("backend.llm_client.get_llm_client", return_value=primary), patch(
        "backend.llm_client.LLMClient"
    ) as fallback_client:
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "Primary works."},
        )

    assert response.status_code == 200
    fallback_client.assert_not_called()


def test_enhance_does_not_retry_malformed_success():
    malformed = type("Malformed", (), {"chat_completion": lambda self, **kwargs: {
        "success": True, "response": {"choices": []}
    }})()
    with patch("backend.llm_client.get_llm_client", return_value=malformed), patch(
        "backend.llm_client.LLMClient"
    ) as fallback_client:
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "Malformed response."},
        )

    assert response.status_code == 500
    assert response.get_json()["error"] == "LLM returned no choices"
    fallback_client.assert_not_called()


def test_enhance_does_not_retry_without_enabled_fallback():
    primary = type("Primary", (), {"chat_completion": lambda self, **kwargs: _failure()})()
    with patch("backend.llm_client.get_llm_client", return_value=primary), patch(
        "models.db.db.get_setting", return_value=""
    ), patch("backend.llm_client.LLMClient") as fallback_client:
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "No fallback."},
        )

    assert response.status_code == 500
    assert response.get_json()["error"] == "LLM API error"
    fallback_client.assert_not_called()
