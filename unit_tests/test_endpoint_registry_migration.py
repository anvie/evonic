"""Regression coverage for the additive provider endpoint registry migration."""

import json

import pytest


CANONICAL_CAPABILITIES = {
    "version": "model-capabilities/v1",
    "operations": {"chat": "verified", "decision": "advertised"},
    "schema_versions": ["decision-schema/v1"],
    "confidence": "unsupported",
    "data_egress": ["public"],
}


@pytest.fixture
def client():
    from app import app

    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session["authenticated"] = True
    return test_client


def test_fresh_database_has_endpoint_registry_ledger_and_additive_fields():
    from models.db import db

    with db._connect() as conn:
        provider_columns = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
        model_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_models)")}
        ledger = {
            row[0]: json.loads(row[1])
            for row in conn.execute("SELECT migration_id, details FROM registry_migration_ledger")
        }

    assert {"endpoint", "adapter_key", "connection_options"} <= provider_columns
    assert "legacy_override_provenance" in model_columns
    assert set(ledger) == {
        "registry_endpoint_v1_provider_fields",
        "registry_endpoint_v1_model_capabilities",
        "model_type_split_v1",
        "dmss_endpoint_to_model_v1",
    }
    assert ledger["registry_endpoint_v1_provider_fields"]["precondition"] == "provider connection columns present"


def test_builtin_liquidai_d1_3b_model_is_selectable_without_connection_data():
    from models.db import db

    model = db.get_model_by_id("systemone/LiquidAI-d1-3B")

    assert model["name"] == "LiquidAI d1-3B"
    assert model["provider"] == "systemone"
    assert model["model_name"] == "LiquidAI/d1-3B"
    assert model["base_url"] == model["api_key"] == ""
    assert model["api_format"] == "systemone"
    assert model["context_window"] == 32768
    assert model["type"] == "decision"
    assert model["vision_supported"] is False
    # Its SystemOne provider ships endpoint-less, ready for the operator to fill in.
    provider = db.get_provider("systemone")
    assert (provider["endpoint"], provider["adapter_key"]) == ("", "systemone.questions.v1")
    assert model["capabilities"] == {
        "version": "model-capabilities/v1",
        "operations": {"decision": "verified", "strict_output": "verified"},
        "schema_versions": ["decision-schema/v1"],
        "confidence": "verified",
        "data_egress": ["public"],
    }


def _simulate_legacy_registry():
    """Strip the new registry fields to exercise the upgrade path in place."""
    from models.db import db

    with db._connect() as conn:
        conn.execute("DELETE FROM registry_migration_ledger")
        conn.execute("DELETE FROM providers")
        conn.execute("INSERT INTO providers (id, name, type, base_url, api_format) VALUES ('legacy', 'Legacy', 'remote', 'https://legacy.example.invalid/v1', 'ollama')")
        conn.execute(
            "INSERT INTO llm_models (id, name, type, provider, base_url, api_key, model_name, api_format, capabilities) "
            "VALUES ('legacy/model', 'Legacy Model', 'remote', 'legacy', "
            "'https://override.example.invalid/v1', 'secret', 'native', 'openai', ?)",
            (json.dumps({"decision": {"structured_output": True, "schema_primitives": ["choice"]}}),),
        )
        conn.commit()


def test_upgrade_migration_is_idempotent_and_normalizes_legacy_rows():
    from models.db import db

    _simulate_legacy_registry()
    db._init_tables()
    db._init_tables()  # resuming/re-running must be safe and must not duplicate work

    provider = db.get_provider("legacy")
    assert provider["endpoint"] == "https://legacy.example.invalid/v1"
    assert provider["adapter_key"] == "ollama.chat.v1"
    assert provider["api_format"] == "ollama"

    model = db.get_model_by_id("legacy/model")
    assert model["legacy_override_provenance"] == "legacy_model_override"
    # Decision-capable and unused for chat: it becomes a decision model.
    assert model["type"] == "decision"
    assert model["capabilities"]["version"] == "model-capabilities/v1"
    assert model["capabilities"]["operations"]["decision"] == "advertised"
    assert model["capabilities"]["schema_versions"] == ["decision-schema/v1"]

    with db._connect() as conn:
        ledger_count = conn.execute("SELECT COUNT(*) FROM registry_migration_ledger").fetchone()[0]
    assert ledger_count == 4

    # Legacy override stays operational through the compatibility resolver only.
    config = db.resolve_model_config(model)
    assert config["base_url"] == "https://override.example.invalid/v1"
    assert config["legacy_provenance"] == "legacy_model_override"


