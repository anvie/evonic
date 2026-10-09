"""Provider-neutral Decim Safety decision resolution.

Decim is the *decision model* layer: this module owns the optional remote
decision integration and deliberately keeps it outside of HMADS.  A valid,
confident Decim result may govern the source-aware safety pipeline; every
failure yields a typed fallback reason so the deterministic HMADS pipeline keeps
its exact existing behaviour.

Only a small, versioned policy packet is sent to a provider.  In particular no
agent context, environment values, credentials, or conversation data leaves this
process.  Provider branding (endpoint, model product) is a private deployment
concern and never forms part of the public contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import re
import socket
import time
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

logger = logging.getLogger(__name__)

POLICY_VERSION = "decim-v1"
SCOPED_TOOL_TYPES = frozenset({"bash", "python"})
VALID_MODES = frozenset({"off", "shadow", "enforce"})
VALID_PROVIDERS = frozenset({"systemone"})
VALID_CHOICES = frozenset({"allow", "review", "block"})
MAX_RESPONSE_BYTES = 64 * 1024

# Obvious secret-bearing markers.  When any of these is present the payload is
# *not* sent to a provider (redaction could change executable semantics), and the
# caller falls back to deterministic safety with reason ``sensitive_payload``.
_SECRET_PATTERN = re.compile(
    r"(?ix)(?:\b(?:api[_-]?key|token|password|secret|authorization)\b\s*(?:=|:)\s*|"
    r"\b(?:sk|ghp|github_pat)_[a-z0-9_-]{12,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)

__all__ = [
    "POLICY_VERSION",
    "SCOPED_TOOL_TYPES",
    "VALID_MODES",
    "VALID_PROVIDERS",
    "DecimSettings",
    "DecimDecision",
    "DecimResolution",
    "mask_secret",
    "DecimSafetyProvider",
    "DecimProviderError",
    "SystemOneDecisionProvider",
    "DecimCircuitBreaker",
    "DecimSafetyResolver",
    "load_decim_settings",
    "validate_decim_settings",
    "save_decim_settings",
    "build_policy_packet",
    "fingerprint_code",
    "get_decim_safety_resolver",
    "reset_decim_safety_resolver",
]


@dataclass(frozen=True)
class DecimSettings:
    """Provider-neutral operational settings for Decim Safety."""

    enabled: bool = False
    mode: Literal["off", "shadow", "enforce"] = "off"
    provider: str = "systemone"
    # Provider decision endpoint (the sensitive deployment secret).  Empty means
    # "not configured here" — the adapter falls back to its environment value.
    provider_endpoint: str = ""
    request_timeout_ms: int = 1500
    minimum_confidence: float = 0.90
    max_payload_chars: int = 12000
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: int = 60
    record_enforce_events: bool = False
    retention_days: int = 30


@dataclass(frozen=True)
class DecimDecision:
    """A normalized, locally validated provider decision."""

    decision: Literal["allow", "review", "block"]
    confidence: float
    model_id: str | None
    provider_key: str
    policy_version: str
    correlation_id: str | None
    latency_ms: int
    # Optional provider-supplied explanation of the choice.  The provider is
    # untrusted, so consumers must treat this as data (bound + sanitize before
    # display); ``None`` when the response carries no explanation.
    reason: str | None = None


@dataclass(frozen=True)
class DecimResolution:
    """The outcome of one optional provider attempt.

    ``decision`` is present only after local validation accepts every part of the
    provider result.  Consumers must use deterministic safety otherwise.
    """

    attempted: bool
    decision: DecimDecision | None = None
    fallback_reason: str | None = None
    correlation_id: str | None = None


class DecimSafetyProvider(Protocol):
    """Typed adapter contract implemented by concrete provider adapters."""

    key: str

    def decide(self, packet: dict[str, Any], settings: DecimSettings) -> DecimDecision:
        """Return a normalized decision or raise a provider/transport error."""


class DecimProviderError(RuntimeError):
    """An expected, non-sensitive provider failure category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


# ---------------------------------------------------------------------------
# Settings loading / validation
# ---------------------------------------------------------------------------

