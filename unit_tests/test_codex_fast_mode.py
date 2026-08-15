"""Session-scoped Fast mode for the Codex Responses API."""

import json
from unittest.mock import MagicMock, patch

from backend.provider.codex_client import CodexClient, model_supports_fast_mode
from backend.slash_commands import execute_command


class _ChatDB:
    def __init__(self):
        self.states = {}

    def get_session_state(self, session_id):
        return self.states.get(session_id)

    def upsert_session_state(self, session_id, content):
        self.states[session_id] = content

    def get_agent_state(self):
        return None


def _codex_model(model_name="gpt-5.6-luna"):
    return {
        "id": f"openai/{model_name}",
        "name": model_name,
        "provider": "openai",
        "model_name": model_name,
        "api_format": "codex",
    }


def _completed_response(model):
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "id": "resp_fast",
        "model": model,
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "ok"}],
        }],
        "usage": {},
    }
    return response


def test_fast_model_gate_matches_codex_catalog_families():
    assert model_supports_fast_mode("gpt-5.4")
    assert model_supports_fast_mode("openai/gpt-5.5")
    assert model_supports_fast_mode("gpt-5.6-luna:high")
    assert not model_supports_fast_mode("gpt-5.4-mini")
    assert not model_supports_fast_mode("gpt-5.3-codex-spark")
    assert not model_supports_fast_mode("claude-opus-4-6")


def test_codex_fast_request_adds_priority_payload_and_routing_header():
    client = CodexClient("token", "https://chatgpt.com/backend-api/codex")
    model = "gpt-5.6-luna"
    with patch("backend.provider.codex_client.httpx.post",
               return_value=_completed_response(model)) as post:
        result = client.send_request(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            service_tier="priority",
        )

    assert result["success"]
    assert post.call_args.kwargs["json"]["service_tier"] == "priority"
    assert post.call_args.kwargs["headers"]["x-codex-routing-hint"] == (
        "model=gpt-5.6-luna;tier=priority"
    )


def test_codex_fast_request_is_omitted_for_unsupported_model():
    client = CodexClient("token", "https://chatgpt.com/backend-api/codex")
    model = "gpt-5.4-mini"
    with patch("backend.provider.codex_client.httpx.post",
               return_value=_completed_response(model)) as post:
        client.send_request(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            service_tier="priority",
        )

    assert "service_tier" not in post.call_args.kwargs["json"]
    assert "x-codex-routing-hint" not in post.call_args.kwargs["headers"]


def test_fast_command_is_session_scoped_and_preserves_state(monkeypatch):
    from models.chat import agent_chat_manager
    from models.db import db

    chat_db = _ChatDB()
    chat_db.states["session-a"] = json.dumps({"workspace_marker": "keep"})
    monkeypatch.setattr(db, "get_agent_model", lambda _agent_id: _codex_model())
    monkeypatch.setattr(db, "resolve_model_config", lambda model: model)
    monkeypatch.setattr(agent_chat_manager, "get", lambda _agent_id: chat_db)

    assert "enabled" in execute_command("fast", "fast", "session-a", "agent", "user")
    assert execute_command("fast", "status", "session-a", "agent", "user") == "Fast mode: on."
    assert execute_command("fast", "status", "session-b", "agent", "user") == "Fast mode: off."
    assert json.loads(chat_db.states["session-a"])["workspace_marker"] == "keep"

    assert "disabled" in execute_command("fast", "normal", "session-a", "agent", "user")
    assert "service_tier" not in json.loads(chat_db.states["session-a"])


def test_fast_command_rejects_non_codex_model_without_writing(monkeypatch):
    from models.chat import agent_chat_manager
    from models.db import db

    model = {**_codex_model(), "api_format": "openai"}
    monkeypatch.setattr(db, "get_agent_model", lambda _agent_id: model)
    monkeypatch.setattr(db, "resolve_model_config", lambda value: value)
    monkeypatch.setattr(
        agent_chat_manager,
        "get",
        lambda _agent_id: (_ for _ in ()).throw(AssertionError("must not write session state")),
    )

    assert execute_command("fast", "on", "session", "agent", "user") == (
        "Fast mode is not supported by the current model."
    )


def test_status_reports_effective_fast_session_setting(monkeypatch):
    from models.chat import agent_chat_manager
    from models.db import db

    chat_db = _ChatDB()
    chat_db.states["session"] = json.dumps({"service_tier": "priority"})
    monkeypatch.setattr(agent_chat_manager, "get", lambda _agent_id: chat_db)
    monkeypatch.setattr(db, "get_agent", lambda _agent_id: {"id": "agent", "name": "Agent"})
    monkeypatch.setattr(db, "get_agent_model", lambda _agent_id: _codex_model())
    monkeypatch.setattr(db, "resolve_model_config", lambda model: model)
    monkeypatch.setattr(db, "get_agent_tools", lambda _agent_id: [])
    monkeypatch.setattr(db, "get_agent_skills", lambda _agent_id: [])
    monkeypatch.setattr(db, "get_channels", lambda _agent_id: [])

    result = execute_command("status", "", "session", "agent", "user")

    assert "Fast: on" in result