def test_canonical_decision_capabilities_remain_assignable():
    from backend.tools.lib.decision_adapters import public_decision_capabilities, validate_decision_compatibility
    from backend.tools.lib.decision_models import DecisionInvocation, DecisionProfileRef, parse_schema

    model = {
        "id": "provider/decision", "enabled": True, "api_format": "openai",
        "capabilities": {
            "version": "model-capabilities/v1", "operations": {"chat": "verified", "decision": "advertised", "strict_output": "advertised"},
            "schema_versions": ["decision-schema/v1"], "confidence": "unsupported", "data_egress": ["public"],
        },
    }
    assert public_decision_capabilities(model)["decision"]["structured_output"] is True
    invocation = DecisionInvocation(
        profile=DecisionProfileRef("agent.task_complexity", "1"),
        schema=parse_schema({"version": "decision-schema/v1", "fields": [
            {"key": "complexity", "kind": "choice", "choices": ["trivial", "complex"]},
        ]}),
        input_context={"task": "x"},
    )
    assert validate_decision_compatibility(model, invocation)["decision"]["structured_output"] is True


def test_provider_dual_write_and_route_resolution_prefers_endpoint_contract():
    from models.db import db

    provider_id = db.create_provider({
        "id": "endpoint-test", "name": "Endpoint Test", "endpoint": "https://example.invalid/v1/",
        "adapter_key": "openai.responses.v1", "connection_options": {"headers": {"x-client": "test"}},
    })
    provider = db.get_provider(provider_id)
    assert provider["endpoint"] == provider["base_url"] == "https://example.invalid/v1"
    assert provider["adapter_key"] == "openai.responses.v1"
    assert provider["api_format"] == "openai"
    assert json.loads(provider["connection_options"]) == {"headers": {"x-client": "test"}}

    model_id = db.create_model({
        "name": "Endpoint model", "type": "remote", "provider": provider_id, "model_name": "native-model",
        "capabilities": CANONICAL_CAPABILITIES,
    })
    route = db.resolve_model_route(model_id)
    assert route.provider.endpoint == "https://example.invalid/v1"
    assert route.provider.adapter_key.value == "openai.responses.v1"
    assert route.to_public_dict()["adapter_key"] == "openai.responses.v1"
    assert "example.invalid" not in str(route.to_public_dict())


def test_provider_legacy_format_change_updates_in_sync_fields_and_rejects_invalid_endpoint():
    from models.db import db

    provider_id = db.create_provider({
        "id": "sync-provider", "name": "Sync", "endpoint": "https://sync.example.invalid/v1",
        "adapter_key": "openai.chat_completions.v1",
    })
    # Legacy api_format edits must recompute the adapter, not silently diverge.
    assert db.update_provider(provider_id, {"api_format": "anthropic"}) is True
    provider = db.get_provider(provider_id)
    assert provider["adapter_key"] == "anthropic.messages.v1"
    assert provider["api_format"] == "anthropic"
    assert provider["endpoint"] == provider["base_url"] == "https://sync.example.invalid/v1"

    with pytest.raises(ValueError):
        db.update_provider(provider_id, {"endpoint": "not-a-url"})


