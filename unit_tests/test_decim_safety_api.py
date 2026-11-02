"""HTTP-surface tests for Decim Safety settings and the admin telemetry API."""

import os
from unittest.mock import patch

import pytest

from app import app
from models.db import db


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    db.invalidate_settings_cache()
    yield
    db.invalidate_settings_cache()


@pytest.fixture(autouse=True)
def _clear_tester_rate_limit():
    """Give every tester test a fresh rate-limit window."""
    from routes import decim_safety

    decim_safety._reset_tester_rate_limit()
    yield
    decim_safety._reset_tester_rate_limit()


DMSS_PROVIDER = "systemone-test"
DMSS_MODEL = "systemone-test/d1"


@pytest.fixture(autouse=True)
def _decision_model():
    """Register a SystemOne decision model and select it for DMSS.

    Mirrors a configured install: the endpoint lives on the provider, and
    DMSS only references the model.
    """
    db.create_provider({"id": DMSS_PROVIDER, "name": "SystemOne test",
                        "base_url": "https://provider.example/decision",
                        "adapter_key": "systemone.questions.v1"})
    db.create_model({"id": DMSS_MODEL, "name": "d1", "type": "decision",
                     "provider": DMSS_PROVIDER, "model_name": "LiquidAI/d1-3B"})
    db.set_setting("decim_safety.model_id", DMSS_MODEL)
    db.invalidate_settings_cache()


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


# ---------------------------------------------------------------------------
# Settings surface
# ---------------------------------------------------------------------------

def test_settings_default_to_off_and_provider_neutral():
    client = _client()
    body = client.get("/api/settings/decim-safety").get_json()["settings"]
    assert body["enabled"] is False
    assert body["mode"] == "off"
    assert body["model_id"] == DMSS_MODEL
    # No endpoint/credential fields may leak through the public surface.
    assert "endpoint" not in body
    assert "model_allowlist" not in body


def test_settings_round_trip_writes_and_reads_back():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={
        "enabled": True,
        "mode": "shadow",
        "model_id": DMSS_MODEL,
        "request_timeout_ms": 2000,
        "minimum_confidence": 0.95,
        "max_payload_chars": 8000,
        "circuit_breaker_failures": 4,
        "circuit_breaker_cooldown_seconds": 120,
        "record_enforce_events": True,
        "retention_days": 14,
    })
    assert response.status_code == 200
    result = response.get_json()
    assert result["success"] is True
    assert result["settings"]["enabled"] is True
    assert result["settings"]["mode"] == "shadow"
    assert result["settings"]["minimum_confidence"] == 0.95

    assert db.get_setting("decim_safety.mode") == "shadow"
    assert db.get_setting("decim_safety.enabled") == "1"
    assert client.get("/api/settings/decim-safety").get_json()["settings"]["mode"] == "shadow"


def test_settings_reject_invalid_mode():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"mode": "turbo"})
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_settings_reject_unknown_or_non_decision_models():
    client = _client()
    db.create_model({"id": "openai/gpt-chat", "name": "chat", "type": "llm",
                     "provider": DMSS_PROVIDER, "model_name": "gpt"})
    for model_id in ("missing/model", "openai/gpt-chat", 5):
        response = client.put("/api/settings/decim-safety", json={"model_id": model_id})
        assert response.status_code == 400
    assert db.get_setting("decim_safety.model_id") == DMSS_MODEL


def test_settings_reject_decision_model_on_unsupported_protocol():
    client = _client()
    db.create_provider({"id": "openai-like", "name": "OpenAI-like",
                        "base_url": "https://llm.example/v1", "adapter_key": "openai.chat_completions.v1"})
    db.create_model({"id": "openai-like/judge", "name": "judge", "type": "decision",
                     "provider": "openai-like", "model_name": "judge"})
    response = client.put("/api/settings/decim-safety", json={"model_id": "openai-like/judge"})
    assert response.status_code == 400
    assert "protocol" in response.get_json()["error"]


def test_settings_reject_out_of_range_confidence():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"minimum_confidence": 1.5})
    assert response.status_code == 400


