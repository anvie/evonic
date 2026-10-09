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
    assert body["provider"] == "systemone"
    # No endpoint/credential fields may leak through the public surface.
    assert "endpoint" not in body
    assert "model_allowlist" not in body


def test_settings_round_trip_writes_and_reads_back():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={
        "enabled": True,
        "mode": "shadow",
        "provider": "systemone",
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


def test_settings_reject_unregistered_provider():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"provider": "vendor-x"})
    assert response.status_code == 400


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

        def decide(self, packet, settings):
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

        def decide(self, packet, settings):
            calls.append(1)
            raise AssertionError("provider must not be called in HMADS-only mode")

    resolver = DecimSafetyResolver(providers={"systemone": NeverProvider()})
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
    resolver = DecimSafetyResolver(providers={"systemone": _fake_provider(calls)})
    for mode in ("shadow", "enforce"):
        settings = DecimSettings(enabled=True, mode=mode, minimum_confidence=0.90)
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
    resolver = DecimSafetyResolver(providers={"systemone": _fake_provider(calls, confidence=0.40)})
    settings = DecimSettings(enabled=True, mode="enforce", minimum_confidence=0.90)
    result = resolver.resolve("echo hello", "bash", settings=settings)
    assert result.attempted is True
    assert result.decision is None
    assert result.fallback_reason == "low_confidence"
    assert calls == [1]

# ---------------------------------------------------------------------------
# Secure DMSS provider configuration (task #838)
# ---------------------------------------------------------------------------


def test_provider_endpoint_is_redacted_in_get_and_put_response():
    """The endpoint is persistable, but no API response may expose it verbatim."""
    client = _client()
    secret = "https://provider.internal.example:9443/decision/very-secret"
    response = client.put("/api/settings/decim-safety", json={"provider_endpoint": secret})
    assert response.status_code == 200
    returned = response.get_json()["settings"]["provider_endpoint"]
    assert returned != secret
    assert secret not in returned
    assert "…" in returned

    body = client.get("/api/settings/decim-safety").get_json()["settings"]
    assert body["provider_endpoint"] == returned
    assert secret not in str(body)
    # The stored setting remains the original secret, never the masked value.
    assert db.get_setting("decim_safety.provider_endpoint") == secret


def test_masked_provider_endpoint_put_preserves_stored_secret():
    """Echoing the GET mask back through PUT must never overwrite the secret."""
    client = _client()
    secret = "https://provider.internal.example:9443/decision/very-secret"
    client.put("/api/settings/decim-safety", json={"provider_endpoint": secret})
    masked = client.get("/api/settings/decim-safety").get_json()["settings"]["provider_endpoint"]

    response = client.put("/api/settings/decim-safety", json={
        "provider_endpoint": masked,
        "request_timeout_ms": 2400,
    })
    assert response.status_code == 200
    assert db.get_setting("decim_safety.provider_endpoint") == secret
    assert db.get_setting("decim_safety.request_timeout_ms") == "2400"


def test_provider_endpoint_rejects_non_http_url():
    client = _client()
    response = client.put("/api/settings/decim-safety", json={"provider_endpoint": "file:///etc/passwd"})
    assert response.status_code == 400
    assert "absolute http(s) URL" in response.get_json()["error"]


def test_health_exposes_aggregate_latency_not_endpoint_secret():
    client = _client()
    secret = "https://provider.internal.example:9443/decision/very-secret"
    client.put("/api/settings/decim-safety", json={"provider_endpoint": secret})
    body = client.get("/api/admin/decim-safety/health").get_json()
    assert body["latency_ms"] == {"count": 0, "p50": None, "p95": None, "max": None}
    assert secret not in str(body)
    assert "internal.example" not in str(body)


# ---------------------------------------------------------------------------
# Bounded diagnostic tester (task #839)
# ---------------------------------------------------------------------------


def _fake_provider_class(calls, decision="allow", confidence=0.99, reason=None):
    """A drop-in provider class for the process-wide resolver default."""
    from backend.tools.lib.decim_safety import DecimDecision, POLICY_VERSION

    class FakeProvider:
        key = "systemone"

        def decide(self, packet, settings):
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
    assert body["provider"] == "systemone"
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


def test_tester_unconfigured_provider_reports_fallback():
    client = _client()
    with patch.dict(os.environ, {"DECIM_SAFETY_SYSTEMONE_ENDPOINT": ""}):
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

        def decide(self, packet, settings):
            raise DecimProviderError("transport_error")

    client = _client()
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider", FailingProvider):
        for _ in range(5):  # more than the default 3-failure breaker threshold
            body = client.post("/api/admin/decim-safety/test",
                               json={"payload": "echo hi"}).get_json()
            assert body["fallback_reason"] == "transport_error"
    # The probe uses an isolated resolver, so production decisions are untouched.
    assert get_decim_safety_resolver().breaker.state() == "closed"


def test_tester_response_never_leaks_endpoint_secret():
    client = _client()
    secret = "https://provider.internal.example:9443/decision/very-secret"
    client.put("/api/settings/decim-safety", json={"provider_endpoint": secret})
    with patch("backend.tools.lib.decim_safety.SystemOneDecisionProvider",
               _fake_provider_class([])):
        body = client.post("/api/admin/decim-safety/test",
                           json={"payload": "echo hi"}).get_json()
    assert secret not in str(body)
    assert "internal.example" not in str(body)