def mask_secret(value: Any) -> str:
    """Return a redacted form of a secret value (never the full value).

    Short values are fully masked; longer values keep a short prefix and the
    last four characters so the field is recognisable without leaking the
    secret (e.g. ``https://internal:8080/decide`` -> ``htt…cide``).
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) <= 8:
        return "\u2022" * len(text)
    return text[:3] + "\u2026" + text[-4:]


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _setting(key: str, default: str) -> str:
    """Read a setting without making Decim a required DB dependency."""
    try:
        from models.db import db
        return db.get_setting(key, default)
    except Exception:
        return default


def _parse_settings(values: dict[str, Any], *, strict: bool) -> DecimSettings:
    """Parse settings from persistence or an administrative API payload."""
    defaults = DecimSettings()
    mode = str(values.get("mode", defaults.mode))
    provider = str(values.get("provider", defaults.provider))
    if mode not in VALID_MODES:
        raise ValueError("mode must be one of off, shadow, enforce")
    if provider not in VALID_PROVIDERS:
        raise ValueError("provider is not registered")
    try:
        timeout = int(values.get("request_timeout_ms", defaults.request_timeout_ms))
        payload = int(values.get("max_payload_chars", defaults.max_payload_chars))
        failures = int(values.get("circuit_breaker_failures", defaults.circuit_breaker_failures))
        cooldown = int(values.get("circuit_breaker_cooldown_seconds", defaults.circuit_breaker_cooldown_seconds))
        retention = int(values.get("retention_days", defaults.retention_days))
        confidence = float(values.get("minimum_confidence", defaults.minimum_confidence))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("numeric Decim Safety settings are invalid") from exc
    if not (100 <= timeout <= 10_000 and 1 <= payload <= 120_000 and 1 <= failures <= 20
            and 1 <= cooldown <= 3_600 and 1 <= retention <= 3_650
            and math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
        raise ValueError("Decim Safety setting is outside its allowed range")

    enabled = _as_bool(values.get("enabled", defaults.enabled), defaults.enabled)
    record_events = _as_bool(values.get("record_enforce_events", defaults.record_enforce_events),
                             defaults.record_enforce_events)
    provider_endpoint = str(values.get("provider_endpoint", defaults.provider_endpoint) or "")
    if strict:
        for name, raw in (("enabled", values.get("enabled", enabled)),
                          ("record_enforce_events", values.get("record_enforce_events", record_events))):
            if not isinstance(raw, (bool, str)):
                raise ValueError(f"{name} must be a boolean")
        if not isinstance(values.get("provider_endpoint", provider_endpoint), str):
            raise ValueError("provider_endpoint must be a string")
        if provider_endpoint:
            parsed_endpoint = urlparse(provider_endpoint)
            if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
                raise ValueError("provider_endpoint must be an absolute http(s) URL")
    return DecimSettings(
        enabled=enabled, mode=mode, provider=provider, provider_endpoint=provider_endpoint,
        request_timeout_ms=timeout, minimum_confidence=confidence,
        max_payload_chars=payload, circuit_breaker_failures=failures,
        circuit_breaker_cooldown_seconds=cooldown, retention_days=retention,
        record_enforce_events=record_events,
    )


def load_decim_settings() -> DecimSettings:
    """Load and validate public Decim operational settings.

    Invalid persisted values safely disable model calls rather than causing an
    execution-path failure. The provider endpoint is a protected secret: only
    the settings serializer/API layer may expose its redacted form.
    """
    raw = {
        "enabled": _setting("decim_safety.enabled", "0"),
        "mode": _setting("decim_safety.mode", "off"),
        "provider": _setting("decim_safety.provider", "systemone"),
        "provider_endpoint": _setting("decim_safety.provider_endpoint", ""),
        "request_timeout_ms": _setting("decim_safety.request_timeout_ms", "1500"),
        "minimum_confidence": _setting("decim_safety.minimum_confidence", "0.90"),
        "max_payload_chars": _setting("decim_safety.max_payload_chars", "12000"),
        "circuit_breaker_failures": _setting("decim_safety.circuit_breaker_failures", "3"),
        "circuit_breaker_cooldown_seconds": _setting("decim_safety.circuit_breaker_cooldown_seconds", "60"),
        "record_enforce_events": _setting("decim_safety.record_enforce_events", "0"),
        "retention_days": _setting("decim_safety.retention_days", "30"),
    }
    try:
        return _parse_settings(raw, strict=False)
    except ValueError:
        logger.warning("Decim Safety settings are invalid; model decisions disabled")
        return DecimSettings()


def validate_decim_settings(values: dict[str, Any]) -> DecimSettings:
    """Validate a prospective provider-neutral settings mapping without DB writes."""
    return _parse_settings(values, strict=True)


_PERSISTED_SETTING_KEYS = (
    ("enabled", lambda s: "1" if s.enabled else "0"),
    ("mode", lambda s: s.mode),
    ("provider", lambda s: s.provider),
    ("provider_endpoint", lambda s: s.provider_endpoint),
    ("request_timeout_ms", lambda s: str(s.request_timeout_ms)),
    ("minimum_confidence", lambda s: str(s.minimum_confidence)),
    ("max_payload_chars", lambda s: str(s.max_payload_chars)),
    ("circuit_breaker_failures", lambda s: str(s.circuit_breaker_failures)),
    ("circuit_breaker_cooldown_seconds", lambda s: str(s.circuit_breaker_cooldown_seconds)),
    ("record_enforce_events", lambda s: "1" if s.record_enforce_events else "0"),
    ("retention_days", lambda s: str(s.retention_days)),
)


def save_decim_settings(settings: DecimSettings) -> None:
    """Persist the public provider-neutral settings using system-settings keys."""
    from models.db import db
    for name, serialize in _PERSISTED_SETTING_KEYS:
        db.set_setting(f"decim_safety.{name}", serialize(settings))


# ---------------------------------------------------------------------------
# Policy packet construction
# ---------------------------------------------------------------------------

def _execution_boundary(agent_context: dict[str, Any] | None) -> str:
    """Normalize the execution boundary to a small, non-identifying label."""
    ctx = agent_context or {}
    if ctx.get("ssh_connected") or ctx.get("ssh_host"):
        return "remote_ssh"
    if not ctx.get("sandbox_enabled", 1):
        return "local_process"
    return "sandboxed_docker"


def fingerprint_code(code: str) -> str:
    """Return an opaque, non-reversible fingerprint of the executable payload."""
    return hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()[:16]


def build_policy_packet(code: str, tool_type: str, boundary: str, max_chars: int) -> dict[str, Any]:
    """Build the bounded, versioned policy packet presented to a provider."""
    if len(code) > max_chars:
        raise DecimProviderError("payload_too_large")
    # Replacing a secret can alter executable meaning, so do not send it at all.
    if _SECRET_PATTERN.search(code):
        raise DecimProviderError("sensitive_payload")
    return {
        "policy_version": POLICY_VERSION,
        "correlation_id": str(uuid4()),
        "tool_type": tool_type,
        "execution_boundary": boundary,
        "requested_code": code,
        "decision_schema": {"allowed": ["allow", "review", "block"]},
    }


# ---------------------------------------------------------------------------
# Provider response parsing
# ---------------------------------------------------------------------------

def _coerce_probability(value: Any) -> float | None:
    """Coerce a candidate probability; return None when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(prob) or not 0.0 <= prob <= 1.0:
        return None
    return prob


