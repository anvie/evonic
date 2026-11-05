"""Regression tests for the ``reasoning_effort`` setting.

Covers the per-model level (persistence in ``models/mixins/models.py`` — NULL
means "provider default" — and validation/normalisation in
``routes/models.py``) and the per-agent override added on top of it
(``agents.reasoning_effort``, the ``PUT/POST /api/agents`` validation and the
runtime merge that decides what actually reaches the provider payload).

Precedence under test: agent override > model value > key absent.
``backend/reasoning_effort.py`` is the single implementation of that rule.

Note: model updates are exposed as ``PUT /api/models/<id>``; ``routes/models.py``
registers no PATCH alias for that endpoint.
"""

import json
import os
import re
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import pytest

from backend.reasoning_effort import (
    REASONING_EFFORTS,
    InvalidReasoningEffort,
    normalize_reasoning_effort,
    resolve_reasoning_effort,
)
from models.db import db


def _model_data(**overrides):
    data = {
        "name": "Reasoner",
        "type": "llm",
        "provider": "openrouter",
        "model_name": "deepseek/deepseek-r1",
    }
    data.update(overrides)
    return data


def _create(**overrides):
    return db.create_model(_model_data(**overrides))


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
    return c


def _create_via_api(client, **extra):
    payload = _model_data()
    payload.update(extra)
    resp = client.post("/api/models", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["success"] is True
    return data["model_id"]


class TestPersistence:
    def test_create_model_persists_reasoning_effort(self):
        model_id = _create(reasoning_effort="max")
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "max"

    def test_create_model_without_reasoning_effort_stores_null(self):
        model_id = _create()
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_update_model_sets_and_clears_reasoning_effort(self):
        model_id = _create()

        assert db.update_model(model_id, {"reasoning_effort": "low"}) is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "low"

        assert db.update_model(model_id, {"reasoning_effort": None}) is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_decision_model_discards_reasoning_effort(self):
        # Chat-only settings have no meaning on a decision model; the model
        # layer must null it out like thinking/thinking_budget.
        model_id = _create(type="decision", reasoning_effort="max")
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_switching_a_model_to_decision_drops_reasoning_effort(self):
        model_id = _create(reasoning_effort="high")
        assert db.update_model(model_id, {"type": "decision"}) is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None


class TestApiValidation:
    def test_create_accepts_and_lowercases_reasoning_effort(self, client):
        model_id = _create_via_api(client, reasoning_effort="MAX")
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "max"

    def test_create_rejects_unknown_reasoning_effort(self, client):
        before = len(db.get_llm_models())
        resp = client.post("/api/models", json=_model_data(reasoning_effort="ultra"))
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "reasoning_effort" in body["error"]
        assert len(db.get_llm_models()) == before

    def test_create_without_reasoning_effort_stores_null(self, client):
        model_id = _create_via_api(client)
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_update_lowercases_reasoning_effort(self, client):
        model_id = _create_via_api(client)
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": "MAX"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "max"

    def test_update_rejects_unknown_reasoning_effort(self, client):
        model_id = _create_via_api(client, reasoning_effort="high")
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": "ultra"})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        # The rejected value must not overwrite the stored one.
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "high"

    def test_update_blank_reasoning_effort_clears_to_null(self, client):
        model_id = _create_via_api(client, reasoning_effort="max")
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": ""})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_update_null_reasoning_effort_clears_to_null(self, client):
        model_id = _create_via_api(client, reasoning_effort="max")
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": None})
        assert resp.status_code == 200
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_clone_copies_reasoning_effort(self, client):
        model_id = _create_via_api(client, reasoning_effort="medium")
        resp = client.post(f"/api/models/{model_id}/clone")
        assert resp.status_code == 200
        clone_id = resp.get_json()["model_id"]
        assert clone_id != model_id
        assert db.get_model_by_id(clone_id)["reasoning_effort"] == "medium"


# ---------------------------------------------------------------------------
# Per-agent override: stored on the agent, resolved at runtime
# ---------------------------------------------------------------------------


def _agent_data(**overrides):
    data = {"id": "effort_agent", "name": "Effort Agent"}
    data.update(overrides)
    return data


def _create_agent(**overrides):
    return db.create_agent(_agent_data(**overrides))


class TestAgentPersistence:
    def test_create_agent_persists_reasoning_effort(self):
        agent_id = _create_agent(reasoning_effort="max")
        assert db.get_agent(agent_id)["reasoning_effort"] == "max"

    def test_create_agent_without_reasoning_effort_stores_null(self):
        agent_id = _create_agent()
        assert db.get_agent(agent_id)["reasoning_effort"] is None

    def test_update_agent_sets_and_clears_reasoning_effort(self):
        agent_id = _create_agent()

        assert db.update_agent(agent_id, {"reasoning_effort": "low"}) is True
        assert db.get_agent(agent_id)["reasoning_effort"] == "low"

        assert db.update_agent(agent_id, {"reasoning_effort": None}) is True
        assert db.get_agent(agent_id)["reasoning_effort"] is None

    def test_agent_listing_exposes_the_column(self):
        agent_id = _create_agent(reasoning_effort="high")
        rows = {row["id"]: row for row in db.get_agents()}
        assert rows[agent_id]["reasoning_effort"] == "high"

    def test_super_agent_row_exposes_the_column(self):
        assert "reasoning_effort" in db.get_super_agent()

    def test_clone_copies_reasoning_effort(self):
        # create_agent writes the full clone set, so an agent spec carrying the
        # key (agent cloning / template cloning) keeps the override.
        agent_id = _create_agent(reasoning_effort="medium")
        source = db.get_agent(agent_id)
        source["id"] = "effort_agent_clone"
        source["name"] = "Clone"
        clone_id = db.create_agent(source)
        assert db.get_agent(clone_id)["reasoning_effort"] == "medium"


class TestAgentApiValidation:
    def test_update_lowercases_reasoning_effort(self, client):
        agent_id = _create_agent()
        resp = client.put(f"/api/agents/{agent_id}", json={"reasoning_effort": "MAX"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["success"] is True
        assert resp.get_json()["agent"]["reasoning_effort"] == "max"
        assert db.get_agent(agent_id)["reasoning_effort"] == "max"

    def test_update_rejects_unknown_reasoning_effort(self, client):
        agent_id = _create_agent(reasoning_effort="high")
        resp = client.put(f"/api/agents/{agent_id}", json={"reasoning_effort": "ultra"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "reasoning_effort" in body["error"]
        # A rejected value must not overwrite the stored one.
        assert db.get_agent(agent_id)["reasoning_effort"] == "high"

    def test_update_blank_reasoning_effort_clears_to_null(self, client):
        agent_id = _create_agent(reasoning_effort="max")
        resp = client.put(f"/api/agents/{agent_id}", json={"reasoning_effort": ""})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert db.get_agent(agent_id)["reasoning_effort"] is None

    def test_update_null_reasoning_effort_clears_to_null(self, client):
        agent_id = _create_agent(reasoning_effort="max")
        resp = client.put(f"/api/agents/{agent_id}", json={"reasoning_effort": None})
        assert resp.status_code == 200
        assert db.get_agent(agent_id)["reasoning_effort"] is None

    def test_create_accepts_and_lowercases_reasoning_effort(self, client):
        payload = _agent_data(reasoning_effort="MAX")
        resp = client.post("/api/agents", json=payload)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        agent_id = resp.get_json()["agent"]["id"]
        assert db.get_agent(agent_id)["reasoning_effort"] == "max"

    def test_create_rejects_unknown_reasoning_effort(self, client):
        resp = client.post("/api/agents", json=_agent_data(reasoning_effort="ultra"))
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "reasoning_effort" in body["error"]
        assert db.get_agent("effort_agent") is None

    def test_export_exposes_reasoning_effort_as_a_configuration_key(self, tmp_path):
        from backend.agent_portability import export_agent

        agent_id = _create_agent(reasoning_effort="max")
        payload = export_agent(db, agent_id, str(tmp_path))
        assert payload["agent"]["configuration"]["reasoning_effort"] == "max"

    def test_import_spec_normalises_and_rejects_reasoning_effort(self):
        from backend.agent_factory import SpecValidationError, normalize_spec

        normalized = normalize_spec({"id": "imported", "reasoning_effort": "MAX"})
        assert normalized["values"]["reasoning_effort"] == "max"

        with pytest.raises(SpecValidationError):
            normalize_spec({"id": "imported", "reasoning_effort": "ultra"})

    def test_import_payload_is_validated_and_persisted_lowercase(self, tmp_path):
        # The import endpoint writes the configuration straight to the DB, so
        # validate_payload() is the only gate between the file and the column.
        from backend.agent_portability import (
            AgentPortabilityError,
            export_agent,
            import_agent,
            validate_payload,
        )

        payload = export_agent(db, _create_agent(reasoning_effort="max"), str(tmp_path))

        payload["agent"]["configuration"]["reasoning_effort"] = "ultra"
        with pytest.raises(AgentPortabilityError):
            validate_payload(payload)

        payload["agent"]["configuration"]["reasoning_effort"] = "MAX"
        root = str(tmp_path / "import-root")
        os.makedirs(root)
        imported_id = import_agent(
            db, payload, root, agent_id="imported_effort", confirm_missing=True)
        assert db.get_agent(imported_id)["reasoning_effort"] == "max"


class TestAgentDetailUi:
    def test_page_renders_the_control_and_registers_it_for_autosave(self, client):
        agent_id = _create_agent(reasoning_effort="max")
        html = client.get(f"/agents/{agent_id}").get_data(as_text=True)

        assert 'id="agent-reasoning-effort"' in html
        # AgentAutoSave persists it through the generic selnull field handler.
        assert "{ id: 'agent-reasoning-effort', key: 'reasoning_effort', type: 'selnull' }" in html
        # The stored override is preselected server-side.
        assert re.search(r'<option value="max"\s+selected>max</option>', html)

    def test_page_selects_inherit_when_no_override_is_stored(self, client):
        agent_id = _create_agent()
        html = client.get(f"/agents/{agent_id}").get_data(as_text=True)

        assert re.search(r'<option value=""\s+selected>Inherit from model</option>', html)


class TestPrecedenceRule:
    def test_known_values_are_the_canonical_four(self):
        assert REASONING_EFFORTS == ("low", "medium", "high", "max")

    def test_normalize_accepts_case_and_blanks(self):
        assert normalize_reasoning_effort("MAX") == "max"
        assert normalize_reasoning_effort("High") == "high"
        assert normalize_reasoning_effort("") is None
        assert normalize_reasoning_effort(None) is None
        with pytest.raises(InvalidReasoningEffort):
            normalize_reasoning_effort("ultra")

    def test_agent_override_wins_over_model_value(self):
        assert resolve_reasoning_effort("max", "low") == "max"

    def test_model_value_is_used_when_the_agent_has_no_override(self):
        assert resolve_reasoning_effort(None, "max") == "max"

    def test_neither_set_resolves_to_none(self):
        assert resolve_reasoning_effort(None, None) is None
        assert resolve_reasoning_effort("", "") is None


# ---------------------------------------------------------------------------
# Payload level: only OpenAI-compatible requests carry the key
# ---------------------------------------------------------------------------


def _openai_payload(reasoning_effort):
    """Send one OpenAI-format completion and return the JSON body posted."""
    from backend.llm_client import LLMClient

    client = LLMClient(
        model_config={
            "provider": "reasoning-effort-test-provider",
            "base_url": "http://llm.example/v1",
            "api_key": "test-key",
            "model_name": "groot",
            "timeout": 30,
            "reasoning_effort": reasoning_effort,
            "api_format": "openai",
        },
        _allow_fallback=False,
    )
    body = {
        "id": "chatcmpl-test",
        "model": "groot",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }
    response = type("Response", (), {
        "status_code": 200,
        "text": "",
        "headers": {"Content-Type": "application/json"},
        "json": lambda self: body,
    })()
    with patch("backend.llm_client.requests.post", return_value=response) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion(
            [{"role": "user", "content": "ping"}], max_tokens=16, enable_thinking=False)
    assert result["success"] is True, result
    return post.call_args.kwargs["json"]


class TestPayload:
    def test_agent_override_reaches_the_openai_payload(self):
        payload = _openai_payload(resolve_reasoning_effort("max", "low"))
        assert payload["reasoning_effort"] == "max"

    def test_model_value_reaches_the_openai_payload_when_inherited(self):
        payload = _openai_payload(resolve_reasoning_effort(None, "max"))
        assert payload["reasoning_effort"] == "max"

    def test_key_is_absent_when_nothing_is_set(self):
        payload = _openai_payload(resolve_reasoning_effort(None, None))
        assert "reasoning_effort" not in payload


# ---------------------------------------------------------------------------
# Runtime: run_tool_loop resolves the agent override for every model config
# ---------------------------------------------------------------------------


def _ok(content="done"):
    return {
        "success": True,
        "response": {"choices": [{"message": {"content": content, "tool_calls": None},
                                  "finish_reason": "stop"}]},
        "duration_ms": 10,
    }


def _api_error():
    return {
        "success": False,
        "error_type": "api_error",
        "error_detail": "LLM API error: 400 - primary rejected request",
        "response": {},
    }


def _model_row(model_name, effort):
    return {
        "id": f"model-{model_name}",
        "name": model_name.upper(),
        "provider": "test-provider",
        "base_url": "http://llm.example/v1",
        "api_key": "test-key",
        "model_name": model_name,
        "timeout": 30,
        "vision_supported": True,
        "reasoning_effort": effort,
    }


def _run_tool_loop(agent, primary_model, db_agent_row=None,
                   fallback_model=None, primary_llm=None, fallback_llm=None):
    """Run one loop turn against mocked LLM clients and return the client configs."""
    import backend.agent_runtime.llm_loop as llm_loop_mod
    import backend.event_stream as event_stream_mod

    primary_llm = primary_llm or MagicMock(provider="test-provider", model="groot")
    clients = [primary_llm]
    if fallback_model is not None:
        fallback_llm = fallback_llm or MagicMock(provider="test-provider", model="fallback")
        clients.append(fallback_llm)

    mock_db = MagicMock()
    mock_db.get_setting.side_effect = lambda key, default=None: default or "0"
    mock_db.add_chat_message.return_value = None
    mock_db.upsert_agent_state.return_value = None
    mock_db.get_agent_state.return_value = None
    mock_db.get_summary.return_value = None
    mock_db.get_agent_model.return_value = primary_model
    mock_db.get_agent_fallback_model.return_value = fallback_model
    mock_db.get_model_by_id.return_value = None
    mock_db.get_agent.return_value = db_agent_row or {"id": agent["id"], "reasoning_effort": None}
    mock_db.resolve_model_config.side_effect = lambda model: model

    mock_tr = MagicMock()
    mock_tr.get_builtin_executor.return_value = lambda n, a: None
    mock_tr.get_real_executor.return_value = lambda n, a: None

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    with patch.object(llm_loop_mod, "db", mock_db), \
         patch.object(llm_loop_mod, "tool_registry", mock_tr), \
         patch.object(event_stream_mod, "event_stream", MagicMock()), \
         patch.object(llm_loop_mod, "LLMClient", side_effect=clients) as client_cls, \
         patch.object(llm_loop_mod, "llm_client", primary_llm):
        llm_loop_mod.run_tool_loop(
            agent=agent,
            agent_context={"user_id": "u1", "channel_id": "ch1", "is_super": False,
                           "agent_state": None},
            messages=messages,
            tools=[],
            session_id="sess-effort",
            llm_lock=threading.Lock(),
            stop_event=threading.Event(),
            session_skill_mds={},
            session_skill_tools={},
            llm_log_path=None,
        )

    return [call.kwargs.get("model_config") for call in client_cls.call_args_list]


class TestRuntimeMerge:
    def test_primary_config_uses_the_agent_override(self):
        primary = MagicMock(provider="test-provider", model="groot")
        primary.chat_completion.return_value = _ok("Done.")
        configs = _run_tool_loop(
            agent={"id": "a1", "reasoning_effort": "max"},
            primary_model=_model_row("groot", "low"),
            primary_llm=primary,
        )
        assert configs[0]["reasoning_effort"] == "max"

    def test_primary_config_inherits_the_model_value_without_an_override(self):
        primary = MagicMock(provider="test-provider", model="groot")
        primary.chat_completion.return_value = _ok("Done.")
        configs = _run_tool_loop(
            agent={"id": "a1", "reasoning_effort": None},
            primary_model=_model_row("groot", "max"),
            primary_llm=primary,
        )
        assert configs[0]["reasoning_effort"] == "max"

    def test_agent_row_is_the_source_when_the_agent_dict_omits_the_key(self):
        # Explorer rows are partial dicts; the override must still come from the
        # agent's stored row rather than silently falling back to the model.
        primary = MagicMock(provider="test-provider", model="groot")
        primary.chat_completion.return_value = _ok("Done.")
        configs = _run_tool_loop(
            agent={"id": "a1", "model": None},
            primary_model=_model_row("groot", "low"),
            db_agent_row={"id": "a1", "reasoning_effort": "max"},
            primary_llm=primary,
        )
        assert configs[0]["reasoning_effort"] == "max"

    def test_fallback_config_keeps_the_agent_override(self):
        primary = MagicMock(provider="test-provider", model="groot")
        primary.chat_completion.return_value = _api_error()
        fallback = MagicMock(provider="test-provider", model="fallback")
        fallback.chat_completion.return_value = _ok("Fallback done.")
        configs = _run_tool_loop(
            agent={"id": "a1", "reasoning_effort": "max"},
            primary_model=_model_row("groot", "low"),
            fallback_model=_model_row("fallback", "medium"),
            primary_llm=primary,
            fallback_llm=fallback,
        )
        assert len(configs) >= 2, configs
        assert configs[0]["reasoning_effort"] == "max"
        assert configs[-1]["model_name"] == "fallback"
        assert configs[-1]["reasoning_effort"] == "max"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_agents_table_gains_the_column_on_startup(self, tmp_path, monkeypatch):
        legacy_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """
            CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                system_prompt TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()

        db.close()
        monkeypatch.setattr(db, "db_path", legacy_path)
        monkeypatch.setattr(db, "_tls", threading.local())

        # Startup runs the migrations; a second start must not raise (idempotent).
        db._init_tables()
        db._init_tables()

        with db._connect() as c:
            columns = {row[1] for row in c.execute("PRAGMA table_info(agents)")}
        assert "reasoning_effort" in columns
