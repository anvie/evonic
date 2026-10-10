"""Admin API and dashboard for Decim Safety telemetry.

Decim Safety is Evonic's generic, provider-agnostic *decision model* safety
layer.  Everything exposed here is provider-neutral: the telemetry and health
surfaces never return the provider endpoint, model product name, or credential.
The routes are protected by the global authentication guard and CSRF
middleware (like every other admin surface), and
all list filters are validated server-side.

Endpoints:

* ``GET  /system/decim-safety``                legacy redirect → /system/safety#dmss
* ``GET  /api/admin/decim-safety/health``      operational status
* ``GET  /api/admin/decim-safety/summary``     aggregate statistics
* ``GET  /api/admin/decim-safety/metrics``     summary + time-bucketed trend
* ``GET  /api/admin/decim-safety/events``      paged, filtered event list
* ``GET  /api/admin/decim-safety/events/<id>`` single event
* ``POST /api/admin/decim-safety/telemetry/clear`` audited retention reset
"""
from __future__ import annotations

import logging
import re
import threading
import time

from flask import Blueprint, jsonify, redirect, request, session

logger = logging.getLogger(__name__)

decim_safety_bp = Blueprint("decim_safety", __name__)

_VALID_MODES = ("off", "shadow", "enforce")
_VALID_TOOLS = ("bash", "python")
_VALID_LEVELS = ("safe", "warning", "requires_approval", "dangerous")


