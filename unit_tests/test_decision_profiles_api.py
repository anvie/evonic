"""Focused persistence and HTTP tests for Decision Profile management."""

import sqlite3

import pytest

from app import app
from models.db import db


@pytest.fixture
def client():
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _model(model_id="test/profile-primary"):
    return db.create_model({
        "id": model_id,
        "name": model_id,
        "type": "decision",
        "provider": "test",
        "model_name": model_id.rsplit("/", 1)[-1],
        "capabilities": {"decision": {"enabled": True, "schema_versions": ["decision-schema/v1"]}},
    })


def _payload(key="application.example", **overrides):
    data = {
        "profile_key": key,
        "display_name": "Example decision",
        "description": "A managed test profile.",
        "enabled": True,
        "mode": "enforce",
        "schema_version": "decision-schema/v1",
        "policy_version": "policy/v1",
        "schema": {"version": "decision-schema/v1", "fields": [
            {"key": "approved", "kind": "boolean"},
        ]},
        "primary_model_id": "test/profile-primary",
        "fallback_model_id": None,
        "timeout_ms": 1200,
        "retry_limit": 1,
        "minimum_confidence": 0.75,
    }
    data.update(overrides)
    return data


def test_schema_is_created_idempotently_and_profile_round_trips():
    _model()
    db._init_tables()
    created = db.create_decision_profile(_payload())
    assert created["profile_key"] == "application.example"
    assert created["schema"]["fields"] == [{
        "key": "approved", "kind": "boolean", "description": "",
        "required": True, "nullable": False,
    }]
    assert created["revision"] == 1

    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_profiles").fetchone()[0] == 4


def test_builtin_agent_profiles_are_seeded_disabled_and_idempotent():
    db._init_tables()
    profiles = {profile["profile_key"]: profile for profile in db.list_decision_profiles()}
    for key in ("agent.task_complexity", "agent.path_boundary"):
        profile = profiles[key]
        assert profile["enabled"] is False
        assert profile["mode"] == "off"
        assert profile["primary_model_id"] == ""
        assert profile["schema_version"] == "decision-schema/v1"

    # A later startup preserves the same seed rows rather than replacing them.
    db._init_tables()
    assert len([profile for profile in db.list_decision_profiles()
                if profile["profile_key"] in {"agent.task_complexity", "agent.path_boundary"}]) == 2


def test_api_crud_disable_conflict_and_secret_free_model_reference(client):
    _model()
    created = client.post("/api/admin/decision-profiles", json=_payload())
    assert created.status_code == 201
    profile = created.get_json()["profile"]
    assert profile["primary_model_id"] == "test/profile-primary"
    assert "api_key" not in profile

    read = client.get("/api/admin/decision-profiles/application.example")
    assert read.status_code == 200
    assert read.get_json()["profile"]["schema"] == profile["schema"]

    update = _payload(enabled=False, mode="off", revision=profile["revision"])
    disabled = client.put("/api/admin/decision-profiles/application.example", json=update)
    assert disabled.status_code == 200
    assert disabled.get_json()["profile"]["enabled"] is False
    assert disabled.get_json()["profile"]["revision"] == 2

    stale = client.put("/api/admin/decision-profiles/application.example", json=update)
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "conflict"
    enabled = client.get("/api/admin/decision-profiles?enabled_only=1").get_json()["profiles"]
    assert [profile["profile_key"] for profile in enabled] == ["agent.skill_worthiness"]


def test_api_rejects_invalid_values_and_unknown_model_with_actionable_errors(client):
    _model()
    response = client.post("/api/admin/decision-profiles", json=_payload(
        key="badkey", timeout_ms=0, primary_model_id="missing/model",
    ))
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_key"
    assert error["path"] == "profile_key"

    response = client.post("/api/admin/decision-profiles", json=_payload(
        key="application.unknown_model", primary_model_id="missing/model",
    ))
    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "unknown_model",
        "path": "primary_model_id",
        "message": "does not reference a configured model",
    }

    response = client.post("/api/admin/decision-profiles", json=_payload(
        key="application.invalid_schema", schema={"version": "decision-schema/v1", "fields": []},
    ))
    assert response.status_code == 400
    assert response.get_json()["error"]["path"].startswith("schema.")


