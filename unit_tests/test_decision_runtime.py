"""Tests for bounded, provider-neutral DecisionRuntime orchestration."""
from dataclasses import dataclass

import pytest

from backend.tools.lib.decision_models import (
    DecisionCancellation, DecisionError, DecisionErrorCode, DecisionFieldResult,
    DecisionResult, ProviderMetadata, TimingMetadata,
)
from backend.tools.lib.decision_runtime import (
    DecisionRuntime, DecisionRuntimeError, DecisionRuntimeErrorCode,
    FixedWindowRateLimiter, RuntimeRequest,
)
from backend.tools.lib.model_routing_contracts import (
    AdapterKey, CapabilityEvidence, ModelCapabilities, Operation, ProviderRoute,
    ResolvedModelRoute,
)


def _model(model_id):
    return {
        "id": model_id, "provider": "test", "model_name": model_id,
        "enabled": True, "api_format": "openai", "timeout": 30,
        "capabilities": {"decision": {"structured_output": True,
            "schema_primitives": ["boolean"]}},
    }


def _profile(**overrides):
    profile = {
        "profile_key": "application.example", "enabled": True, "mode": "enforce",
        "policy_version": "1", "schema_version": "decision-schema/v1",
        "schema": {"version": "decision-schema/v1", "fields": [
            {"key": "approved", "kind": "boolean", "description": "Approval"},
        ]},
        "primary_model_id": "primary", "fallback_model_id": "fallback",
        "timeout_ms": 1000, "retry_limit": 1, "minimum_confidence": 0.5,
    }
    profile.update(overrides)
    return profile


class FakeDB:
    def __init__(self, profile=None, models=None):
        self.profile = profile or _profile()
        self.models = models or {"primary": _model("primary"), "fallback": _model("fallback")}

    def get_decision_profile(self, key):
        return self.profile if key == self.profile["profile_key"] else None

    def validate_decision_profile(self, data, *, existing=None):
        return data

    def get_model_by_id(self, model_id):
        return self.models.get(model_id)


def _route(model_id, *, enabled=True, provider_enabled=True, egress=("public",)):
    return ResolvedModelRoute(
        model_id, ProviderRoute("test", "https://adapter.invalid/v1", AdapterKey.OPENAI_CHAT_COMPLETIONS_V1, provider_enabled),
        model_id, ModelCapabilities({Operation.DECISION: CapabilityEvidence.VERIFIED,
                                     Operation.STRICT_OUTPUT: CapabilityEvidence.VERIFIED},
                                    frozenset({"decision-schema/v1"}), data_egress=frozenset(egress)),
        credentials={"api_key": "private"}, enabled=enabled,
    )


class RoutedFakeDB(FakeDB):
    def __init__(self, routes, **kwargs):
        super().__init__(**kwargs)
        self.routes = routes

    def resolve_model_route(self, model_id):
        return self.routes.get(model_id)


@dataclass
class FakeAdapter:
    result: DecisionResult
    calls: int = 0
    key: str = "fake"

    def decide(self, invocation):
        self.calls += 1
        return self.result


def _result(invocation, *, error=None, confidence=0.9, text=None):
    fields = () if error else (DecisionFieldResult("approved", True, confidence),)
    if text:
        fields = (DecisionFieldResult("approved", True, confidence),)
    return DecisionResult(invocation.profile, invocation.schema.version, fields,
        ProviderMetadata("test", "model"), TimingMetadata(1, 1), 1, error=error)


def _runtime(db, adapters, **kwargs):
    def factory(model):
        return adapters.pop(0)
    return DecisionRuntime(db, adapter_factory=factory, **kwargs)


def _request(**overrides):
    request = RuntimeRequest("application.example", "1", {"subject": "sample"})
    return type(request)(**{**request.__dict__, **overrides})


def test_normal_result_is_validated_and_contains_only_normalized_metadata():
    adapter = FakeAdapter(None)
    runtime = _runtime(FakeDB(), [adapter])
    # The adapter needs the runtime-created invocation; return it through a closure.
    class DynamicAdapter:
        key = "fake"
        def decide(self, invocation):
            return _result(invocation)
    runtime = _runtime(FakeDB(), [DynamicAdapter()])
    result = runtime.invoke(_request())
    assert result.error is None
    assert result.fields[0].value is True
    assert result.provider.model_id == "model"
    assert result.attempts == 1


def test_provider_failure_uses_fallback_deterministically():
    class Failure:
        key = "failure"
        def decide(self, invocation):
            return _result(invocation, error=DecisionError(DecisionErrorCode.PROVIDER_UNAVAILABLE, "internal provider text", True))
    class Success:
        key = "success"
        def decide(self, invocation): return _result(invocation)
    result = _runtime(FakeDB(), [Failure(), Success()]).invoke(_request())
    assert result.error is None
    assert result.attempts == 2
    assert result.fallback.used is True


def test_timeout_uses_fallback_and_returns_safe_result():
    class Timeout:
        key = "timeout"
        def decide(self, invocation): raise TimeoutError("provider endpoint detail")
    class Success:
        key = "success"
        def decide(self, invocation): return _result(invocation)
    result = _runtime(FakeDB(), [Timeout(), Success()]).invoke(_request())
    assert result.error is None and result.fallback.used