def test_settings_change_is_audited():
    client = _client()
    with patch("routes.settings.audit.log_setting_change") as logger:
        client.put("/api/settings/decim-safety", json={"mode": "enforce"})
    logger.assert_called_once()


# ---------------------------------------------------------------------------
# Admin telemetry API
# ---------------------------------------------------------------------------

def test_health_is_provider_neutral():
    client = _client()
    body = client.get("/api/admin/decim-safety/health").get_json()
    assert body["mode"] == "off"
    assert body["enabled"] is False
    assert "endpoint" not in body
    assert body["circuit_state"] in ("closed", "open", "half-open", "unknown")


def test_summary_and_events_start_empty():
    client = _client()
    summary = client.get("/api/admin/decim-safety/summary").get_json()
    assert summary["total_comparisons"] == 0
    events = client.get("/api/admin/decim-safety/events").get_json()
    assert events["events"] == []
    assert events["total"] == 0


def test_events_ignore_invalid_filters():
    """Invalid filter values are ignored server-side (never a 500 or injection)."""
    client = _client()
    response = client.get("/api/admin/decim-safety/events?mode=bogus&tool_type=java&final_level=nope")
    assert response.status_code == 200
    assert response.get_json()["events"] == []


def test_clear_telemetry_is_audited_and_rolls_epoch():
    from backend.services import decim_safety_telemetry

    client = _client()
    before = decim_safety_telemetry.get_state()["telemetry_epoch"]
    response = client.post("/api/admin/decim-safety/telemetry/clear")
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    after = decim_safety_telemetry.get_state()["telemetry_epoch"]
    assert after == before + 1


def test_event_detail_404_for_unknown_id():
    client = _client()
    assert client.get("/api/admin/decim-safety/events/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# General-tab persistence (task #837): partial PUT merge + resolver behaviour
# ---------------------------------------------------------------------------

def test_partial_put_preserves_unsent_fields():
    """The General tab PUTs only {enabled, mode}; every other field must survive."""
    client = _client()
    # Seed a distinctive value on a field the General tab does NOT send.
    client.put("/api/settings/decim-safety", json={"request_timeout_ms": 2500})

    # HMADS-only policy payload, exactly as the General tab sends it.
    response = client.put("/api/settings/decim-safety", json={"enabled": False, "mode": "off"})
    assert response.status_code == 200
    assert response.get_json()["success"] is True

    body = client.get("/api/settings/decim-safety").get_json()["settings"]
    assert body["enabled"] is False
    assert body["mode"] == "off"
    # Unsent field preserved (merge semantics => no data loss on round-trip).
    assert body["request_timeout_ms"] == 2500


def test_dmss_enabled_payload_round_trips():
    """The DMSS-enabled policy payload round-trips through the API intact."""
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"enabled": True, "mode": "enforce"})
    assert response.status_code == 200
    body = client.get("/api/settings/decim-safety").get_json()["settings"]
    assert body["enabled"] is True
    assert body["mode"] == "enforce"


def _fake_provider(calls, decision="allow", confidence=0.99):
    from backend.tools.lib.decim_safety import DecimDecision, POLICY_VERSION

    class FakeProvider:
        key = "systemone"

        def decide(self, packet, settings, route=None):
            calls.append(1)
            return DecimDecision(
                decision=decision, confidence=confidence, model_id="m-1",
                provider_key="systemone", policy_version=POLICY_VERSION,
                correlation_id="c-1", latency_ms=12,
            )

    return FakeProvider()


def test_resolver_hmads_only_never_calls_provider():
    """In 'HMADS only' (enabled=False / mode=off) the DMSS provider is never invoked."""
    from backend.tools.lib.decim_safety import DecimSafetyResolver, DecimSettings

    calls = []

    class NeverProvider:
        key = "systemone"

        def decide(self, packet, settings, route=None):
            calls.append(1)
            raise AssertionError("provider must not be called in HMADS-only mode")

    resolver = DecimSafetyResolver(providers={"systemone.questions.v1": NeverProvider()})
    for settings in (DecimSettings(enabled=False, mode="off"),
                     DecimSettings(enabled=True, mode="off")):
        result = resolver.resolve("echo hello", "bash", settings=settings)
        assert result.attempted is False
        assert result.decision is None
        assert result.fallback_reason == "disabled"
    assert calls == []


