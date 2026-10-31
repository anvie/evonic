"""Focused HTTP tests for the bounded, side-effect-free Decision Playground."""
from __future__ import annotations

from backend.tools.lib.decision_models import (
    DecisionFieldResult, DecisionProfileRef, DecisionResult, ProviderMetadata,
    TimingMetadata,
)
from models.db import db
from routes import decision_playground
from app import app


def _model(model_id="test/playground"):
    return db.create_model({
        "id": model_id, "name": model_id, "type": "decision", "provider": "test",
        "model_name": "playground", "enabled": True,
        "capabilities": {"decision": {"enabled": True, "structured_output": True,
            "schema_versions": ["decision-schema/v1"],
            "schema_primitives": ["choice", "boolean", "score"]}},
    })


def _profile():
    return db.create_decision_profile({
        "profile_key": "application.playground", "display_name": "Playground profile",
        "description": "", "enabled": True, "mode": "enforce",
        "schema_version": "decision-schema/v1", "policy_version": "policy/v1",
        "schema": {"version": "decision-schema/v1", "fields": [{"key": "approved", "kind": "boolean"}]},
        "primary_model_id": "test/playground", "fallback_model_id": None,
        "timeout_ms": 1000, "retry_limit": 0, "minimum_confidence": 0,
    })


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


class _Runtime:
    """No provider/network call; captures the immutable runtime request only."""
    calls = []

    def __init__(self, runtime_db, **_kwargs):
        self.runtime_db = runtime_db

    def invoke(self, request):
        self.__class__.calls.append((self.runtime_db, request))
        profile = self.runtime_db.get_decision_profile(request.profile_key)
        fields = tuple(DecisionFieldResult(field["key"], True, 0.9) for field in profile["schema"]["fields"])
        return DecisionResult(DecisionProfileRef(request.profile_key, request.profile_version), "decision-schema/v1", fields,
                              ProviderMetadata("test-adapter", profile["primary_model_id"]), TimingMetadata(4, 3), 1)


def test_direct_playground_uses_ephemeral_config_and_returns_sanitized_result(monkeypatch):
    _model()
    monkeypatch.setattr(decision_playground, "DecisionRuntime", _Runtime)
    before = db.list_decision_profiles()
    response = _client().post("/api/admin/decision-playground/run", json={
        "mode": "direct", "model_id": "test/playground",
        "schema": {"version": "decision-schema/v1", "fields": [
            {"key": "priority", "kind": "choice", "choices": ["HIGH", "LOW"]},
            {"key": "requires_review", "kind": "boolean"},
        ]},
        "input_context": {"incident": "Synthetic outage"},
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["result"]["profile"]["key"] == "playground.direct"
    assert body["result"]["display"] == {"candidate_scores": "unavailable", "provider_logits": "unavailable"}
    assert db.list_decision_profiles() == before
    runtime_db, request = _Runtime.calls[-1]
    assert runtime_db.get_decision_profile("playground.direct")["primary_model_id"] == "test/playground"
    assert request.activity_source == "playground"


def test_profile_playground_reuses_saved_profile_without_mutating_it(monkeypatch):
    _model(); profile = _profile()
    monkeypatch.setattr(decision_playground, "DecisionRuntime", _Runtime)
    response = _client().post("/api/admin/decision-playground/run", json={
        "mode": "profile", "profile_key": profile["profile_key"], "input_context": {"incident": "Synthetic outage"},
    })
    assert response.status_code == 200
    assert db.get_decision_profile(profile["profile_key"]) == profile
    assert _Runtime.calls[-1][1].activity_source == "profile_test"


def test_playground_rejects_secret_like_context_and_unsupported_schema():
    _model()
    client = _client()
    secret = client.post("/api/admin/decision-playground/run", json={
        "mode": "direct", "model_id": "test/playground",
        "schema": {"version": "decision-schema/v1", "fields": [{"key": "approved", "kind": "boolean"}]},
        "input_context": {"authorization": "Bearer should-not-echo"},
    })
    assert secret.status_code == 400
    assert secret.get_json()["error"]["code"] == "secret_detected"
    assert "should-not-echo" not in secret.get_data(as_text=True)
    invalid = client.post("/api/admin/decision-playground/run", json={
        "mode": "direct", "model_id": "test/playground",
        "schema": {"version": "decision-schema/v1", "fields": [{"key": "nested", "kind": "object"}]},
        "input_context": {},
    })
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_kind"


def test_playground_requires_authentication():
    response = app.test_client().post("/api/admin/decision-playground/run", json={})
    assert response.status_code == 401
