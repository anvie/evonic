"""DMSS activity detail: command preview redaction/cap and detection labelling."""

from types import SimpleNamespace

import pytest

from app import app
from backend.services import decim_safety_telemetry as telemetry


def _settings(**overrides):
    base = dict(enabled=True, mode="shadow", model_id="systemone/test-model", retention_days=30)
    base.update(overrides)
    return SimpleNamespace(**base)


def _record(code, deterministic, *, model_decision=None, decision_source="decim"):
    return telemetry.record_comparison(
        settings=_settings(),
        code=code,
        tool_type="bash",
        agent_context=None,
        deterministic=deterministic,
        model_decision=model_decision,
        confidence=0.91 if model_decision else None,
        latency_ms=120 if model_decision else None,
        fallback_reason=None if model_decision else "provider_unavailable",
        decision_source=decision_source,
        provider_key="systemone",
        model_id="test-model",
        policy_version="decim-v1",
        correlation_id="corr-1",
    )


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


# ---------------------------------------------------------------------------
# command_preview
# ---------------------------------------------------------------------------

def test_command_preview_redacts_env_assignment_secrets():
    preview = telemetry.command_preview(
        "curl -X POST api.internal -d 'password=hunter2&api_key=abcd1234' --token zzzz"
    )
    assert "hunter2" not in preview
    assert "abcd1234" not in preview
    assert "zzzz" not in preview
    assert "[REDACTED]" in preview


def test_command_preview_redacts_bearer_headers_and_url_credentials():
    preview = telemetry.command_preview(
        "curl -H 'Authorization: Bearer abcdefghijklmnop' "
        "https://deploy:s3cr3tpw@git.example.com/repo.git"
    )
    assert "abcdefghijklmnop" not in preview
    assert "s3cr3tpw" not in preview


def test_command_preview_redacts_private_key_blocks():
    preview = telemetry.command_preview(
        "cat > k.pem <<EOF\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\nEOF"
    )
    assert "MIIEow" not in preview
    assert "[REDACTED PRIVATE KEY]" in preview


def test_command_preview_collapses_whitespace():
    assert telemetry.command_preview("echo   a\n\n\t b") == "echo a b"


def test_command_preview_missing_text_is_empty():
    assert telemetry.command_preview("") == ""
    assert telemetry.command_preview(None) == ""
    assert telemetry.command_preview("   \n\t ") == ""


def test_command_preview_is_capped_at_1000_characters():
    preview = telemetry.command_preview("echo " + "a" * 5000)
    assert len(preview) <= telemetry.MAX_COMMAND_PREVIEW == 1000
    assert preview.endswith("… [truncated]")


def test_command_preview_redacts_secret_that_straddles_the_cap():
    """A secret crossing the truncation boundary must not survive partially."""
    body = "x " * 490 + "password=" + "p" * 200
    preview = telemetry.command_preview(body)
    assert len(preview) <= telemetry.MAX_COMMAND_PREVIEW
    assert "pppppppp" not in preview


# ---------------------------------------------------------------------------
# detection_label
# ---------------------------------------------------------------------------

def test_detection_label_uses_rule_categories():
    label = telemetry.detection_label(
        {"level": "dangerous", "blocked_patterns": ["sandbox_escape", "privilege_escalation"]},
        None,
    )
    assert label == "privilege_escalation,sandbox_escape"


def test_detection_label_is_model_when_only_the_model_flags():
    assert telemetry.detection_label({"blocked_patterns": []}, "review") == "model"
    assert telemetry.detection_label({"blocked_patterns": []}, "block") == "model"


def test_detection_label_is_none_for_a_clean_allow():
    assert telemetry.detection_label({"blocked_patterns": []}, "allow") == "none"
    assert telemetry.detection_label({"blocked_patterns": []}, None) == "none"


def test_detection_label_ignores_unaccepted_model_verdict():
    assert telemetry.detection_label({"blocked_patterns": []}, "block", accepted=False) == "none"


# ---------------------------------------------------------------------------
# record_comparison + API surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_type_command,decision_source", [("bash", "decim"), ("python", "deterministic")])
def test_record_comparison_persists_command_preview_and_detection(tool_type_command, decision_source):
    event_id = telemetry.record_comparison(
        settings=_settings(),
        code="rm -rf /tmp/x && echo password=s3cret",
        tool_type="bash",
        agent_context=None,
        deterministic={"level": "dangerous", "score": 90, "blocked_patterns": ["destructive_fs"]},
        model_decision=None,
        decision_source=decision_source,
        provider_key="systemone",
        model_id="test-model",
        policy_version="decim-v1",
        correlation_id="corr-2",
    )
    assert event_id

    event = telemetry.get_event(event_id)
    assert event["detection_type"] == "destructive_fs"
    assert event["detection_command"].startswith("rm -rf /tmp/x")
    assert "s3cret" not in event["detection_command"]
    assert event["command_length"] == len("rm -rf /tmp/x && echo password=s3cret")


def test_events_api_returns_populated_detection_fields():
    event_id = _record(
        "curl -s http://127.0.0.1:5000/api -H 'X-Token: abcdefghijklmnop'",
        {"level": "requires_approval", "score": 55, "blocked_patterns": ["network_egress"]},
        model_decision="review",
    )
    client = _client()

    listed = client.get("/api/admin/decim-safety/events").get_json()["events"]
    assert any(e["id"] == event_id for e in listed)
    row = next(e for e in listed if e["id"] == event_id)
    assert row["detection_type"] == "network_egress"
    assert "abcdefghijklmnop" not in row["detection_command"]

    detail = client.get(f"/api/admin/decim-safety/events/{event_id}").get_json()
    assert detail["detection_type"] == "network_egress"
    assert detail["detection_command"]
    assert detail["model_decision"] == "review"
    assert detail["deterministic_categories"] == "network_egress"
    assert detail["final_level"] == "requires_approval"