def test_resolver_dmss_enabled_calls_provider():
    """With DMSS enabled (shadow or enforce) the provider is invoked and its
    decision is returned (deterministic HMADS is the fallback only on failure)."""
    from backend.tools.lib.decim_safety import DecimSafetyResolver, DecimSettings

    calls = []
    resolver = DecimSafetyResolver(providers={"systemone.questions.v1": _fake_provider(calls)})
    for mode in ("shadow", "enforce"):
        settings = DecimSettings(enabled=True, mode=mode, minimum_confidence=0.90, model_id=DMSS_MODEL)
        result = resolver.resolve("echo hello", "bash", settings=settings)
        assert result.attempted is True
        assert result.decision is not None
        assert result.decision.decision == "allow"
        assert result.fallback_reason is None
    assert calls == [1, 1]


def test_resolver_low_confidence_falls_back_to_hmads():
    """A low-confidence provider decision is rejected => deterministic fallback."""
    from backend.tools.lib.decim_safety import DecimSafetyResolver, DecimSettings

    calls = []
    resolver = DecimSafetyResolver(providers={"systemone.questions.v1": _fake_provider(calls, confidence=0.40)})
    settings = DecimSettings(enabled=True, mode="enforce", minimum_confidence=0.90, model_id=DMSS_MODEL)
    result = resolver.resolve("echo hello", "bash", settings=settings)
    assert result.attempted is True
    assert result.decision is None
    assert result.fallback_reason == "low_confidence"
    assert calls == [1]

# ---------------------------------------------------------------------------
# DMSS decision model selection
# ---------------------------------------------------------------------------


def test_model_id_round_trips_and_canonicalizes():
    client = _client()
    client.put("/api/settings/decim-safety", json={"model_id": ""})
    response = client.put("/api/settings/decim-safety", json={"model_id": f" {DMSS_MODEL} "})
    assert response.status_code == 200
    assert response.get_json()["settings"]["model_id"] == DMSS_MODEL
    assert db.get_setting("decim_safety.model_id") == DMSS_MODEL
    # The provider endpoint is never part of the DMSS settings surface.
    assert "provider.example" not in str(client.get("/api/settings/decim-safety").get_json())


def test_model_id_can_be_cleared_with_empty_string():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"model_id": ""})
    assert response.status_code == 200
    assert response.get_json()["settings"]["model_id"] == ""
    assert db.get_setting("decim_safety.model_id") == ""


def test_health_stays_provider_neutral_without_endpoint():
    client = _client()
    endpoint = "https://provider.example/decision"
    body = client.get("/api/admin/decim-safety/health").get_json()
    assert body["latency_ms"] == {"count": 0, "p50": None, "p95": None, "max": None}
    # The health surface stays provider-neutral: it never echoes the endpoint.
    assert endpoint not in str(body)


# ---------------------------------------------------------------------------
# Bounded diagnostic tester (task #839)
# ---------------------------------------------------------------------------


def _fake_provider_class(calls, decision="allow", confidence=0.99, reason=None):
    """A drop-in provider class for the process-wide resolver default."""
    from backend.tools.lib.decim_safety import DecimDecision, POLICY_VERSION

    class FakeProvider:
        key = "systemone"

        def decide(self, packet, settings, route=None):
            calls.append(packet)
            return DecimDecision(
                decision=decision, confidence=confidence, model_id="m-1",
                provider_key="systemone", policy_version=POLICY_VERSION,
                correlation_id=packet.get("correlation_id"), latency_ms=12,
                reason=reason,
            )

    return FakeProvider