def _conservative_confidence(reported: Any, probabilities: Any, choice: str,
                             selected_probability: Any) -> float:
    """Compute a conservative confidence from any combination of signals.

    Where both an overall confidence and a selected-choice probability exist we
    take the minimum, so a provider cannot inflate confidence above its own
    distribution.
    """
    selected = _coerce_probability(selected_probability)
    if isinstance(probabilities, dict):
        values: list[float] = []
        for key, raw in probabilities.items():
            prob = _coerce_probability(raw.get("probability") if isinstance(raw, dict) else raw)
            if prob is None:
                raise DecimProviderError("invalid_response")
            values.append(prob)
            if key == choice:
                selected = prob
        # A coherent distribution must not sum beyond 1 (allow tiny rounding).
        if values and sum(values) > 1.0 + 1e-6:
            raise DecimProviderError("invalid_response")
    reported_value = _coerce_probability(reported)
    candidates = [value for value in (reported_value, selected) if value is not None]
    if not candidates:
        raise DecimProviderError("invalid_response")
    return min(candidates)


# Upper bound for a provider-supplied reason captured at parse time.  The API
# layer sanitizes and re-bounds it before anything reaches the browser.
MAX_REASON_CHARS = 500


def _parse_reason(answer: dict[str, Any]) -> str | None:
    """Extract an optional provider explanation, or ``None`` when absent."""
    reason = answer.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip()[:MAX_REASON_CHARS]


