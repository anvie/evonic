"""Privacy, query, authorization, and failure-isolation coverage for Decision Activity."""
from dataclasses import dataclass

import pytest

from app import app
from models.db import db
from backend.tools.lib.decision_models import DecisionFieldResult, DecisionResult, ProviderMetadata, TimingMetadata
from backend.tools.lib.decision_runtime import DecisionRuntime, DecisionRuntimeError, RuntimeRequest


def _event(**overrides):
    event = {"source": "production", "profile_key": "application.example", "profile_version": "v1",
             "model_id": "test/model", "adapter_key": "test", "outcome_summary": {"approved": True},
             "confidence": 0.9, "latency_ms": 12, "attempts": 1, "created_at": 100.0}
    event.update(overrides)
    return event


def _client():
    client = app.test_client()
    with client.session_transaction() as session: session["authenticated"] = True
    return client


def test_activity_sanitizes_summary_bounds_and_supports_redaction_retention():
    event_id = db.record_decision_activity(_event(outcome_summary={"approved": True, "bad-key": "drop", "raw_response": "drop", "nested": {"raw": "drop"}, "note": "x" * 999, "comment": "token=drop"}, correlation_id="c" * 999))
    detail = db.get_decision_activity(event_id)
    assert detail["outcome_summary"] == {"approved": True, "note": "x" * 256}
    assert len(detail["correlation_id"]) == 256
    assert db.redact_decision_activity(event_id)
    assert db.get_decision_activity(event_id) is None
    old = db.record_decision_activity(_event(created_at=1.0))
    assert db.purge_decision_activity(2.0) == 1
    assert db.get_decision_activity(old) is None


def test_activity_query_is_deterministic_and_admin_authorized():
    first = db.record_decision_activity(_event(created_at=200.0, source="playground"))
    second = db.record_decision_activity(_event(created_at=200.0, source="profile_test", error_code="internal"))
    client = _client()
    page = client.get("/api/admin/decision-activity?limit=1")
    assert page.status_code == 200
    payload = page.get_json()
    assert len(payload["events"]) == 1 and payload["next_cursor"]
    next_page = client.get("/api/admin/decision-activity?limit=1&cursor=" + payload["next_cursor"])
    assert next_page.get_json()["events"][0]["id"] != payload["events"][0]["id"]
    filtered = client.get("/api/admin/decision-activity?source=profile_test&result_class=error")
    assert [item["id"] for item in filtered.get_json()["events"]] == [second]
    assert client.get("/api/admin/decision-activity/" + first).status_code == 200
    assert app.test_client().get("/api/admin/decision-activity").status_code == 401


class _RuntimeDB:
    def __init__(self): self.events = []
    def get_decision_profile(self, key):
        return {"profile_key": key, "enabled": True, "mode": "enforce", "policy_version": "v1", "schema_version": "decision-schema/v1", "schema": {"version": "decision-schema/v1", "fields": [{"key": "approved", "kind": "boolean"}]}, "primary_model_id": "test/model", "fallback_model_id": None, "timeout_ms": 1000, "retry_limit": 0, "minimum_confidence": 0}
    def validate_decision_profile(self, data, *, existing=None): return data
    def get_model_by_id(self, model_id): return {"id": model_id, "provider": "test", "model_name": "model", "capabilities": {"decision": {"enabled": True, "schema_versions": ["decision-schema/v1"], "structured_output": True, "schema_primitives": ["boolean"]}}}
    def record_decision_activity(self, event): raise OSError("telemetry unavailable")


class _Adapter:
    key = "test"
    def decide(self, invocation):
        return DecisionResult(invocation.profile, invocation.schema.version, (DecisionFieldResult("approved", True, .9),), ProviderMetadata("test", "test/model"), TimingMetadata(1), 1)


def test_activity_write_failure_never_changes_runtime_result():
    runtime = DecisionRuntime(_RuntimeDB(), adapter_factory=lambda model: _Adapter())
    result = runtime.invoke(RuntimeRequest("application.example", "v1", {"safe": "input"}, activity_source="playground"))
    assert result.error is None and result.fields[0].value is True