def test_tester_returns_provider_decision_with_timing():
    client = _client()
    calls = []
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class(calls)):
        response = client.post("/api/admin/decim-safety/test",
                               json={"payload": "echo hello", "tool_type": "bash"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["diagnostic"] is True
    assert body["decision"] == "allow"
    assert body["confidence"] == 0.99
    assert body["model_id"] == DMSS_MODEL
    assert body["model"] == "m-1"
    assert body["latency_ms"] == 12
    assert body["fallback_reason"] is None
    assert body["payload_chars"] == len("echo hello")
    assert body["tool_type"] == "bash"
    # Default settings are disabled: the probe still reaches the provider,
    # and the response says production is not using DMSS.
    assert body["dmss_active"] is False
    assert body["mode"] == "off"
    assert body["circuit_state"] in ("closed", "open")
    # The payload travelled as data inside the policy packet.
    assert calls and calls[0]["requested_code"] == "echo hello"
    assert calls[0]["tool_type"] == "bash"


def test_tester_low_confidence_flags_hmads_fallback():
    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([], confidence=0.40)):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hello"}).get_json()
    # The raw provider decision is still reported for diagnostics...
    assert body["decision"] == "allow"
    assert body["confidence"] == 0.40
    # ...but production would reject it as unusable (below the 0.90 minimum).
    assert body["fallback_reason"] == "low_confidence"
    assert "minimum" in body["reason"]


def test_tester_payload_bounded_by_max_payload_chars():
    client = _client()
    client.put("/api/settings/decim-safety", json={"max_payload_chars": 10})
    calls = []
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class(calls)):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "x" * 20}).get_json()
    assert body["decision"] is None
    assert body["fallback_reason"] == "payload_too_large"
    assert "10" in body["reason"]
    assert calls == []  # never sent to the provider


def test_tester_sensitive_payload_not_sent_to_provider():
    client = _client()
    calls = []
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class(calls)):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "export api_key=sk_abcdefghijklmnop"}).get_json()
    assert body["decision"] is None
    assert body["fallback_reason"] == "sensitive_payload"
    assert calls == []


def test_tester_sanitizes_provider_reason_paths():
    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([], reason="wrote to /home/user/secrets.txt")):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hello"}).get_json()
    assert body["reason"] == "wrote to [path]"
    assert "/home" not in body["reason"]


def test_tester_redacts_reason_containing_secrets():
    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([], reason="found api_key=sk_abcdefghijklmnop in output")):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hello"}).get_json()
    assert "redacted" in body["reason"].lower()
    assert "sk_abcdefghijklmnop" not in body["reason"]


def test_tester_rate_limited_per_session():
    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([])):
        for _ in range(10):
            assert client.post("/api/admin/decim-safety/test",
                               json={"payload": "echo hi"}).status_code == 200
        response = client.post("/api/admin/decim-safety/test",
                               json={"payload": "echo hi"})
    assert response.status_code == 429
    assert response.get_json()["retry_after"] > 0
    assert response.headers.get("Retry-After")


def test_tester_requires_payload_and_valid_tool():
    client = _client()
    assert client.post("/api/admin/decim-safety/test", json={}).status_code == 400
    assert client.post("/api/admin/decim-safety/test", json={"payload": "   "}).status_code == 400
    assert client.post("/api/admin/decim-safety/test", json={"payload": 5}).status_code == 400
    assert client.post("/api/admin/decim-safety/test",
                       json={"payload": "x", "tool_type": "java"}).status_code == 400


def test_tester_provider_timeout_maps_to_transport_error():
    """A provider request that times out (socket.timeout) is reported as transport_error."""
    import socket
    from backend.tools.lib.decim_safety import SystemOneDecisionProvider

    class _TimingOutOpener:
        def open(self, request, timeout=None):
            raise socket.timeout("read timed out")

    class TimingOutProvider(SystemOneDecisionProvider):
        def __init__(self, *, transport=None):
            super().__init__(
                             transport=transport or _TimingOutOpener())

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", TimingOutProvider):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hi"}).get_json()
    assert body["success"] is True
    assert body["decision"] is None
    assert body["fallback_reason"] == "transport_error"
    assert "timed out" in body["reason"].lower()


