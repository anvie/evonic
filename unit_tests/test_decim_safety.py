"""Tests for Decim Safety: provider resolution, source-aware pipeline, telemetry.

These tests never touch the network.  The provider adapter is exercised with an
injected fake transport, and the resolver is exercised with injected providers so
both the accepted decisions and every typed fallback path can be asserted.

Decim Safety is a *provider-agnostic* decision-model layer: the public names here
(``decim_safety``, ``systemone`` provider key, ``DecimDecision``) are stable and
must stay free of vendor-specific naming.
"""

import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.tools.lib.decim_safety import (  # noqa: E402
    POLICY_VERSION,
    DecimCircuitBreaker,
    DecimDecision,
    DecimProviderError,
    DecimSafetyResolver,
    DecimSettings,
    SystemOneDecisionProvider,
    build_policy_packet,
    fingerprint_code,
    validate_decim_settings,
)
from backend.tools.lib.safety_pipeline import SafetyPipeline  # noqa: E402

# A script that deterministic HMADS flags as at least requires_approval.
UNSAFE_BASH = "shred -u /etc/hosts"
SAFE_BASH = "echo hello"


def _decision(choice: str, confidence: float = 0.99) -> DecimDecision:
    return DecimDecision(
        decision=choice,
        confidence=confidence,
        model_id="model-x",
        provider_key="systemone",
        policy_version=POLICY_VERSION,
        correlation_id=None,
        latency_ms=12,
    )


class _Provider:
    """A scripted provider returning a fixed decision or raising an error."""

    key = "systemone"

    def __init__(self, result):
        self._result = result

    def decide(self, packet, settings, route=None):
        if isinstance(self._result, Exception):
            raise self._result
        if self._result.correlation_id is None:
            return replace(self._result, correlation_id=packet.get("correlation_id"))
        return self._result


def _settings(**overrides) -> DecimSettings:
    base = dict(enabled=True, mode="shadow", model_id="systemone/test-model",
                minimum_confidence=0.9, retention_days=30)
    base.update(overrides)
    return DecimSettings(**base)


ADAPTER = "systemone.questions.v1"


def _route(endpoint="http://127.0.0.1:9/decide", api_key="", enabled=True, adapter=ADAPTER):
    """A stand-in for the registry's ResolvedModelRoute."""
    return SimpleNamespace(
        model_id="systemone/test-model", enabled=enabled,
        credentials={"api_key": api_key} if api_key else {},
        provider=SimpleNamespace(provider_id="systemone", endpoint=endpoint, enabled=True,
                                 adapter_key=SimpleNamespace(value=adapter)),
    )


def _resolver(providers, route=None, **kwargs):
    """Resolver whose selected model routes to the given protocol client."""
    route = route or _route()
    return DecimSafetyResolver({ADAPTER: p for p in providers.values()},
                               route_resolver=lambda _model_id: route, **kwargs)


# ---------------------------------------------------------------------------
# Policy packet + parsing
# ---------------------------------------------------------------------------

def test_packet_rejects_sensitive_payload():
    with pytest.raises(DecimProviderError) as exc:
        build_policy_packet("export API_KEY=abcdef0123456789", "bash", "sandboxed_docker", 12000)
    assert exc.value.category == "sensitive_payload"


def test_packet_rejects_oversized_payload_without_truncation():
    with pytest.raises(DecimProviderError) as exc:
        build_policy_packet("x" * 50, "bash", "sandboxed_docker", 10)
    assert exc.value.category == "payload_too_large"


def test_packet_contains_only_bounded_fields():
    packet = build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000)
    assert packet["policy_version"] == POLICY_VERSION
    assert packet["tool_type"] == "bash"
    assert packet["execution_boundary"] == "sandboxed_docker"
    assert packet["requested_code"] == "echo hi"
    assert packet["decision_schema"] == {"allowed": ["allow", "review", "block"]}
    # No agent/context leakage.
    assert "agent_context" not in packet


def test_fingerprint_is_stable_and_not_reversible():
    assert fingerprint_code("echo hi") == fingerprint_code("echo hi")
    assert fingerprint_code("echo hi") != fingerprint_code("echo bye")