def test_legacy_model_format_override_still_routes_until_deprecated():
    from models.db import db

    provider_id = db.create_provider({
        "id": "format-provider", "name": "Format", "endpoint": "https://format.example.invalid/v1",
        "adapter_key": "openai.chat_completions.v1",
    })
    # A model carrying its own non-default legacy format keeps routing behaviour
    # during the compatibility window instead of silently switching adapters.
    model_id = db.create_model({
        "name": "Legacy format", "type": "local", "provider": provider_id, "model_name": "legacy-format",
        "api_format": "ollama", "capabilities": CANONICAL_CAPABILITIES,
    })
    config = db.resolve_model_config(db.get_model_by_id(model_id))
    assert config["adapter_key"] == "ollama.chat.v1"
    assert config["api_format"] == "ollama"


def test_new_models_cannot_set_connection_overrides_and_capabilities_are_validated(client):
    from models.db import db

    provider_id = db.create_provider({
        "id": "api-provider", "name": "API Provider", "endpoint": "https://api.example.invalid/v1",
        "adapter_key": "openai.chat_completions.v1",
    })
    overridden = client.post("/api/models", json={
        "name": "Bad", "type": "remote", "provider": provider_id, "model_name": "bad", "base_url": "https://override.invalid",
    })
    assert overridden.status_code == 400
    assert "inherit endpoint" in overridden.get_json()["error"]

    invalid = client.post("/api/models", json={
        "name": "Bad capability", "type": "remote", "provider": provider_id, "model_name": "bad-capability",
        "capabilities": {"version": "model-capabilities/v1", "operations": {"not-real": "verified"}},
    })
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_capability"

    unknown_type = client.post("/api/models", json={
        "name": "Bad type", "type": "private-network", "provider": provider_id, "model_name": "bad-type",
    })
    assert unknown_type.status_code == 400

    created = client.post("/api/models", json={
        "name": "Good", "type": "llm", "provider": provider_id, "model_name": "good",
        "capabilities": CANONICAL_CAPABILITIES,
    })
    assert created.status_code == 200
    # An LLM never carries decision operations; capabilities follow the type.
    assert db.get_model_by_id(created.get_json()["model_id"])["capabilities"] == {
        **CANONICAL_CAPABILITIES, "operations": {"chat": "verified"}, "schema_versions": [],
    }


def test_provider_api_redacts_credentials_and_model_delete_reports_soft_references(client):
    from models.db import db

    provider_id = db.create_provider({
        "id": "redacted-provider", "name": "Redacted", "endpoint": "https://redacted.example.invalid/v1",
        "adapter_key": "openai.chat_completions.v1", "api_key": "never-return-this",
    })
    provider_response = client.get("/api/providers/redacted-provider")
    assert provider_response.status_code == 200
    assert "never-return-this" not in str(provider_response.get_json())

    model_id = db.create_model({
        "name": "Referenced", "type": "remote", "provider": provider_id, "model_name": "referenced",
        "capabilities": CANONICAL_CAPABILITIES,
    })
    with db._connect() as conn:
        conn.execute("UPDATE agents SET model_id = ? WHERE id = ?", (model_id, "test_super_agent"))
        conn.execute(
            "UPDATE decision_profiles SET primary_model_id = ? WHERE profile_key = ?",
            (model_id, "agent.task_complexity"),
        )
        conn.commit()
    response = client.delete(f"/api/models/{model_id}")
    assert response.status_code == 200
    references = response.get_json()["affected_references"]
    assert "model_id:test_super_agent" in references["agents"]
    assert "agent.task_complexity" in references["profiles"]


def _rerun_registry_migrations(*migration_ids):
    from models.db import db

    with db._connect() as conn:
        conn.executemany("DELETE FROM registry_migration_ledger WHERE migration_id = ?",
                         [(m,) for m in migration_ids])
        conn.commit()
    db._init_tables()
    db.invalidate_settings_cache()