def test_tester_provider_http_error_maps_to_http_error():
    """A non-2xx provider response is reported as http_error (diagnostic, not 5xx)."""
    from backend.tools.lib.decim_safety import SystemOneDecisionProvider

    class _FakeResponse:
        status = 503

        def read(self, n=-1):
            return b"Service Unavailable"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeOpener:
        def open(self, request, timeout=None):
            return _FakeResponse()

    class Http503Provider(SystemOneDecisionProvider):
        def __init__(self, *, transport=None):
            super().__init__(
                             transport=transport or _FakeOpener())

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", Http503Provider):
        response = client.post("/api/admin/decim-safety/test",
                               json={"payload": "echo hi"})
    assert response.status_code == 200  # diagnostic outcome, not a server error
    body = response.get_json()
    assert body["decision"] is None
    assert body["fallback_reason"] == "http_error"
    assert "non-2xx" in body["reason"].lower()


def test_tester_malformed_provider_response_maps_to_invalid_response():
    """A 2xx response whose body is not a valid decision is reported as invalid_response."""
    from backend.tools.lib.decim_safety import SystemOneDecisionProvider

    class GarbageProvider(SystemOneDecisionProvider):
        def _read_response(self, request, settings):
            return b"not-json"

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", GarbageProvider):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hi"}).get_json()
    assert body["decision"] is None
    assert body["fallback_reason"] == "invalid_response"
    assert "malformed" in body["reason"].lower()


def test_tester_unconfigured_provider_reports_fallback():
    client = _client()
    client.put("/api/settings/decim-safety", json={"model_id": ""})
    body = client.post("/api/admin/decim-safety/test",
                       json={"payload": "echo hi"}).get_json()
    assert body["success"] is True
    assert body["decision"] is None
    assert body["fallback_reason"] == "provider_unconfigured"


def test_tester_does_not_pollute_telemetry():
    from backend.services import decim_safety_telemetry

    client = _client()
    before = decim_safety_telemetry.get_state()["total_recorded"]
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([])):
        client.post("/api/admin/decim-safety/test", json={"payload": "echo hi"})
    assert decim_safety_telemetry.get_state()["total_recorded"] == before
    assert decim_safety_telemetry.list_events(limit=200)["events"] == []


def test_tester_does_not_trip_production_circuit_breaker():
    from backend.tools.lib.decim_safety import DecimProviderError, get_decim_safety_resolver

    class FailingProvider:
        key = "systemone"

        def decide(self, packet, settings, route=None):
            raise DecimProviderError("transport_error")

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", FailingProvider):
        for _ in range(5):  # more than the default 3-failure breaker threshold
            body = client.post("/api/admin/decim-safety/test",
                               json={"payload": "echo hi"}).get_json()
            assert body["fallback_reason"] == "transport_error"
    # The probe uses an isolated resolver, so production decisions are untouched.
    assert get_decim_safety_resolver().breaker.state() == "closed"


def test_tester_response_does_not_include_provider_endpoint():
    client = _client()
    endpoint = "https://provider.internal.example:9443/decision/very-secret"
    db.update_provider(DMSS_PROVIDER, {"base_url": endpoint})
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([])):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hi"}).get_json()
    # The diagnostic response is provider-neutral: it never echoes the endpoint.
    assert endpoint not in str(body)
    assert "internal.example" not in str(body)


# ---------------------------------------------------------------------------
# Provider endpoint connectivity test (task #895)
# ---------------------------------------------------------------------------

def test_endpoint_test_reports_ok_for_a_healthy_provider():
    client = _client()
    calls = []
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class(calls)):
        response = client.post("/api/admin/decim-safety/endpoint/test", json={})
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["ok"] is True
    assert body["diagnostic"] is True
    assert body["decision"] == "allow"
    assert body["confidence"] == 0.99
    assert body["latency_ms"] == 12
    assert body["fallback_reason"] is None
    assert "valid decision" in body["message"].lower()
    # A fixed, minimal diagnostic payload was used (data, never executed).
    assert calls and calls[0]["requested_code"] == "echo ok"
    assert body["checked_at"]


def test_endpoint_test_probes_an_override_without_persisting_it():
    client = _client()
    client.put("/api/settings/decim-safety", json={"model_id": ""})
    calls = []
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class(calls)):
        body = client.post("/api/admin/decim-safety/endpoint/test",
                           json={"model_id": DMSS_MODEL}).get_json()
    assert body["ok"] is True
    assert body["model_id"] == DMSS_MODEL
    assert calls, "the override must reach the provider adapter"
    # The selection being tested never becomes configuration.
    assert db.get_setting("decim_safety.model_id", "") == ""