# ---------------------------------------------------------------------------
# Resolver behaviour
# ---------------------------------------------------------------------------

def test_resolver_accepts_confident_decision():
    resolver = _resolver({"systemone": _Provider(_decision("allow"))})
    result = resolver.resolve("echo hi", "bash", {}, _settings(mode="enforce"))
    assert result.attempted is True
    assert result.decision is not None
    assert result.decision.decision == "allow"
    assert result.decision.correlation_id


def test_resolver_falls_back_on_low_confidence():
    resolver = _resolver({"systemone": _Provider(_decision("allow", 0.4))})
    result = resolver.resolve("echo hi", "bash", {}, _settings(mode="enforce"))
    assert result.attempted is True
    assert result.decision is None
    assert result.fallback_reason == "low_confidence"


def test_resolver_isolates_provider_exceptions():
    resolver = _resolver({"systemone": _Provider(RuntimeError("boom"))})
    result = resolver.resolve("echo hi", "bash", {}, _settings(mode="enforce"))
    assert result.attempted is True
    assert result.decision is None
    assert result.fallback_reason == "provider_error"


def test_resolver_is_disabled_when_off_or_disabled():
    resolver = _resolver({"systemone": _Provider(_decision("block"))})
    assert resolver.resolve("echo hi", "bash", {}, _settings(enabled=False)).attempted is False
    assert resolver.resolve("echo hi", "bash", {}, _settings(mode="off")).attempted is False


def test_resolver_ignores_out_of_scope_tools():
    resolver = _resolver({"systemone": _Provider(_decision("block"))})
    result = resolver.resolve("echo hi", "file_write", {}, _settings())
    assert result.attempted is False
    assert result.fallback_reason == "out_of_scope"


def test_circuit_breaker_opens_then_half_opens():
    clock = {"t": 1000.0}
    breaker = DecimCircuitBreaker(clock=lambda: clock["t"])
    resolver = _resolver(
        {"systemone": _Provider(DecimProviderError("transport_error"))},
        breaker=breaker,
    )
    settings = _settings(mode="enforce", circuit_breaker_failures=2,
                         circuit_breaker_cooldown_seconds=30)

    assert resolver.resolve("x", "bash", {}, settings).fallback_reason == "transport_error"
    assert resolver.resolve("x", "bash", {}, settings).fallback_reason == "transport_error"
    # Breaker now open: no attempt, no provider call.
    opened = resolver.resolve("x", "bash", {}, settings)
    assert opened.attempted is False
    assert opened.fallback_reason == "circuit_open"
    # After the cooldown a single probe is permitted again.
    clock["t"] += 31
    assert resolver.resolve("x", "bash", {}, settings).fallback_reason == "transport_error"


# ---------------------------------------------------------------------------
# Provider adapter (transport injected; no sockets)
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeTransport:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.opened = []

    def open(self, request, timeout=None):
        self.opened.append(request)
        return _Response(self._payload, self._status)


class _BoundAdapter:
    """The real SystemOne client, called with a fixed model route."""

    def __init__(self, transport, route=None):
        self._provider = SystemOneDecisionProvider(transport=transport)
        self._route = route or _route()

    def decide(self, packet, settings):
        return self._provider.decide(packet, settings, self._route)


def _adapter(transport, route=None):
    return _BoundAdapter(transport, route)


def test_adapter_parses_typed_choice_response():
    import json
    payload = json.dumps({
        "answers": {"decision": {"choice": "block", "confidence": 0.97}},
        "model_id": "nimble-ish",
    }).encode()
    decision = _adapter(_FakeTransport(payload)).decide(
        build_policy_packet("rm -rf /", "bash", "sandboxed_docker", 12000), _settings())
    assert decision.decision == "block"
    assert decision.confidence == pytest.approx(0.97)
    assert decision.provider_key == "systemone"


