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


def _scripted_client(replies):
    """Fake LLM client returning one canned reply per call (last reply repeats)."""
    class _ScriptedLLMClient:
        def __init__(self):
            self.calls = []

        def chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            idx = min(len(self.calls) - 1, len(replies) - 1)
            return {
                "success": True,
                "response": {"choices": [{"message": {"content": replies[idx]}}]},
            }

    return _ScriptedLLMClient()


_ENGLISH_REPLY = (
    "---TITLE---\n"
    "Add PDF export for evaluation reports\n"
    "---DESCRIPTION---\n"
    "Add a PDF export feature so users can download model evaluation results."
)

_INDONESIAN_REPLY = (
    "---TITLE---\n"
    "Buatkan fitur export laporan ke PDF\n"
    "---DESCRIPTION---\n"
    "Tambahkan fitur export laporan ke PDF supaya user bisa mengunduh hasil evaluasi model."
)


def test_enhance_system_prompt_enforces_english():
    client = _scripted_client([_ENGLISH_REPLY])
    with patch("backend.llm_client.get_llm_client", return_value=client):
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "Buatkan fitur export laporan ke PDF."},
        )

    assert response.status_code == 200
    assert len(client.calls) == 1  # English output -> no translation pass
    system_content = client.calls[0]["messages"][0]["content"]
    assert "ALWAYS write" in system_content and "English" in system_content
    assert response.get_json() == {
        "title": "Add PDF export for evaluation reports",
        "description": "Add a PDF export feature so users can download model evaluation results.",
    }


def test_enhance_translates_non_english_output():
    client = _scripted_client([_INDONESIAN_REPLY, _ENGLISH_REPLY])
    with patch("backend.llm_client.get_llm_client", return_value=client):
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "", "description": "Buatkan fitur export laporan ke PDF."},
        )

    assert response.status_code == 200
    assert len(client.calls) == 2  # initial enhance + translation pass
    assert "translator" in client.calls[1]["messages"][0]["content"].lower()
    assert response.get_json() == {
        "title": "Add PDF export for evaluation reports",
        "description": "Add a PDF export feature so users can download model evaluation results.",
    }


def test_enhance_keeps_english_output_without_translation_pass():
    client = _scripted_client([_ENGLISH_REPLY])
    with patch("backend.llm_client.get_llm_client", return_value=client):
        response = _client().post(
            "/api/kanban/enhance",
            json={"title": "PDF export", "description": "Add a PDF export feature."},
        )

    assert response.status_code == 200
    assert len(client.calls) == 1


def test_looks_non_english_heuristic():
    assert routes._looks_non_english("Buatkan fitur export laporan ke PDF supaya user bisa unduh.") is True
    assert routes._looks_non_english("Improve the task creator form.") is False