def test_endpoint_test_reports_unreachable_provider():
    from backend.tools.lib.decim_safety import DecimProviderError

    class FailingProvider:
        key = "systemone"

        def decide(self, packet, settings, route=None):
            raise DecimProviderError("transport_error")

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", FailingProvider):
        body = client.post("/api/admin/decim-safety/endpoint/test", json={}).get_json()
    assert body["success"] is True   # the request was processed
    assert body["ok"] is False       # ...and the verdict is a failure
    assert body["decision"] is None
    assert body["fallback_reason"] == "transport_error"
    assert "unreachable" in body["message"].lower()
    assert body["latency_ms"] is not None


def test_endpoint_test_reports_unconfigured_provider():
    client = _client()
    for override in ({}, {"model_id": "missing/model"}):
        if not override:
            client.put("/api/settings/decim-safety", json={"model_id": ""})
        body = client.post("/api/admin/decim-safety/endpoint/test", json=override).get_json()
        assert body["ok"] is False
        assert body["fallback_reason"] == "provider_unconfigured"
        assert "no usable decision model" in body["message"].lower()


def test_endpoint_test_rejects_invalid_override():
    client = _client()
    assert client.post("/api/admin/decim-safety/endpoint/test",
                       json={"model_id": 5}).status_code == 400
    assert client.post("/api/admin/decim-safety/endpoint/test",
                       data="nope", content_type="text/plain").status_code == 400


def test_endpoint_test_is_rate_limited_per_session():
    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([])):
        for _ in range(10):
            assert client.post("/api/admin/decim-safety/endpoint/test",
                               json={}).status_code == 200
        response = client.post("/api/admin/decim-safety/endpoint/test", json={})
    assert response.status_code == 429
    assert response.get_json()["ok"] is False
    assert response.get_json()["retry_after"] > 0
    assert response.headers.get("Retry-After")


def test_endpoint_test_does_not_trip_the_production_circuit_breaker():
    from backend.tools.lib.decim_safety import DecimProviderError, get_decim_safety_resolver

    class FailingProvider:
        key = "systemone"

        def decide(self, packet, settings, route=None):
            raise DecimProviderError("transport_error")

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", FailingProvider):
        for _ in range(5):  # beyond the default 3-failure breaker threshold
            assert client.post("/api/admin/decim-safety/endpoint/test",
                               json={}).get_json()["ok"] is False
    assert get_decim_safety_resolver().breaker.state() == "closed"


def test_endpoint_test_does_not_echo_the_endpoint():
    client = _client()
    endpoint = "https://provider.internal.example:9443/decision/very-secret"
    db.update_provider(DMSS_PROVIDER, {"base_url": endpoint})
    calls = []
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class(calls)):
        body = client.post("/api/admin/decim-safety/endpoint/test", json={}).get_json()
    assert body["ok"] is True
    assert endpoint not in str(body)
    assert "internal.example" not in str(body)


# ---------------------------------------------------------------------------
# Simplified telemetry table: agent, decided_by, verdict filters
# ---------------------------------------------------------------------------

def _record_event(*, agent_id, decision_source, model_decision, det_level, final_level):
    from types import SimpleNamespace
    from backend.services import decim_safety_telemetry

    return decim_safety_telemetry.record_comparison(
        settings=SimpleNamespace(enabled=True, mode="enforce", model_id=DMSS_MODEL, retention_days=30),
        code="echo hi", tool_type="bash", agent_context={"id": agent_id},
        deterministic={"level": det_level, "score": 0, "blocked_patterns": []},
        model_decision=model_decision, confidence=0.9 if model_decision else None,
        latency_ms=50, decision_source=decision_source, final_level=final_level,
        hmads_duration_ms=2,
    )