def test_adapter_takes_conservative_confidence_from_probabilities():
    import json
    payload = json.dumps({
        "answers": {"decision": {
            "choice": "allow",
            "confidence": 0.99,
            "probabilities": {"allow": 0.55, "review": 0.25, "block": 0.20},
        }},
    }).encode()
    decision = _adapter(_FakeTransport(payload)).decide(
        build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert decision.confidence == pytest.approx(0.55)


def test_adapter_prefers_answer_confidence_over_entropy_confidence():
    """`confidence` on a Laya-shaped response is entropy, not max(p).

    Gating on the entropy number would demand max(p) of roughly 0.98 for a
    three-option question, so the calibrated `answer_confidence` must win.
    Values below are a real response for `rm -rf /var/www/html`.
    """
    import json
    payload = json.dumps({
        "answers": {"decision": {
            "type": "choice",
            "choice": "block",
            "probabilities": {"allow": 0.3233, "review": 0.0266, "block": 0.65},
            "confidence": 0.325,
            "answer_confidence": 0.65,
        }},
        "usage": {"truncated": False, "state_tokens_dropped": 0},
    }).encode()
    decision = _adapter(_FakeTransport(payload)).decide(
        build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert decision.decision == "block"
    assert decision.confidence == pytest.approx(0.65)


def test_adapter_still_minimises_answer_confidence_against_distribution():
    """A provider cannot report more confidence than its own probabilities."""
    import json
    payload = json.dumps({
        "answers": {"decision": {
            "choice": "allow",
            "confidence": 0.99,
            "answer_confidence": 0.99,
            "probabilities": {"allow": 0.55, "review": 0.25, "block": 0.20},
        }},
    }).encode()
    decision = _adapter(_FakeTransport(payload)).decide(
        build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert decision.confidence == pytest.approx(0.55)


def test_adapter_tolerates_rounded_probability_sum():
    """Four-decimal rounding can push a valid distribution just over 1.0."""
    import json
    payload = json.dumps({
        "answers": {"decision": {
            "choice": "block",
            "probabilities": {"allow": 0.3334, "review": 0.3333, "block": 0.3334},
        }},
    }).encode()
    decision = _adapter(_FakeTransport(payload)).decide(
        build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert decision.decision == "block"


def test_adapter_abstains_when_provider_truncated_the_state():
    """A decision read from a partial state must not be trusted.

    The tail of a script is right-truncated, and that is exactly where a
    destructive command usually sits.
    """
    import json
    payload = json.dumps({
        "answers": {"decision": {"choice": "allow", "confidence": 0.99,
                                 "probabilities": {"allow": 0.99}}},
        "usage": {"truncated": True, "state_tokens_dropped": 169},
    }).encode()
    with pytest.raises(DecimProviderError) as exc:
        _adapter(_FakeTransport(payload)).decide(
            build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert exc.value.category == "truncated_state"


def test_adapter_abstains_when_dropped_tokens_reported_without_flag():
    """`state_tokens_dropped` alone is enough; the boolean may be absent."""
    import json
    payload = json.dumps({
        "answers": {"decision": {"choice": "allow", "confidence": 0.99,
                                 "probabilities": {"allow": 0.99}}},
        "usage": {"state_tokens_dropped": 827},
    }).encode()
    with pytest.raises(DecimProviderError) as exc:
        _adapter(_FakeTransport(payload)).decide(
            build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert exc.value.category == "truncated_state"


def test_resolver_falls_back_when_state_was_truncated():
    class _TruncatingProvider:
        key = "systemone"

        def decide(self, packet, settings, route):
            raise DecimProviderError("truncated_state")

    resolver = _resolver({"systemone": _TruncatingProvider()})
    result = resolver.resolve("echo hi", "bash", {}, _settings(mode="enforce"))
    assert result.attempted is True
    assert result.decision is None
    assert result.fallback_reason == "truncated_state"


def test_adapter_rejects_malformed_response():
    with pytest.raises(DecimProviderError) as exc:
        _adapter(_FakeTransport(b"not-json")).decide(
            build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert exc.value.category == "invalid_response"


def test_adapter_requires_configured_endpoint():
    with pytest.raises(DecimProviderError) as exc:
        SystemOneDecisionProvider().decide(
            build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings(), _route(endpoint=""))
    assert exc.value.category == "provider_unconfigured"


def test_adapter_sends_provider_credentials_from_the_model_route():
    import json
    transport = _FakeTransport(json.dumps({"answers": {"decision": {"choice": "allow", "confidence": 0.99}}}).encode())
    decision = _adapter(transport, _route(api_key="s1-key")).decide(
        build_policy_packet("echo hi", "bash", "sandboxed_docker", 12000), _settings())
    assert transport.opened[0].get_header("Authorization") == "Bearer s1-key"
    assert transport.opened[0].full_url == "http://127.0.0.1:9/decide"
    # Without a provider-reported model id, the registry model id is used.
    assert decision.model_id == "systemone/test-model"


@pytest.mark.parametrize("settings, route", [
    (_settings(model_id=""), _route()),
    (_settings(), None),
    (_settings(), _route(enabled=False)),
    (_settings(), _route(adapter="openai.chat_completions.v1")),
])
def test_resolver_falls_back_without_a_usable_decision_model(settings, route):
    resolver = DecimSafetyResolver({ADAPTER: _Provider(_decision("allow"))},
                                   route_resolver=lambda _model_id: route)
    result = resolver.resolve("echo hi", "bash", {}, replace(settings, mode="enforce"))
    assert result.decision is None
    assert result.fallback_reason == "provider_unconfigured"


# ---------------------------------------------------------------------------
# Source-aware pipeline
# ---------------------------------------------------------------------------

def _pipeline(provider) -> SafetyPipeline:
    pipeline = SafetyPipeline()
    pipeline._decim_resolver = _resolver({"systemone": provider})
    return pipeline


def test_deterministic_result_is_unchanged_and_labelled():
    result = SafetyPipeline().check(SAFE_BASH, tool_type="bash")
    assert result["level"] == "safe"
    assert result["decision_source"] == "deterministic"
    assert set(result) >= {"level", "score", "reasons", "blocked_patterns",
                           "requires_approval", "approval_info"}


def test_shadow_preserves_deterministic_verdict_and_attaches_evidence():
    pipeline = _pipeline(_Provider(_decision("allow")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(mode="shadow"))
    assert result["level"] != "safe"                 # deterministic verdict governs
    assert result["decision_source"] == "deterministic_shadow"
    assert result["decim_safety"]["model_decision"] == "allow"
    assert result["decim_safety"]["accepted"] is True


def test_enforce_valid_allow_overrides_deterministic_result():
    pipeline = _pipeline(_Provider(_decision("allow")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(mode="enforce"))
    assert result["level"] == "safe"
    assert result["decision_source"] == "decim"
    assert result["requires_approval"] is False


def test_enforce_valid_block_is_dangerous():
    pipeline = _pipeline(_Provider(_decision("block")))
    result = pipeline.check(SAFE_BASH, tool_type="bash", decim_settings=_settings(mode="enforce"))
    assert result["level"] == "dangerous"
    assert result["decision_source"] == "decim"


def test_enforce_valid_review_requires_approval():
    pipeline = _pipeline(_Provider(_decision("review")))
    result = pipeline.check(SAFE_BASH, tool_type="bash", decim_settings=_settings(mode="enforce"))
    assert result["level"] == "requires_approval"
    assert result["requires_approval"] is True
    assert result["approval_info"]


def test_enforce_fallback_preserves_deterministic_result():
    pipeline = _pipeline(_Provider(_decision("allow", 0.1)))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(mode="enforce"))
    assert result["level"] != "safe"
    assert result["decision_source"] == "deterministic_fallback"
    assert result["decim_safety"]["fallback_reason"] == "low_confidence"


def test_disabled_decim_skips_model_entirely():
    pipeline = _pipeline(_Provider(RuntimeError("must not be called")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(enabled=False))
    assert result["decision_source"] == "deterministic"
    assert "decim_safety" not in result


# ---------------------------------------------------------------------------
# Telemetry persistence
# ---------------------------------------------------------------------------

def _events():
    from backend.services import decim_safety_telemetry as telemetry
    return telemetry


def test_shadow_always_records_comparison_evidence():
    telemetry = _events()
    telemetry.clear()
    pipeline = _pipeline(_Provider(_decision("allow")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(mode="shadow"))
    event_id = result["decim_safety"].get("event_id")
    assert event_id
    stored = telemetry.get_event(event_id)
    assert stored["mode"] == "shadow"
    assert stored["model_decision"] == "allow"
    assert stored["final_level"] == result["level"]
    assert stored["agreement"] in {"exact", "decim_more_conservative", "decim_more_permissive"}
    assert stored["final_unsafe"] == 1
    # Command text is kept only as a bounded, redacted preview (policy: 1000 chars).
    assert "shred -u /etc/hosts" in stored["detection_command"]
    assert len(stored["detection_command"]) <= 1000
    assert stored["command_fingerprint"]
    assert stored["detection_type"]


def test_event_records_agent_id_and_both_timings():
    telemetry = _events()
    telemetry.clear()
    pipeline = _pipeline(_Provider(_decision("allow")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", agent_context={"id": "agent-7"},
                            decim_settings=_settings(mode="shadow"))
    stored = telemetry.get_event(result["decim_safety"]["event_id"])
    assert stored["agent_id"] == "agent-7"
    assert isinstance(stored["hmads_duration_ms"], int) and stored["hmads_duration_ms"] >= 0
    assert stored["model_latency_ms"] is not None


def test_dmss_failure_records_measured_time():
    telemetry = _events()
    telemetry.clear()
    pipeline = _pipeline(_Provider(RuntimeError("provider down")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(mode="shadow"))
    assert result["decision_source"] == "deterministic_fallback"
    event = telemetry.list_events()["events"][0]
    assert event["model_decision"] is None
    assert isinstance(event["model_latency_ms"], int)


def test_enforce_records_only_when_enabled():
    telemetry = _events()
    telemetry.clear()
    pipeline = _pipeline(_Provider(_decision("allow")))

    pipeline.check(SAFE_BASH, tool_type="bash", decim_settings=_settings(mode="enforce"))
    assert telemetry.list_events()["total"] == 0

    pipeline.check(SAFE_BASH, tool_type="bash",
                   decim_settings=_settings(mode="enforce", record_enforce_events=True))
    assert telemetry.list_events()["total"] == 1


def test_telemetry_clear_bumps_epoch_and_is_audited():
    telemetry = _events()
    telemetry.clear()
    pipeline = _pipeline(_Provider(_decision("allow")))
    pipeline.check(SAFE_BASH, tool_type="bash", decim_settings=_settings(mode="shadow"))
    before = telemetry.get_state()

    result = telemetry.clear(actor="tester")
    assert result["success"] is True
    after = telemetry.get_state()
    assert after["telemetry_epoch"] == before["telemetry_epoch"] + 1
    assert telemetry.list_events()["total"] == 0


def test_telemetry_agreement_and_unsafe_flags():
    telemetry = _events()
    telemetry.clear()
    pipeline = _pipeline(_Provider(_decision("allow")))
    result = pipeline.check(UNSAFE_BASH, tool_type="bash", decim_settings=_settings(mode="shadow"))
    stored = telemetry.get_event(result["decim_safety"]["event_id"])
    # Deterministic says unsafe, model says allow -> model is more permissive.
    assert stored["agreement"] == "decim_more_permissive"
    assert stored["deterministic_unsafe"] == 1
    assert stored["model_unsafe"] == 0


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

def test_validate_settings_rejects_unknown_mode_and_non_string_model():
    with pytest.raises(ValueError):
        validate_decim_settings({"mode": "aggressive"})
    with pytest.raises(ValueError):
        validate_decim_settings({"model_id": 42})


def test_validate_settings_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        validate_decim_settings({"mode": "shadow", "minimum_confidence": 1.5})
    with pytest.raises(ValueError):
        validate_decim_settings({"mode": "shadow", "request_timeout_ms": 5})


def test_validate_settings_accepts_valid_mapping():
    settings = validate_decim_settings({
        "enabled": "true", "mode": "enforce", "model_id": "systemone/test-model",
        "minimum_confidence": 0.95, "retention_days": 7,
    })
    assert settings.enabled is True
    assert settings.mode == "enforce"
    assert settings.minimum_confidence == pytest.approx(0.95)
