"""Admin API and dashboard for Decim Safety telemetry.

Decim Safety is Evonic's generic, provider-agnostic *decision model* safety
layer.  Everything exposed here is provider-neutral: no endpoint, model product
name, or credential is ever returned.  The routes are protected by the global
authentication guard and CSRF middleware (like every other admin surface), and
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

from flask import Blueprint, jsonify, redirect, request

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
        # Provider key only: endpoint and model identity are private deployment
        # configuration and are intentionally never returned.
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
