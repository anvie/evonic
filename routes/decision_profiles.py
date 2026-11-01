"""Authenticated management API for durable Decision Profiles.

Global authentication and CSRF middleware in :mod:`app` protects this blueprint.
The responses intentionally contain only model IDs, never model credentials.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from models.db import db
from models.mixins.decision_profiles import DecisionProfileValidationError


decision_profiles_bp = Blueprint("decision_profiles", __name__)


def _error(exc: DecisionProfileValidationError, status: int = 400):
    return jsonify({
        "success": False,
        "error": {
            "code": exc.code,
            "path": exc.path,
            "message": str(exc),
        },
    }), status


def _payload() -> Mapping[str, Any] | None:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _cursor(value: str | None) -> tuple[float, str] | None:
    if not value:
        return None
    try:
        created_at, event_id = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        return float(created_at), str(event_id)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid cursor") from None


def _next_cursor(events: list[Mapping[str, Any]]) -> str | None:
    if not events:
        return None
    event = events[-1]
    data = json.dumps([event["created_at"], event["id"]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode()


@decision_profiles_bp.route("/api/admin/decision-profiles", methods=["GET"])
def list_decision_profiles():
    """List profiles, including disabled profiles unless ``enabled_only=1``."""
    enabled_only = request.args.get("enabled_only") in {"1", "true", "True"}
    return jsonify({"profiles": db.list_decision_profiles(include_disabled=not enabled_only)})


@decision_profiles_bp.route("/api/admin/decision-profiles", methods=["POST"])
def create_decision_profile():
    """Create a profile with a stable, never-renamed key."""
    data = _payload()
    if data is None:
        return jsonify({"success": False, "error": {"code": "invalid_payload", "path": "$", "message": "must be a JSON object"}}), 400
    try:
        profile = db.create_decision_profile(data)
    except DecisionProfileValidationError as exc:
        return _error(exc, 409 if exc.code == "already_exists" else 400)
    return jsonify({"success": True, "profile": profile}), 201


@decision_profiles_bp.route("/api/admin/decision-profiles/<path:profile_key>", methods=["GET"])
def get_decision_profile(profile_key: str):
    profile = db.get_decision_profile(profile_key)
    if not profile:
        return jsonify({"success": False, "error": {"code": "not_found", "path": "profile_key", "message": "profile not found"}}), 404
    return jsonify({"profile": profile})


@decision_profiles_bp.route("/api/admin/decision-profiles/<path:profile_key>", methods=["PUT"])
def update_decision_profile(profile_key: str):
    """Replace/update a profile using its current revision for conflict detection."""
    data = _payload()
    if data is None:
        return jsonify({"success": False, "error": {"code": "invalid_payload", "path": "$", "message": "must be a JSON object"}}), 400
    revision = data.pop("revision", None)
    try:
        profile = db.update_decision_profile(profile_key, data, revision)
    except DecisionProfileValidationError as exc:
        return _error(exc, 409 if exc.code == "conflict" else 400)
    if not profile:
        return jsonify({"success": False, "error": {"code": "not_found", "path": "profile_key", "message": "profile not found"}}), 404
    return jsonify({"success": True, "profile": profile})


@decision_profiles_bp.route("/api/admin/decision-profiles/<path:profile_key>", methods=["DELETE"])
def delete_decision_profile(profile_key: str):
    """Delete non-built-in profiles; built-ins are safely deactivated by PUT."""
    try:
        deleted = db.delete_decision_profile(profile_key)
    except DecisionProfileValidationError as exc:
        return _error(exc, 409)
    if not deleted:
        return jsonify({"success": False, "error": {"code": "not_found", "path": "profile_key", "message": "profile not found"}}), 404
    return jsonify({"success": True})


@decision_profiles_bp.route("/api/admin/decision-activity", methods=["GET"])
def list_decision_activity():
    """List bounded activity metadata using authorized, cursor-based pagination."""
    try:
        limit = int(request.args.get("limit", "50"))
        cursor = _cursor(request.args.get("cursor"))
        filters: dict[str, Any] = {}
        for key in ("source", "profile_key", "model_id", "result_class", "error_code", "correlation_id", "from", "to"):
            if request.args.get(key) is not None:
                filters[key] = request.args[key]
        if request.args.get("fallback_used") is not None:
            value = request.args["fallback_used"].lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError("invalid fallback_used")
            filters["fallback_used"] = value in {"true", "1"}
        events = db.list_decision_activity(limit=limit + 1, cursor=cursor, filters=filters)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": {"code": "invalid_query", "message": "query parameters are invalid"}}), 400
    page_size = max(1, min(limit, 100))
    has_more = len(events) > page_size
    events = events[:page_size]
    return jsonify({"events": events, "next_cursor": _next_cursor(events) if has_more else None})


@decision_profiles_bp.route("/api/admin/decision-activity", methods=["DELETE"])
def clear_decision_activity():
    return jsonify({"success": True, "deleted": db.clear_decision_activity()})


@decision_profiles_bp.route("/api/admin/decision-activity/<event_id>", methods=["GET"])
def get_decision_activity(event_id: str):
    event = db.get_decision_activity(event_id)
    if not event:
        return jsonify({"success": False, "error": {"code": "not_found", "message": "activity event not found"}}), 404
    return jsonify({"event": event})


@decision_profiles_bp.route("/api/admin/decision-activity/<event_id>/redact", methods=["POST"])
def redact_decision_activity(event_id: str):
    if not db.redact_decision_activity(event_id):
        return jsonify({"success": False, "error": {"code": "not_found", "message": "activity event not found"}}), 404
    return jsonify({"success": True})


@decision_profiles_bp.route("/api/admin/decision-activity/purge", methods=["POST"])
def purge_decision_activity():
    data = _payload()
    try:
        before = float(data["before"]) if data else None
        if before is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify({"success": False, "error": {"code": "invalid_payload", "message": "before must be a Unix timestamp"}}), 400
    return jsonify({"success": True, "deleted": db.purge_decision_activity(before)})