def _parse_decision_answer(data: Any) -> tuple[str, float, str | None, str | None]:
    """Parse a SystemOne/Nimble-style typed choice response.

    Returns ``(choice, confidence, model_id, reason)`` or raises
    :class:`DecimProviderError` with a non-sensitive category.
    """
    if not isinstance(data, dict):
        raise DecimProviderError("invalid_response")
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise DecimProviderError("invalid_response")
    answer = answers.get("decision")
    if not isinstance(answer, dict):
        raise DecimProviderError("invalid_response")

    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in VALID_CHOICES:
        raise DecimProviderError("invalid_response")

    confidence = _conservative_confidence(
        answer.get("confidence"),
        answer.get("probabilities") or answer.get("candidates"),
        choice,
        answer.get("probability"),
    )
    model_id = data.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        model_id = None
    return choice, confidence, model_id, _parse_reason(answer)


# ---------------------------------------------------------------------------
# Provider adapter: SystemOne calling convention
# ---------------------------------------------------------------------------

class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects outright: providers must not bounce sensitive requests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise DecimProviderError("redirect_rejected")


class SystemOneDecisionProvider:
    """Adapter for the currently available SystemOne decision endpoint.

    Internally this targets the deployment's decision model.  The endpoint and
    model identity are private deployment configuration and are never exposed as
    part of the public Decim Safety contract.
    """

    key = "systemone"

    def __init__(self, endpoint: str | None = None, *, transport=None):
        self._endpoint_override = endpoint
        self._transport = transport

    @property
    def endpoint(self) -> str:
        if self._endpoint_override is not None:
            return self._endpoint_override
        return os.getenv("DECIM_SAFETY_SYSTEMONE_ENDPOINT", "")

    def _build_request(self, packet: dict[str, Any], endpoint: str) -> Request:
        body = json.dumps({
            "state": packet["requested_code"],
            "context": {
                "policy_version": packet["policy_version"],
                "correlation_id": packet["correlation_id"],
                "tool_type": packet["tool_type"],
                "execution_boundary": packet["execution_boundary"],
            },
            "questions": {
                "decision": {
                    "type": "choice",
                    "instructions": "Classify execution safety.",
                    "criteria": {
                        "allow": "safe to execute",
                        "review": "requires human approval",
                        "block": "must not execute",
                    },
                }
            },
        }, separators=(",", ":")).encode()
        return Request(endpoint, data=body,
                       headers={"Content-Type": "application/json", "Accept": "application/json"},
                       method="POST")

    def _read_response(self, request: Request, settings: DecimSettings) -> bytes:
        opener = self._transport or build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=settings.request_timeout_ms / 1000) as response:
                if not 200 <= response.status < 300:
                    raise DecimProviderError("http_error")
                return response.read(MAX_RESPONSE_BYTES + 1)
        except DecimProviderError:
            raise
        except HTTPError:
            raise DecimProviderError("http_error") from None
        except (URLError, socket.timeout, TimeoutError):
            raise DecimProviderError("transport_error") from None
        except Exception:
            raise DecimProviderError("provider_error") from None

    def decide(self, packet: dict[str, Any], settings: DecimSettings) -> DecimDecision:
        # The admin-configured endpoint (stored secret) wins; fall back to the
        # deployment environment value when none is configured in settings.
        endpoint = (getattr(settings, "provider_endpoint", "") or "").strip() or self.endpoint
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DecimProviderError("provider_unconfigured")
        started = time.monotonic()
        raw = self._read_response(self._build_request(packet, endpoint), settings)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DecimProviderError("response_too_large")
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            raise DecimProviderError("invalid_response") from None
        choice, confidence, model_id, reason = _parse_decision_answer(data)
        return DecimDecision(
            decision=choice,
            confidence=confidence,
            model_id=model_id,
            provider_key=self.key,
            policy_version=POLICY_VERSION,
            correlation_id=packet.get("correlation_id"),
            latency_ms=int((time.monotonic() - started) * 1000),
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Circuit breaker + resolver
# ---------------------------------------------------------------------------

class DecimCircuitBreaker:
    """Closed/open/half-open rolling circuit breaker with an injectable clock."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    def permits(self, settings: DecimSettings) -> bool:
        if self._opened_at is None:
            return True
        # Half-open: once the cooldown elapses a single probe is permitted.
        return self._clock() - self._opened_at >= settings.circuit_breaker_cooldown_seconds

    def success(self) -> None:
        self._failures, self._opened_at = 0, None

    def failure(self, settings: DecimSettings) -> None:
        self._failures += 1
        if self._failures >= settings.circuit_breaker_failures:
            self._opened_at = self._clock()

    def state(self) -> str:
        return "open" if self._opened_at is not None else "closed"


class DecimSafetyResolver:
    """Resolve an explicit Decim decision or a typed fallback reason."""

    def __init__(self, providers: dict[str, DecimSafetyProvider] | None = None,
                 breaker: DecimCircuitBreaker | None = None):
        self.providers = providers or {"systemone": SystemOneDecisionProvider()}
        self.breaker = breaker or DecimCircuitBreaker()

    def resolve(self, code: str, tool_type: str, agent_context: dict[str, Any] | None = None,
                settings: DecimSettings | None = None) -> DecimResolution:
        settings = settings or load_decim_settings()
        if not settings.enabled or settings.mode == "off":
            return DecimResolution(False, fallback_reason="disabled")
        if tool_type not in SCOPED_TOOL_TYPES:
            return DecimResolution(False, fallback_reason="out_of_scope")
        if not settings.provider or settings.provider not in self.providers:
            return DecimResolution(False, fallback_reason="provider_unconfigured")
        if not self.breaker.permits(settings):
            return DecimResolution(False, fallback_reason="circuit_open")
        try:
            packet = build_policy_packet(code, tool_type, _execution_boundary(agent_context),
                                         settings.max_payload_chars)
            decision = self.providers[settings.provider].decide(packet, settings)
            if decision.provider_key != settings.provider or decision.policy_version != POLICY_VERSION:
                raise DecimProviderError("invalid_response")
            if decision.decision not in VALID_CHOICES:
                raise DecimProviderError("invalid_response")
            if not math.isfinite(decision.confidence) or not 0.0 <= decision.confidence <= 1.0:
                raise DecimProviderError("invalid_response")
            if decision.confidence < settings.minimum_confidence:
                raise DecimProviderError("low_confidence")
        except DecimProviderError as exc:
            self.breaker.failure(settings)
            return DecimResolution(True, fallback_reason=exc.category)
        except Exception:
            logger.exception("Decim provider implementation failed")
            self.breaker.failure(settings)
            return DecimResolution(True, fallback_reason="provider_error")
        self.breaker.success()
        return DecimResolution(True, decision=decision, correlation_id=decision.correlation_id)


_resolver: DecimSafetyResolver | None = None


def get_decim_safety_resolver() -> DecimSafetyResolver:
    global _resolver
    if _resolver is None:
        _resolver = DecimSafetyResolver()
    return _resolver


def reset_decim_safety_resolver() -> None:
    """Reset the process-wide resolver (used by tests)."""
    global _resolver
    _resolver = None