# ── recorded inputs: reproduce an event in the Playground ────────────

def test_the_input_is_recorded_returned_in_detail_and_kept_out_of_the_list():
    context = {"incident": "Payment outage", "count": 3, "tags": ["a", "b"]}
    event_id = db.record_decision_activity(_event(input_context=context))
    detail = _client().get("/api/admin/decision-activity/" + event_id).get_json()["event"]
    assert detail["input_context"] == context and detail["input_status"] == "recorded" and detail["has_input"] is True
    listed = [e for e in _client().get("/api/admin/decision-activity?limit=100").get_json()["events"] if e["id"] == event_id][0]
    assert "input_context" not in listed and "input_json" not in listed and listed["has_input"] is True


def test_inputs_that_look_like_credentials_or_are_too_large_are_withheld_with_a_reason():
    secret_key = db.record_decision_activity(_event(input_context={"api_key": "abc"}))
    secret_value = db.record_decision_activity(_event(input_context={"note": "my password: hunter2"}))
    nested = db.record_decision_activity(_event(input_context={"a": [{"token": "x"}]}))
    huge = db.record_decision_activity(_event(input_context={"blob": "x" * 20000}))
    for event_id, status in ((secret_key, "withheld"), (secret_value, "withheld"), (nested, "withheld"), (huge, "too_large")):
        detail = db.get_decision_activity(event_id)
        assert detail["input_context"] is None and detail["input_status"] == status and detail["has_input"] is False


def test_events_without_an_input_say_so():
    detail = db.get_decision_activity(db.record_decision_activity(_event()))
    assert detail["input_context"] is None and detail["input_status"] is None


def test_recording_inputs_can_be_switched_off_and_back_on():
    client = _client()
    assert client.get("/api/admin/decision-activity/settings").get_json() == {"record_input": True}
    assert client.put("/api/admin/decision-activity/settings", json={"record_input": False}).get_json()["success"] is True
    off = db.record_decision_activity(_event(input_context={"x": 1}))
    assert db.get_decision_activity(off)["input_context"] is None and db.get_decision_activity(off)["input_status"] == "off"
    assert client.put("/api/admin/decision-activity/settings", json={"record_input": True}).status_code == 200
    on = db.record_decision_activity(_event(input_context={"x": 1}))
    assert db.get_decision_activity(on)["input_context"] == {"x": 1}
    assert client.put("/api/admin/decision-activity/settings", json={"record_input": "yes"}).status_code == 400
    assert app.test_client().get("/api/admin/decision-activity/settings").status_code == 401


def test_redaction_removes_the_recorded_input_too():
    event_id = db.record_decision_activity(_event(input_context={"secret_plan": "none"} if False else {"x": 1}))
    assert db.redact_decision_activity(event_id)
    with db._connect() as conn:
        row = conn.execute("SELECT input_json, redacted FROM decision_activity WHERE id = ?", (event_id,)).fetchone()
    assert row[0] is None and row[1] == 1


class _RecordingDB(_RuntimeDB):
    def __init__(self): super().__init__(); self.recorded = []
    def record_decision_activity(self, event): self.recorded.append(event)


def test_the_runtime_hands_the_validated_input_to_activity():
    store = _RecordingDB()
    runtime = DecisionRuntime(store, adapter_factory=lambda model: _Adapter())
    context = {"incident": "outage", "n": 2}
    runtime.invoke(RuntimeRequest("application.example", "v1", context, activity_source="production"))
    assert store.recorded[0]["input_context"] == context and store.recorded[0]["source"] == "production"


def test_a_request_refused_for_a_secret_never_reaches_the_activity_input():
    store = _RecordingDB()
    runtime = DecisionRuntime(store, adapter_factory=lambda model: _Adapter())
    with pytest.raises(DecisionRuntimeError) as refused:
        runtime.invoke(RuntimeRequest("application.example", "v1", {"note": "api_key=abc123"}, activity_source="production"))
    assert refused.value.code.value == "secret_detected"
    assert all(e.get("input_context") is None for e in store.recorded)            # refused before anything is recorded