def _parse_positive_int(raw, *, default: int, minimum: int = 1, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


@decim_safety_bp.route("/system/decim-safety")
def decim_safety_page():
    """Legacy dashboard route — now consolidated under System > Safety.

    Redirects to the DMSS tab of the consolidated Safety page so existing
    bookmarks and links keep resolving (task #836). The API surface below is
    unchanged.
    """
    return redirect("/system/safety#dmss")


@decim_safety_bp.route("/api/admin/decim-safety/health", methods=["GET"])
def api_decim_safety_health():
    """Operational status with a redacted provider view."""
    from backend.tools.lib.decim_safety import get_decim_safety_resolver, load_decim_settings

    try:
        settings = load_decim_settings()
    except Exception:
        logger.exception("Unable to load Decim Safety settings for health endpoint")
        return jsonify({"error": "unavailable"}), 500

    circuit_state = "closed"
    try:
        circuit_state = get_decim_safety_resolver().breaker.state()
    except Exception:
        logger.exception("Unable to read Decim Safety circuit state")

    from backend.services import decim_safety_metrics, decim_safety_telemetry

    summary = decim_safety_metrics.summary(window_hours=24)
    state = decim_safety_telemetry.get_state()

    return jsonify({
        "enabled": settings.enabled,
        "mode": settings.mode,
        # Provider key only: this health surface never echoes the endpoint.
        "provider": settings.provider if settings.enabled else "",
        "circuit_state": circuit_state,
        "recording": settings.mode == "shadow" or (
            settings.mode == "enforce" and settings.record_enforce_events
        ),
        "last_decision_at": state.get("last_decision_at"),
        # Aggregate timing only; never expose transport endpoint/hostname.
        "latency_ms": (summary.get("latency_ms") or {}).copy(),
        "fallback_rate_24h": summary.get("fallback_rate"),
        "total_recorded": state.get("total_recorded"),
        "telemetry_epoch": state.get("telemetry_epoch"),
        "retention_days": settings.retention_days,
    })


@decim_safety_bp.route("/api/admin/decim-safety/summary", methods=["GET"])
def api_decim_safety_summary():
    """Aggregate statistics for the dashboard cards."""
    from backend.services import decim_safety_metrics

    window = _parse_positive_int(request.args.get("window_hours"),
                                  default=24 * 7, minimum=1, maximum=24 * 365)
    mode = request.args.get("mode")
    tool_type = request.args.get("tool_type")
    if mode not in _VALID_MODES:
        mode = None
    if tool_type not in _VALID_TOOLS:
        tool_type = None
    return jsonify(decim_safety_metrics.summary(window, mode=mode, tool_type=tool_type))


@decim_safety_bp.route("/api/admin/decim-safety/metrics", methods=["GET"])
def api_decim_safety_metrics():
    """Summary plus a time-bucketed trend for the charts."""
    from backend.services import decim_safety_metrics

    window = _parse_positive_int(request.args.get("window_hours"),
                                  default=24 * 7, minimum=1, maximum=24 * 365)
    bucket = request.args.get("bucket", "day")
    if bucket not in ("hour", "day"):
        bucket = "day"
    mode = request.args.get("mode")
    tool_type = request.args.get("tool_type")
    if mode not in _VALID_MODES:
        mode = None
    if tool_type not in _VALID_TOOLS:
        tool_type = None
    return jsonify({
        "summary": decim_safety_metrics.summary(window, mode=mode, tool_type=tool_type),
        "trend": decim_safety_metrics.trend(window, bucket, mode=mode, tool_type=tool_type),
    })


@decim_safety_bp.route("/api/admin/decim-safety/events", methods=["GET"])
def api_decim_safety_events():
    """Paged, server-side-validated list of sanitized comparison events."""
    from backend.services import decim_safety_telemetry

    limit = _parse_positive_int(request.args.get("limit"), default=50, minimum=1, maximum=200)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    mode = request.args.get("mode")
    tool_type = request.args.get("tool_type")
    final_level = request.args.get("final_level")
    if mode not in _VALID_MODES:
        mode = None
    if tool_type not in _VALID_TOOLS:
        tool_type = None
    if final_level not in _VALID_LEVELS:
        final_level = None
    unsafe_only = request.args.get("unsafe_only", "").lower() in ("1", "true", "yes")
    since = request.args.get("since") or None

    return jsonify(decim_safety_telemetry.list_events(
        limit=limit, offset=offset, mode=mode, tool_type=tool_type,
        final_level=final_level, unsafe_only=unsafe_only, since=since,
    ))


@decim_safety_bp.route("/api/admin/decim-safety/events/<event_id>", methods=["GET"])
def api_decim_safety_event(event_id):
    """Fetch a single sanitized event; never returns raw command text."""
    from backend.services import decim_safety_telemetry

    event = decim_safety_telemetry.get_event(event_id)
    if not event:
        return jsonify({"error": "not found"}), 404
    return jsonify(event)


@decim_safety_bp.route("/api/admin/decim-safety/telemetry/clear", methods=["POST"])
def api_decim_safety_clear():
    """Audited retention reset.  Never changes mode/configuration or decisions."""
    from backend.services import decim_safety_telemetry

    result = decim_safety_telemetry.clear(actor="admin")
    status = 200 if result.get("success") else 500
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Diagnostic tester (task #839)
#
# A bounded, *diagnostic only* probe of the configured DMSS provider: the
# pasted payload is treated as data (never executed), the probe does not alter
# production decisions, and it does not write to the telemetry store.  The
# probe runs through a fresh resolver so its failures cannot trip the
# production circuit breaker; the production breaker state is reported
# read-only for context.
# ---------------------------------------------------------------------------

# The probe costs a real provider round-trip, so it carries its own stricter
# budget on top of the global API rate-limit tier.
_TESTER_MAX_BODY_BYTES = 128 * 1024
_TESTER_MAX_REQUESTS = 10
_TESTER_WINDOW_SECONDS = 60
_TESTER_REASON_MAX = 300

# In-memory sliding window per session: key -> [monotonic timestamps].
_tester_windows: dict[str, list[float]] = {}
_tester_windows_lock = threading.Lock()

# Internal absolute paths that must not reach the browser in a provider reason.
_TESTER_PATH_PATTERN = re.compile(r"(?:(?:/[A-Za-z0-9._-]+){2,}|[A-Za-z]:\\[^\s]*)")
# Secret-bearing markers (mirrors the library payload check): if present in the
# provider's own explanation, redact the whole reason rather than risk leaking
# a credential fragment.
_TESTER_SECRETISH_PATTERN = re.compile(
    r"(?ix)(?:\b(?:api[_-]?key|token|password|secret|authorization)\b\s*(?:=|:)\s*|"
    r"\b(?:sk|ghp|github_pat)_[a-z0-9_-]{12,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)

# Human-readable descriptions of typed fallback categories (provider-neutral).
_TESTER_FALLBACK_TEXT = {
    "payload_too_large": "Payload exceeds the configured maximum size and was not sent to the provider.",
    "sensitive_payload": "Payload looks like it contains a secret (API key/token/private key) and was not sent to the provider.",
    "provider_unconfigured": "No provider endpoint is configured.",
    "transport_error": "Provider unreachable or the request timed out.",
    "http_error": "Provider returned a non-2xx HTTP status.",
    "invalid_response": "Provider returned a malformed or unexpected response.",
    "low_confidence": "Provider confidence is below the configured minimum.",
    "redirect_rejected": "Provider redirected the request (redirects are rejected).",
    "response_too_large": "Provider response exceeded the size limit.",
    "provider_error": "Unexpected provider failure.",
}


def _tester_rate_limit_key() -> str:
    """Session identity for the tester budget (user when authenticated, else IP)."""
    if session.get("authenticated"):
        return f"user:{session.get('_user_id', 'admin')}"
    return f"ip:{request.remote_addr or '0.0.0.0'}"


def _tester_rate_limit() -> int | None:
    """Record one probe; return seconds to wait when the budget is exhausted."""
    now = time.monotonic()
    key = _tester_rate_limit_key()
    with _tester_windows_lock:
        window = [t for t in _tester_windows.get(key, []) if now - t < _TESTER_WINDOW_SECONDS]
        if len(window) >= _TESTER_MAX_REQUESTS:
            _tester_windows[key] = window
            return max(1, int(window[0] + _TESTER_WINDOW_SECONDS - now) + 1)
        window.append(now)
        _tester_windows[key] = window
        return None


def _reset_tester_rate_limit() -> None:
    """Clear the in-memory tester budget (used by tests)."""
    with _tester_windows_lock:
        _tester_windows.clear()


def _sanitize_tester_reason(text: str) -> str:
    """Bound and scrub a provider reason: no secrets, no internal paths."""
    collapsed = " ".join(str(text).split())
    if _TESTER_SECRETISH_PATTERN.search(collapsed):
        return "Provider reason redacted (possible secret)."
    collapsed = _TESTER_PATH_PATTERN.sub("[path]", collapsed)
    return collapsed[:_TESTER_REASON_MAX]


def _tester_result(settings, decision, fallback_reason, payload, tool_type) -> dict:
    """Assemble the diagnostic response (provider-neutral, bounded)."""
    from backend.tools.lib.decim_safety import get_decim_safety_resolver

    circuit_state = "closed"
    try:
        circuit_state = get_decim_safety_resolver().breaker.state()
    except Exception:
        logger.exception("Unable to read Decim Safety circuit state for tester")

    dmss_active = bool(settings.enabled) and settings.mode != "off"
    if decision is not None:
        if fallback_reason == "low_confidence":
            reason = (
                f"Provider returned '{decision.decision}' at {decision.confidence:.2f} confidence, "
                f"below the configured minimum ({settings.minimum_confidence}) — "
                f"production would fall back to HMADS."
            )
        elif decision.reason:
            reason = _sanitize_tester_reason(decision.reason)
        else:
            reason = (
                f"Provider classified the payload as '{decision.decision}' "
                f"with {decision.confidence:.2f} confidence."
            )
        body = {
            "decision": decision.decision,
            "confidence": decision.confidence,
            "model": decision.model_id,
            "latency_ms": decision.latency_ms,
            "correlation_id": decision.correlation_id,
        }
    else:
        if fallback_reason == "payload_too_large":
            reason = _TESTER_FALLBACK_TEXT[fallback_reason] + f" (max {settings.max_payload_chars} chars)"
        else:
            reason = _TESTER_FALLBACK_TEXT.get(fallback_reason, "Provider probe failed.")
        body = {
            "decision": None,
            "confidence": None,
            "model": None,
            "latency_ms": None,
            "correlation_id": None,
        }

    return {
        "success": True,
        "diagnostic": True,
        "provider": settings.provider,
        "mode": settings.mode,
        "dmss_active": dmss_active,
        "circuit_state": circuit_state,
        "payload_chars": len(payload),
        "tool_type": tool_type,
        "fallback_reason": fallback_reason,
        "reason": reason,
        **body,
    }


@decim_safety_bp.route("/api/admin/decim-safety/test", methods=["POST"])
def api_decim_safety_test():
    """Bounded diagnostic probe of the configured DMSS provider.

    Request: ``{"payload": "<sample text>", "tool_type": "bash"|"python"}``.
    The payload is bounded by ``max_payload_chars``, the provider call by
    ``request_timeout_ms``, and the endpoint by a per-session 10 req/min
    budget.  The response carries decision, confidence, a sanitized reason,
    latency and the provider/model used.  Success means the probe was
    processed; a provider failure is itself a diagnostic outcome
    (``decision: null`` + ``fallback_reason``).
    """
    from backend.tools.lib.decim_safety import (
        DecimProviderError, DecimSafetyResolver, SCOPED_TOOL_TYPES,
        build_policy_packet, load_decim_settings,
    )

    if (request.content_length or 0) > _TESTER_MAX_BODY_BYTES:
        return jsonify({"success": False, "error": "payload too large for a diagnostic probe"}), 413

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "expected a JSON object"}), 400
    payload = data.get("payload")
    if not isinstance(payload, str) or not payload.strip():
        return jsonify({"success": False, "error": "payload must be a non-empty string"}), 400
    tool_type = data.get("tool_type", "bash")
    if tool_type not in SCOPED_TOOL_TYPES:
        return jsonify({"success": False, "error": "tool_type must be bash or python"}), 400

    retry_after = _tester_rate_limit()
    if retry_after is not None:
        resp = jsonify({"success": False, "error": "rate limit exceeded", "retry_after": retry_after})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    settings = load_decim_settings()
    # Fresh resolver: probe failures must not trip the production breaker.
    resolver = DecimSafetyResolver()
    provider = resolver.providers.get(settings.provider)
    if provider is None:
        return jsonify(_tester_result(settings, None, "provider_unconfigured", payload, tool_type)), 200

    decision = None
    fallback_reason = None
    try:
        # Bounded by settings.max_payload_chars; secret-looking payloads stay local.
        packet = build_policy_packet(payload, tool_type, "sandboxed_docker",
                                     settings.max_payload_chars)
        # decide() enforces settings.request_timeout_ms for the provider call.
        decision = provider.decide(packet, settings)
    except DecimProviderError as exc:
        fallback_reason = exc.category
    except Exception:
        logger.exception("Decim diagnostic probe failed unexpectedly")
        fallback_reason = "provider_error"

    if decision is not None and decision.confidence < settings.minimum_confidence:
        fallback_reason = "low_confidence"

    return jsonify(_tester_result(settings, decision, fallback_reason, payload, tool_type)), 200