def test_builtin_profile_cannot_be_deleted_but_can_be_deactivated(client):
    _model()
    create = client.post("/api/admin/decision-profiles", json=_payload("safety.command"))
    profile = create.get_json()["profile"]

    deleted = client.delete("/api/admin/decision-profiles/safety.command")
    assert deleted.status_code == 409
    assert deleted.get_json()["error"]["code"] == "builtin_profile"

    disabled = client.put("/api/admin/decision-profiles/safety.command", json=_payload(
        "safety.command", enabled=False, mode="off", revision=profile["revision"],
    ))
    assert disabled.status_code == 200
    assert disabled.get_json()["profile"]["enabled"] is False


def test_non_builtin_can_be_deleted_and_routes_require_authentication(client):
    _model()
    assert client.post("/api/admin/decision-profiles", json=_payload()).status_code == 201
    assert client.delete("/api/admin/decision-profiles/application.example").status_code == 200
    assert client.get("/api/admin/decision-profiles/application.example").status_code == 404

    anonymous = app.test_client()
    assert anonymous.get("/api/admin/decision-profiles").status_code == 401


def test_api_accepts_canonical_decision_model_capabilities(client):
    # ``systemone/LiquidAI-d1-3B`` ships as a builtin canonical-capability model,
    # so it is consumed here instead of re-created under the same id.
    assert db.get_model_by_id("systemone/LiquidAI-d1-3B") is not None

    response = client.post("/api/admin/decision-profiles", json=_payload(
        key="application.liquidai", primary_model_id="systemone/LiquidAI-d1-3B",
    ))

    assert response.status_code == 201
    assert response.get_json()["profile"]["primary_model_id"] == "systemone/LiquidAI-d1-3B"


def test_api_rejects_canonical_model_without_strict_output(client):
    db.create_model({
        "id": "test/decision-without-strict-output",
        "name": "decision without strict output",
        "type": "remote",
        "provider": "test",
        "model_name": "decision-without-strict-output",
        "capabilities": {
            "operations": {"decision": "verified"},
            "schema_versions": ["decision-schema/v1"],
        },
    })
    response = client.post("/api/admin/decision-profiles", json=_payload(
        key="application.incompatible", primary_model_id="test/decision-without-strict-output",
    ))
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "incompatible_model"


def test_api_rejects_models_without_decision_capability(client):
    db.create_model({
        "id": "test/not-decision",
        "name": "not decision",
        "type": "remote",
        "provider": "test",
        "model_name": "not-decision",
    })
    response = client.post("/api/admin/decision-profiles", json=_payload(
        key="application.incompatible", primary_model_id="test/not-decision",
    ))
    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "incompatible_model",
        "path": "primary_model_id",
        "message": "must reference a decision model",
    }


def test_sample_input_round_trips_and_survives_updates_that_omit_it():
    _model()
    sample = {"incident": "Payment-service outage", "impact": "Checkout is down", "count": 3}
    created = db.create_decision_profile(_payload("application.sampled", sample_input=sample))
    assert created["sample_input"] == sample
    assert db.get_decision_profile("application.sampled")["sample_input"] == sample

    # An update that does not mention the sample keeps it; an explicit value replaces it.
    base = {k: v for k, v in created.items() if k not in {"sample_input", "revision", "created_at", "updated_at"}}
    kept = db.update_decision_profile("application.sampled", {**base, "display_name": "Renamed"}, created["revision"])
    assert kept["sample_input"] == sample and kept["display_name"] == "Renamed"
    replaced = db.update_decision_profile("application.sampled", {**base, "sample_input": {"incident": "x"}}, kept["revision"])
    assert replaced["sample_input"] == {"incident": "x"}
    cleared = db.update_decision_profile("application.sampled", {**base, "sample_input": None}, replaced["revision"])
    assert cleared["sample_input"] is None


def test_profiles_without_a_sample_report_none_and_invalid_samples_are_rejected():
    _model()
    plain = db.create_decision_profile(_payload("application.unsampled"))
    assert plain["sample_input"] is None
    from models.mixins.decision_profiles import DecisionProfileValidationError
    for bad in ("not an object", [1, 2], {"blob": "x" * 9000}):
        with pytest.raises(DecisionProfileValidationError) as exc:
            db.create_decision_profile(_payload("application.badsample", sample_input=bad))
        assert exc.value.path == "sample_input"


def test_sample_input_is_available_over_the_api(client):
    _model()
    db.create_decision_profile(_payload("application.apisample", sample_input={"incident": "disk full"}))
    body = client.get("/api/admin/decision-profiles/application.apisample").get_json()
    assert body["profile"]["sample_input"] == {"incident": "disk full"}


