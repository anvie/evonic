"""Ping sends a real prompt through the model and never borrows another model's answer."""

from unittest.mock import patch


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _model(db):
    db.create_provider({"id": "pg-prov", "name": "PG", "type": "remote",
                        "endpoint": "https://x.example.invalid/v1",
                        "adapter_key": "openai.chat_completions.v1"})
    db.create_model({"id": "pg-a", "name": "pg-a", "type": "remote", "provider": "pg-prov",
                     "model_name": "pg-a", "enabled": True})


class _Ok:
    def __init__(self, model_config=None, _allow_fallback=True):
        _Ok.kwargs = {"allow_fallback": _allow_fallback, "model": model_config["model_name"]}

    def chat_completion(self, **kw):
        _Ok.call = kw
        return {"success": True, "response": {"choices": [{"message": {"content": "pong"}}]}}


class _Fail:
    def __init__(self, model_config=None, _allow_fallback=True):
        pass

    def chat_completion(self, **kw):
        return {"success": False, "error_type": "auth_error", "error_detail": "401 bad key"}


class _ReasoningOnly:
    """Server answered 200 but burned the whole budget on chain-of-thought."""

    def __init__(self, model_config=None, _allow_fallback=True):
        pass

    def chat_completion(self, **kw):
        return {
            "success": False,
            "error_type": "generation_timeout",
            "answered": True,
            "error_detail": "Generation hit max_tokens limit (32) without producing final answer.",
        }


class _Capture:
    def __init__(self, model_config=None, _allow_fallback=True):
        _Capture.config = model_config

    def chat_completion(self, **kw):
        return {"success": True, "response": {"choices": [{"message": {"content": "pong"}}]}}


def _thinking_model(db, provider_type):
    db.create_provider({"id": f"pg-{provider_type}", "name": provider_type.upper(),
                        "type": provider_type, "endpoint": "https://x.example.invalid/v1",
                        "adapter_key": "openai.chat_completions.v1"})
    db.create_model({"id": f"pg-{provider_type}-think", "name": "thinker",
                     "type": provider_type, "provider": f"pg-{provider_type}",
                     "model_name": "thinker", "thinking": 1, "enabled": True})


def test_ping_success_uses_the_model_without_fallback():
    from models.db import db

    _model(db)
    with patch("backend.llm_client.LLMClient", _Ok):
        data = _client().post("/api/models/pg-a/ping").get_json()
    assert data["success"] is True and data["reply"] == "pong" and data["latency_ms"] >= 0
    assert _Ok.kwargs == {"allow_fallback": False, "model": "pg-a"}
    assert _Ok.call["enable_thinking"] is False


def test_ping_reports_the_failure_reason():
    from models.db import db

    _model(db)
    with patch("backend.llm_client.LLMClient", _Fail):
        data = _client().post("/api/models/pg-a/ping").get_json()
    assert data["success"] is False and "401 bad key" in data["error"]


def test_reasoning_only_answer_is_reachable_not_unreachable():
    """A 200 answer whose budget went to reasoning still proves reachability."""
    from models.db import db

    _model(db)
    with patch("backend.llm_client.LLMClient", _ReasoningOnly):
        data = _client().post("/api/models/pg-a/ping").get_json()

    assert data["success"] is True
    assert data["reply"] == ""
    assert data["reasoning_only"] is True
    assert "reasoning" in data["note"]
    assert data["latency_ms"] >= 0


def test_local_thinking_model_ping_asks_server_to_skip_reasoning():
    from models.db import db

    _thinking_model(db, "local")
    with patch("backend.llm_client.LLMClient", _Capture):
        data = _client().post("/api/models/pg-local-think/ping").get_json()

    assert data["success"] is True
    assert _Capture.config["chat_template_kwargs"] == {"enable_thinking": False}
    assert _Capture.config["max_tokens"] == 32


def test_remote_thinking_model_ping_sends_no_provider_specific_options():
    from models.db import db

    _thinking_model(db, "remote")
    with patch("backend.llm_client.LLMClient", _Capture):
        _client().post("/api/models/pg-remote-think/ping").get_json()

    assert "chat_template_kwargs" not in _Capture.config


def test_ping_unknown_model_is_404():
    assert _client().post("/api/models/nope/ping").status_code == 404