@pytest.fixture
def _telemetry_rows():
    from backend.services import decim_safety_telemetry

    decim_safety_telemetry.clear()
    with db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = 'dmss-test-agent'")
        conn.execute("INSERT INTO agents (id, name, system_prompt) VALUES ('dmss-test-agent', 'Atlas', '')")
        conn.commit()
    ids = {
        # Enforced DMSS block.
        "dmss_block": _record_event(agent_id="dmss-test-agent", decision_source="decim",
                                    model_decision="block", det_level="safe", final_level="dangerous"),
        # HMADS floor: DMSS allowed, the stricter rule won.
        "floor": _record_event(agent_id="dmss-test-agent", decision_source="decision_profile",
                               model_decision="allow", det_level="dangerous", final_level="dangerous"),
        # Shadow: DMSS observed, HMADS decided.
        "shadow": _record_event(agent_id="ghost-agent", decision_source="deterministic_shadow",
                                model_decision="review", det_level="safe", final_level="safe"),
    }
    yield ids
    decim_safety_telemetry.clear()
    with db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = 'dmss-test-agent'")
        conn.commit()


def test_events_carry_agent_name_and_decided_by(_telemetry_rows):
    events = {e["id"]: e for e in _client().get("/api/admin/decim-safety/events").get_json()["events"]}
    block = events[_telemetry_rows["dmss_block"]]
    assert block["agent_id"] == "dmss-test-agent" and block["agent_name"] == "Atlas"
    assert block["decided_by"] == "dmss"
    assert block["hmads_duration_ms"] == 2 and block["model_latency_ms"] == 50
    assert events[_telemetry_rows["floor"]]["decided_by"] == "hmads"
    shadow = events[_telemetry_rows["shadow"]]
    assert shadow["decided_by"] == "hmads"
    # A deleted / unknown agent keeps its id and has no name.
    assert shadow["agent_name"] is None and shadow["agent_id"] == "ghost-agent"


def test_events_filter_by_agent_decided_by_and_verdict(_telemetry_rows):
    client = _client()

    def ids(query):
        return {e["id"] for e in client.get("/api/admin/decim-safety/events?" + query).get_json()["events"]}

    assert ids("agent_id=dmss-test-agent") == {_telemetry_rows["dmss_block"], _telemetry_rows["floor"]}
    assert ids("decided_by=dmss") == {_telemetry_rows["dmss_block"]}
    assert ids("verdict=unsafe") == {_telemetry_rows["dmss_block"], _telemetry_rows["floor"]}
    assert ids("verdict=allowed") == {_telemetry_rows["shadow"]}
    assert ids("window_hours=24") == set(_telemetry_rows.values())


def test_agents_endpoint_lists_agents_with_events(_telemetry_rows):
    agents = _client().get("/api/admin/decim-safety/agents").get_json()["agents"]
    assert {"id": "dmss-test-agent", "name": "Atlas"} in agents
    assert {"id": "ghost-agent", "name": None} in agents


def test_summary_counts_verdicts_and_decided_by(_telemetry_rows):
    summary = _client().get("/api/admin/decim-safety/summary?window_hours=0").get_json()
    assert summary["final_verdicts"] == {"allowed": 1, "approval": 0, "blocked": 2}
    assert summary["decided_by"] == {"dmss": 1, "hmads": 2}
    agent = _client().get("/api/admin/decim-safety/summary?agent_id=dmss-test-agent").get_json()
    assert agent["total_comparisons"] == 2


def test_hmads_floor_round_trips_through_the_settings_api():
    client = _client()
    assert client.get("/api/settings/decim-safety").get_json()["settings"]["hmads_floor"] is False

    response = client.put("/api/settings/decim-safety", json={"hmads_floor": True})

    assert response.status_code == 200
    assert response.get_json()["settings"]["hmads_floor"] is True
    assert db.get_setting("decim_safety.hmads_floor") == "1"
    assert client.get("/api/settings/decim-safety").get_json()["settings"]["hmads_floor"] is True


def test_hmads_floor_rejects_non_boolean_values():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"hmads_floor": 3.5})
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_health_reports_recording_policy_fields():
    client = _client()
    body = client.get("/api/admin/decim-safety/health").get_json()
    assert body["recording"] is False
    assert body["record_enforce_events"] is False
    assert body["hmads_floor"] is False
    assert "telemetry_error" in body
