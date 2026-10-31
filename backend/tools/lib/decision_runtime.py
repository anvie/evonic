"""Secure, domain-neutral Decision Runtime orchestration.

This module owns invocation boundaries, profile/model selection and deterministic
retry/fallback behaviour.  It deliberately does *not* interpret a decision's
fields; consumers retain all domain policy.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from enum import Enum
import json
import logging
import re
import time
from typing import Any, Callable, Mapping, Protocol

from backend.tools.lib.decision_adapters import DecisionCompatibilityError, OpenAIStructuredDecisionAdapter, validate_decision_compatibility
from backend.tools.lib.model_route_adapters import DEFAULT_ROUTE_ADAPTER_REGISTRY, RouteAdapterRegistry
from backend.tools.lib.model_routing_contracts import (
    AdapterKey, Operation, OperationRequirements, compatibility_for,
)
from backend.tools.lib.systemone_decision_adapter import SystemOneQuestionsDecisionAdapter
from backend.tools.lib.decision_models import (
    CONTRACT_VERSION, SCHEMA_VERSION, ContractValidationError, DecisionAdapter,
    DecisionCancellation, DecisionError, DecisionErrorCode, DecisionInvocation,
    DecisionProfileRef, DecisionResult, FallbackMetadata, TimingMetadata,
    validate_invocation, validate_result,
)

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_FIELDS = 32
MAX_CHOICES_PER_FIELD = 32
MAX_DESCRIPTION_CHARS = 2_000
MAX_ATTEMPTS = 3
MAX_TIMEOUT_MS = 120_000
_SECRET_PATTERN = re.compile(r"(?i)(?:api[_-]?key|authorization|password|secret|token)\s*[:=]\s*(?:bearer\s+)?[^\s,]+")


class DecisionRuntimeErrorCode(str, Enum):
    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_DISABLED = "profile_disabled"
    PROFILE_INVALID = "profile_invalid"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    SECRET_DETECTED = "secret_detected"
    LOW_CONFIDENCE = "low_confidence"
    RESPONSE_TOO_LARGE = "response_too_large"


class DecisionRuntimeError(ValueError):
    """Safe, typed runtime failure; messages must not contain request data."""
    def __init__(self, code: DecisionRuntimeErrorCode, message: str):
        self.code = code
        super().__init__(message)


class MetricsSink(Protocol):
    def increment(self, name: str, *, tags: Mapping[str, str] | None = None) -> None: ...
    def observe(self, name: str, value: float, *, tags: Mapping[str, str] | None = None) -> None: ...


@dataclass(frozen=True)
class RuntimeRequest:
    profile_key: str
    profile_version: str
    input_context: Mapping[str, Any]
    policy_metadata: Mapping[str, Any] | None = None
    correlation_id: str | None = None
    cancellation: DecisionCancellation | None = None
    channel: str = "internal"
    rate_limit_key: str = "default"
    # Explicit operational origin for privacy-safe activity diagnostics.
    activity_source: str = "production"


class FixedWindowRateLimiter:
    """Small in-process limiter suitable for internal runtime channels."""
    def __init__(self, limit: int = 60, window_seconds: float = 60.0) -> None:
        self.limit, self.window_seconds = limit, window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        calls = self._calls[key]
        while calls and calls[0] <= now - self.window_seconds:
            calls.popleft()
        if len(calls) >= self.limit:
            return False
        calls.append(now)
        return True


def _safe_json_bytes(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _has_secret(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SECRET_PATTERN.search(value))
    if isinstance(value, Mapping):
        for key, item in value.items():
            # Keys alone are not secrets, but are a reliable indicator that their
            # value must not cross the generic provider boundary.
            if isinstance(key, str) and re.search(r"(?i)(?:api[_-]?key|authorization|password|secret|token)", key):
                return True
            if _has_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_has_secret(item) for item in value)
    return False


class _RouteUnavailable(Exception):
    """The registry has no resolvable provider route for a model."""


def _safe_error(code: DecisionErrorCode, message: str, retryable: bool = False) -> DecisionError:
    return DecisionError(code, message, retryable)


class DecisionRuntime:
    """Resolve profiles and execute bounded, validated decisions.

    ``adapter_factory`` is injected to permit provider-free tests. It receives the
    resolved model mapping, whose credentials are never returned or logged.
    """
    def __init__(self, db: Any, *, adapter_factory: Callable[[Mapping[str, Any]], DecisionAdapter] | None = None,
                 route_adapters: RouteAdapterRegistry | None = None,
                 rate_limiter: FixedWindowRateLimiter | None = None, metrics: MetricsSink | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.db = db
        # Keep injection available for provider-free tests, while production route
        # selection remains explicit and keyed by the native protocol adapter.
        self.adapter_factory = adapter_factory
        self.route_adapters = route_adapters or DEFAULT_ROUTE_ADAPTER_REGISTRY
        self.rate_limiter = rate_limiter or FixedWindowRateLimiter()
        self.metrics, self.logger = metrics, logger or logging.getLogger(__name__)

    def _metric(self, name: str, value: float | None = None, **tags: str) -> None:
        if not self.metrics:
            return
        if value is None:
            self.metrics.increment(name, tags=tags)
        else:
            self.metrics.observe(name, value, tags=tags)

    def _activity(self, request: RuntimeRequest, result: DecisionResult) -> None:
        """Best-effort, privacy-safe diagnostic persistence.

        Activity persistence must not alter a decision result, including on a
        database outage or an implementation defect in the telemetry layer.
        """
        try:
            source = request.activity_source if request.activity_source in {"production", "playground", "profile_test"} else "production"
            fields = {field.key: field.value for field in result.fields}
            confidences = [field.confidence for field in result.fields if field.confidence is not None]
            self.db.record_decision_activity({
                "source": source, "profile_key": result.profile.key,
                "profile_version": result.profile.version,
                "model_id": result.provider.model_id if result.provider else None,
                "adapter_key": result.provider.provider_key if result.provider else None,
                "outcome_summary": fields,
                "confidence": min(confidences) if confidences else None,
                "latency_ms": result.timing.total_ms, "attempts": result.attempts,
                "fallback_used": bool(result.fallback and result.fallback.used),
                "fallback_reason": result.fallback.reason if result.fallback else None,
                "error_code": result.error.code.value if result.error else None,
                "correlation_id": request.correlation_id,
            })
        except Exception:
            self.logger.warning("decision_runtime.activity_write_failed profile=%s", request.profile_key)

    def _profile(self, request: RuntimeRequest) -> Mapping[str, Any]:
        profile = self.db.get_decision_profile(request.profile_key)
        if not profile:
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.PROFILE_NOT_FOUND, "Decision profile was not found.")
        if not profile.get("enabled") or profile.get("mode") == "off":
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.PROFILE_DISABLED, "Decision profile is not enabled.")
        if profile.get("policy_version") != request.profile_version:
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.PROFILE_INVALID, "Decision profile version does not match.")
        try:
            # Revalidate persisted configuration and schema at invocation time.
            self.db.validate_decision_profile(profile, existing=profile)
        except (ValueError, ContractValidationError) as exc:
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.PROFILE_INVALID, "Decision profile configuration is invalid.") from exc
        return profile

    def _invocation(self, request: RuntimeRequest, profile: Mapping[str, Any]) -> DecisionInvocation:
        raw = {"contract_version": CONTRACT_VERSION, "profile": {"key": request.profile_key, "version": request.profile_version},
               "schema": profile["schema"], "input_context": request.input_context,
               "policy_metadata": request.policy_metadata or {}, "correlation_id": request.correlation_id,
               "cancellation": None if request.cancellation is None else {"deadline_unix_ms": request.cancellation.deadline_unix_ms, "cancelled": request.cancellation.cancelled}}
        if _safe_json_bytes(raw) > MAX_PAYLOAD_BYTES:
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.PAYLOAD_TOO_LARGE, "Decision request exceeds the payload limit.")
        if _has_secret(raw):
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.SECRET_DETECTED, "Decision request contains a secret-like value.")
        invocation = validate_invocation(raw)
        if len(invocation.schema.fields) > MAX_FIELDS or any(len(field.choices) > MAX_CHOICES_PER_FIELD or len(field.description) > MAX_DESCRIPTION_CHARS for field in invocation.schema.fields):
            raise DecisionRuntimeError(DecisionRuntimeErrorCode.PROFILE_INVALID, "Decision schema exceeds runtime limits.")
        return invocation

    def invoke(self, request: RuntimeRequest) -> DecisionResult:
        """Run primary then fallback deterministically; never emit raw provider data."""
        started = time.monotonic()
        try:
            if not self.rate_limiter.allow(f"{request.channel}:{request.rate_limit_key}"):
                raise DecisionRuntimeError(DecisionRuntimeErrorCode.RATE_LIMITED, "Decision request rate limit exceeded.")
            profile = self._profile(request)
            invocation = self._invocation(request, profile)
            if invocation.cancellation and invocation.cancellation.cancelled:
                result = self._failure(invocation, DecisionErrorCode.CANCELLED, "Decision was cancelled.", started)
                self._activity(request, result)
                return result
            model_ids = [profile["primary_model_id"]] + ([profile["fallback_model_id"]] if profile.get("fallback_model_id") else [])
            max_attempts = min(MAX_ATTEMPTS, int(profile["retry_limit"]) + 1)
            last: DecisionResult | None = None
            for attempt in range(max_attempts):
                model_id = model_ids[min(attempt, len(model_ids) - 1)]
                fallback = attempt > 0
                model = self.db.get_model_by_id(model_id)
                try:
                    configured_timeout = min(MAX_TIMEOUT_MS / 1000, float(profile["timeout_ms"]) / 1000)
                    # The profile timeout is a cumulative budget across primary
                    # and fallback attempts, never a fresh timeout per provider.
                    timeout = configured_timeout - (time.monotonic() - started)
                    if timeout <= 0:
                        raise TimeoutError("Decision deadline elapsed before provider invocation.")
                    route = self._resolve_route(model_id)
                    if route is None and callable(getattr(self.db, "resolve_model_route", None)):
                        # A registry model without a provider endpoint is a
                        # configuration gap, not a reason to guess another adapter.
                        raise _RouteUnavailable(
                            f"Decision model '{model_id}' has no usable provider: "
                            f"provider '{(model or {}).get('provider') or '?'}' is missing or has no endpoint. "
                            "Set the endpoint under System \u203a Models.")
                    if route is None:
                        # Temporary compatibility branch for non-registry test/dry-run
                        # stores. Production registry databases always resolve a route.
                        validate_decision_compatibility(model, invocation)
                        bounded_model = {**model, "timeout": timeout}
                    else:
                        requirements = OperationRequirements(
                            frozenset({Operation.DECISION, Operation.STRICT_OUTPUT}),
                            schema_version=invocation.schema.version,
                            require_confidence=bool(profile.get("require_confidence", False)),
                            data_classification=str((request.policy_metadata or {}).get("data_classification", "public")),
                        )
                        compatible = compatibility_for(route, requirements)
                        if not compatible.compatible:
                            raise DecisionCompatibilityError(compatible.message)
                        adapter_failure = self.route_adapters.validate(route, Operation.DECISION)
                        if adapter_failure:
                            raise DecisionCompatibilityError(adapter_failure.message)
                        adapter = self.route_adapters.get(route.provider.adapter_key)
                        assert adapter is not None
                        bounded_model = adapter.model_config(route, timeout=timeout)
                        validate_decision_compatibility(bounded_model, invocation)
                    adapter_factory = self.adapter_factory
                    if adapter_factory is None:
                        adapter_factory = (
                            SystemOneQuestionsDecisionAdapter
                            if bounded_model.get("adapter_key") == AdapterKey.SYSTEMONE_QUESTIONS_V1.value
                            else OpenAIStructuredDecisionAdapter
                        )
                    result = adapter_factory(bounded_model).decide(invocation)
                    result = replace(result, attempts=attempt + 1, fallback=FallbackMetadata(fallback, "previous_attempt_failed" if fallback else None) if fallback else None)
                    if result.error and result.error.code is DecisionErrorCode.CANCELLED:
                        # Cancellation is terminal: never retry or invoke fallback.
                        result = replace(result, timing=TimingMetadata(int((time.monotonic() - started) * 1000), result.timing.provider_ms))
                        self._activity(request, result)
                        return result
                    if _safe_json_bytes(result.to_dict()) > MAX_RESPONSE_BYTES:
                        raise DecisionRuntimeError(DecisionRuntimeErrorCode.RESPONSE_TOO_LARGE, "Decision response exceeds the response limit.")
                    if result.error is None:
                        result = validate_result(invocation.schema, result)
                        confidences = [field.confidence for field in result.fields if field.confidence is not None]
                        confidence_required = bool(profile.get("require_confidence", False))
                        if (confidence_required and len(confidences) != len(result.fields)) or (confidences and min(confidences) < float(profile["minimum_confidence"])):
                            self._metric("decision_runtime.low_confidence", profile=request.profile_key)
                            # Runtime-detected providers that answer below the
                            # confidence policy are retryable so the configured
                            # fallback model gets a chance to answer.
                            last = replace(result, error=_safe_error(DecisionErrorCode.PROVIDER_RESPONSE_INVALID, "Decision confidence was missing or below the configured threshold.", True))
                        else:
                            self._success(result, request, started, fallback)
                            self._activity(request, result)
                            return result
                    else:
                        last = result
                        # A provider explicitly marks non-retryable failures as
                        # terminal; never egress the request to a fallback route.
                        if result.error is not None and not result.error.retryable:
                            break
                except _RouteUnavailable as exc:
                    last = self._failure(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE, str(exc), started, attempt + 1, fallback)
                except (DecisionCompatibilityError, ContractValidationError, ValueError, TimeoutError) as exc:
                    code = DecisionErrorCode.DEADLINE_EXCEEDED if isinstance(exc, TimeoutError) else DecisionErrorCode.NO_COMPATIBLE_MODEL if isinstance(exc, DecisionCompatibilityError) else DecisionErrorCode.PROVIDER_RESPONSE_INVALID
                    last = self._failure(invocation, code, "Decision provider returned an invalid response." if code is DecisionErrorCode.PROVIDER_RESPONSE_INVALID else "No compatible decision model is available." if code is DecisionErrorCode.NO_COMPATIBLE_MODEL else "Decision provider timed out.", started, attempt + 1, fallback)
                if fallback:
                    self._metric("decision_runtime.fallback", profile=request.profile_key)
                # Route compatibility failures and a terminal provider result
                # must not consume another attempt or invoke fallback.
                if last and last.error and not last.error.retryable:
                    break
            assert last is not None
            self._metric("decision_runtime.failure", profile=request.profile_key, error=last.error.code.value if last.error else "unknown")
            result = replace(last, timing=TimingMetadata(int((time.monotonic() - started) * 1000), last.timing.provider_ms))
            self._activity(request, result)
            return result
        except DecisionRuntimeError:
            self._metric("decision_runtime.failure", profile=request.profile_key, error="boundary")
            raise

    def _resolve_route(self, model_id: str):
        resolver = getattr(self.db, "resolve_model_route", None)
        return resolver(model_id) if callable(resolver) else None

    def _failure(self, invocation: DecisionInvocation, code: DecisionErrorCode, message: str, started: float, attempts: int = 0, fallback: bool = False) -> DecisionResult:
        # Runtime-detected malformed responses are retryable so the fallback
        # model is tried.  Errors a provider explicitly declares non-retryable
        # never reach this helper and stay terminal.
        retryable = code in {
            DecisionErrorCode.PROVIDER_UNAVAILABLE,
            DecisionErrorCode.DEADLINE_EXCEEDED,
            DecisionErrorCode.PROVIDER_RESPONSE_INVALID,
        }
        return DecisionResult(invocation.profile, SCHEMA_VERSION, (), None, TimingMetadata(int((time.monotonic() - started) * 1000)), attempts, (), FallbackMetadata(True, "previous_attempt_failed") if fallback else None, _safe_error(code, message, retryable))

    def _success(self, result: DecisionResult, request: RuntimeRequest, started: float, fallback: bool) -> None:
        self._metric("decision_runtime.success", profile=request.profile_key)
        if fallback:
            self._metric("decision_runtime.fallback", profile=request.profile_key)
        self._metric("decision_runtime.latency_ms", (time.monotonic() - started) * 1000, profile=request.profile_key)
        # Deliberately logs only identifiers/metadata, never contexts or results.
        self.logger.info("decision_runtime.completed profile=%s attempts=%d fallback=%s", request.profile_key, result.attempts, fallback)