def test_existing_database_gains_the_sample_column():
    db._init_tables()
    with sqlite3.connect(db.db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(decision_profiles)")}
    assert "sample_input_json" in cols
    db._init_tables()                      # idempotent


def test_a_fallback_chain_round_trips_and_mirrors_its_first_entry():
    for mid in ("test/profile-primary", "test/fb-1", "test/fb-2", "test/fb-3"):
        _model(mid)
    created = db.create_decision_profile(_payload("application.chain",
                                                  fallback_model_ids=["test/fb-1", "test/fb-2"]))
    assert created["fallback_model_ids"] == ["test/fb-1", "test/fb-2"]
    assert created["fallback_model_id"] == "test/fb-1"
    # A write with only fallback_model_id (the single-fallback editor) replaces the first and keeps the rest.
    data = {k: v for k, v in created.items() if k != "fallback_model_ids"}
    data["fallback_model_id"] = "test/fb-3"
    updated = db.update_decision_profile("application.chain", data, created["revision"])
    assert updated["fallback_model_ids"] == ["test/fb-3", "test/fb-2"]
    # Clearing it clears the chain.
    data = {k: v for k, v in updated.items() if k != "fallback_model_ids"}
    data["fallback_model_id"] = None
    cleared = db.update_decision_profile("application.chain", data, updated["revision"])
    assert cleared["fallback_model_ids"] == [] and cleared["fallback_model_id"] is None


def test_a_fallback_chain_is_validated():
    from models.mixins.decision_profiles import DecisionProfileValidationError
    for mid in ("test/profile-primary", "test/fb-1"):
        _model(mid)
    for ids, code in ((["test/fb-1", "test/fb-1"], "duplicate_fallback_model"),
                      (["test/profile-primary"], "duplicate_fallback_model"),
                      (["test/missing"], "unknown_model"),
                      (["test/fb-1"] * 6, "too_many_fallback_models")):
        with pytest.raises(DecisionProfileValidationError) as err:
            db.validate_decision_profile(_payload("application.bad", fallback_model_ids=ids))
        assert err.value.code == code


def test_a_model_deep_in_a_fallback_chain_is_still_referenced():
    for mid in ("test/profile-primary", "test/fb-1", "test/fb-2"):
        _model(mid)
    db.create_decision_profile(_payload("application.refs", fallback_model_ids=["test/fb-1", "test/fb-2"]))
    assert "application.refs" in db.get_model_references("test/fb-2")["profiles"]
    with db._connect() as conn:
        assert db._model_is_referenced(conn.cursor(), "test/fb-2")


def test_skill_worthiness_is_seeded_active_on_the_default_models():
    db._init_tables()
    profile = db.get_decision_profile("agent.skill_worthiness")
    assert profile["enabled"] is True
    assert profile["mode"] == "enforce"
    assert profile["primary_model_id"] == ""


def test_default_models_chain_round_trips_and_is_validated(client):
    for index in range(6):
        _model(f"test/default-{index}")
    assert client.get("/api/admin/decision-default-models").get_json() == {"model_ids": []}

    chain = [f"test/default-{index}" for index in range(5)]
    saved = client.put("/api/admin/decision-default-models", json={"model_ids": chain})
    assert saved.get_json() == {"success": True, "model_ids": chain}
    assert client.get("/api/admin/decision-default-models").get_json() == {"model_ids": chain}

    for bad, code in ((chain + ["test/default-5"], "too_many_default_models"),
                      (["test/default-0", "test/default-0"], "duplicate_default_model"),
                      (["missing/model"], "unknown_model")):
        response = client.put("/api/admin/decision-default-models", json={"model_ids": bad})
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == code
    assert db.get_default_decision_model_ids() == chain


def test_active_profile_without_a_model_needs_the_default_chain(client):
    _model()
    missing = client.post("/api/admin/decision-profiles", json=_payload(primary_model_id=""))
    assert missing.status_code == 400
    assert missing.get_json()["error"]["path"] == "primary_model_id"

    db.set_default_decision_model_ids(["test/profile-primary"])
    created = client.post("/api/admin/decision-profiles", json=_payload(primary_model_id=""))
    assert created.status_code == 201
    assert created.get_json()["profile"]["primary_model_id"] == ""
    assert created.get_json()["profile"]["fallback_model_ids"] == []