def test_type_split_keeps_chat_models_and_moves_profiles_to_a_decision_copy():
    from models.db import db

    decision_caps = json.dumps({**CANONICAL_CAPABILITIES, "operations": {"chat": "verified", "decision": "advertised", "strict_output": "advertised"}})
    with db._connect() as conn:
        conn.execute("INSERT INTO llm_models (id, name, type, provider, model_name, capabilities) "
                     "VALUES ('mixed/m', 'Mixed', 'remote', 'mixed', 'm', ?)", (decision_caps,))
        conn.execute("INSERT INTO llm_models (id, name, type, provider, model_name, capabilities) "
                     "VALUES ('plain/m', 'Plain', 'local', 'plain', 'm', '{}')")
        conn.execute("UPDATE agents SET model_id = 'mixed/m' WHERE id = 'test_super_agent'")
        conn.execute("INSERT INTO decision_profiles (profile_key, display_name, schema_version, policy_version, "
                     "schema_json, primary_model_id, timeout_ms) VALUES ('application.mixed', 'Mixed', "
                     "'decision-schema/v1', 'p/v1', '{}', 'mixed/m', 1000)")
        conn.commit()

    _rerun_registry_migrations("model_type_split_v1")

    mixed, plain = db.get_model_by_id("mixed/m"), db.get_model_by_id("plain/m")
    assert (mixed["type"], plain["type"]) == ("llm", "llm")
    assert "decision" not in mixed["capabilities"]["operations"]
    copy = db.get_model_by_id("mixed/m-decision")
    assert copy["type"] == "decision"
    assert db.get_decision_profile("application.mixed")["primary_model_id"] == "mixed/m-decision"


def test_dmss_endpoint_moves_into_a_systemone_provider_and_model(monkeypatch):
    from models.db import db

    monkeypatch.delenv("DECIM_SAFETY_SYSTEMONE_ENDPOINT", raising=False)
    db.set_setting("decim_safety.provider", "systemone")
    db.set_setting("decim_safety.provider_endpoint", "https://s1.example.invalid/decide")

    _rerun_registry_migrations("dmss_endpoint_to_model_v1")

    assert db.get_setting("decim_safety.model_id") == "systemone/LiquidAI-d1-3B"
    assert db.get_setting("decim_safety.provider_endpoint", None) is None
    assert db.get_setting("decim_safety.provider", None) is None
    route = db.resolve_model_route("systemone/LiquidAI-d1-3B")
    assert route.provider.endpoint == "https://s1.example.invalid/decide"
    assert route.provider.adapter_key.value == "systemone.questions.v1"


def test_dmss_endpoint_gets_its_own_provider_when_systemone_points_elsewhere(monkeypatch):
    from models.db import db

    monkeypatch.delenv("DECIM_SAFETY_SYSTEMONE_ENDPOINT", raising=False)
    db.update_provider("systemone", {"endpoint": "https://other.example.invalid/decide"})
    db.set_setting("decim_safety.provider_endpoint", "https://s1.example.invalid/decide")

    _rerun_registry_migrations("dmss_endpoint_to_model_v1")

    model_id = db.get_setting("decim_safety.model_id")
    assert model_id == "systemone-dmss/LiquidAI-d1-3B"
    assert db.get_model_by_id(model_id)["type"] == "decision"
    assert db.resolve_model_route(model_id).provider.endpoint == "https://s1.example.invalid/decide"
    assert db.get_provider("systemone")["endpoint"] == "https://other.example.invalid/decide"


def test_decision_model_without_provider_endpoint_reports_the_real_cause():
    from backend.tools.lib.decision_runtime import DecisionRuntime, RuntimeRequest
    from models.db import db

    db.create_decision_profile({
        "profile_key": "application.unconfigured", "display_name": "Unconfigured", "enabled": True,
        "mode": "shadow", "primary_model_id": "systemone/LiquidAI-d1-3B", "timeout_ms": 1000,
        "retry_limit": 0, "minimum_confidence": 0.5, "policy_version": "p/v1", "schema_version": "decision-schema/v1",
        "schema": {"version": "decision-schema/v1", "fields": [{"key": "approved", "kind": "boolean"}]},
    })
    result = DecisionRuntime(db).invoke(RuntimeRequest(
        profile_key="application.unconfigured", profile_version="p/v1", input_context={"x": 1}))
    assert result.error.code.value == "provider_unavailable"
    assert "provider 'systemone'" in result.error.message
    assert "base_url" not in result.error.message