def test_cancellation_is_terminal_and_does_not_invoke_adapter():
    adapter = FakeAdapter(None)
    result = _runtime(FakeDB(), [adapter]).invoke(_request(cancellation=DecisionCancellation(cancelled=True)))
    assert result.error.code is DecisionErrorCode.CANCELLED
    assert adapter.calls == 0


def test_malformed_output_falls_back():
    class Invalid:
        key = "invalid"
        def decide(self, invocation):
            return DecisionResult(invocation.profile, invocation.schema.version,
                (DecisionFieldResult("approved", "not-a-bool", 0.9),), None, TimingMetadata(1), 1)
    class Success:
        key = "success"
        def decide(self, invocation): return _result(invocation)
    result = _runtime(FakeDB(), [Invalid(), Success()]).invoke(_request())
    assert result.error is None and result.attempts == 2


def test_low_confidence_falls_back():
    class Low:
        key = "low"
        def decide(self, invocation): return _result(invocation, confidence=0.1)
    class Success:
        key = "success"
        def decide(self, invocation): return _result(invocation)
    result = _runtime(FakeDB(), [Low(), Success()]).invoke(_request())
    assert result.error is None and result.attempts == 2


def test_rate_limit_is_enforced_before_invocation():
    runtime = _runtime(FakeDB(), [], rate_limiter=FixedWindowRateLimiter(limit=0))
    with pytest.raises(DecisionRuntimeError) as exc:
        runtime.invoke(_request())
    assert exc.value.code is DecisionRuntimeErrorCode.RATE_LIMITED


def test_secret_like_context_is_rejected_without_echoing_value():
    runtime = _runtime(FakeDB(), [])
    with pytest.raises(DecisionRuntimeError) as exc:
        runtime.invoke(_request(input_context={"authorization": "Bearer dont-return-this"}))
    assert exc.value.code is DecisionRuntimeErrorCode.SECRET_DETECTED
    assert "dont-return-this" not in str(exc.value)


def test_disabled_provider_route_is_rejected_without_invoking_adapter():
    db = RoutedFakeDB({"primary": _route("primary", provider_enabled=False)},
                      profile=_profile(fallback_model_id=None, retry_limit=0))
    result = _runtime(db, []).invoke(_request())
    assert result.error.code is DecisionErrorCode.NO_COMPATIBLE_MODEL


def test_egress_restricted_fallback_is_not_invoked():
    class RetryableFailure:
        key = "failure"
        def decide(self, invocation):
            return _result(invocation, error=DecisionError(DecisionErrorCode.PROVIDER_UNAVAILABLE, "safe", True))

    db = RoutedFakeDB({
        "primary": _route("primary", egress=("internal",)),
        "fallback": _route("fallback", egress=("public",)),
    })
    result = _runtime(db, [RetryableFailure()]).invoke(
        _request(policy_metadata={"data_classification": "internal"}))
    assert result.error.code is DecisionErrorCode.NO_COMPATIBLE_MODEL


def test_non_retryable_failure_does_not_egress_to_fallback():
    class TerminalFailure:
        key = "terminal"
        def decide(self, invocation):
            return _result(invocation, error=DecisionError(DecisionErrorCode.PROVIDER_RESPONSE_INVALID, "safe", False))

    db = RoutedFakeDB({"primary": _route("primary"), "fallback": _route("fallback")})
    result = _runtime(db, [TerminalFailure()]).invoke(_request())
    assert result.error.code is DecisionErrorCode.PROVIDER_RESPONSE_INVALID
    assert result.attempts == 1


def _chain_runtime(profile, models, outcomes):
    """A runtime whose adapters answer per model: True succeeds, False fails (retryable). Returns (runtime, calls)."""
    calls = []
    def factory(model):
        class Adapter:
            key = "chain"
            def decide(self, invocation):
                calls.append(model["id"])
                if outcomes.get(model["id"]):
                    return _result(invocation)
                return _result(invocation, error=DecisionError(DecisionErrorCode.PROVIDER_UNAVAILABLE, "down", True))
        return Adapter()
    db = FakeDB(profile=profile, models={m: _model(m) for m in models})
    return DecisionRuntime(db, adapter_factory=factory), calls


def test_a_fallback_chain_is_walked_in_order_within_the_retry_limit():
    models = ["primary", "f1", "f2", "f3"]
    profile = _profile(fallback_model_id="f1", fallback_model_ids=["f1", "f2", "f3"], retry_limit=3)
    runtime, calls = _chain_runtime(profile, models, {"f3": True})
    result = runtime.invoke(_request())
    assert result.error is None and result.attempts == 4 and calls == models


def test_the_retry_limit_still_bounds_the_chain():
    profile = _profile(fallback_model_id="f1", fallback_model_ids=["f1", "f2"], retry_limit=0)
    runtime, calls = _chain_runtime(profile, ["primary", "f1", "f2"], {})
    assert runtime.invoke(_request()).error is not None and calls == ["primary"]


def test_a_profile_without_a_list_reads_its_single_fallback():
    runtime, calls = _chain_runtime(_profile(retry_limit=2), ["primary", "fallback"], {})
    runtime.invoke(_request())
    assert calls == ["primary", "fallback", "fallback"]
