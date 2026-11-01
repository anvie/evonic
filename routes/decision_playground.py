"""Safe, bounded Decision Playground invocation API.

The Playground is deliberately an inference-only caller of :class:`DecisionRuntime`.
It never writes profile/model configuration and never retains request context or raw
provider responses.  Durable telemetry remains the runtime's sanitized activity
summary only.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from backend.tools.lib.decision_models import ContractValidationError, SCHEMA_VERSION, parse_schema
from backend.tools.lib.decision_runtime import (
    MAX_TIMEOUT_MS,
    DecisionRuntime,
    DecisionRuntimeError,
    FixedWindowRateLimiter,
    RuntimeRequest,
)
from models.db import db


decision_playground_bp = Blueprint("decision_playground", __name__)

# Deliberately independent from production callers: one administrator/browser may
# make at most ten manual tests per minute.  DecisionRuntime additionally enforces
# its own channel rate limit and all payload/schema/secret bounds.
_PLAYGROUND_RATE_LIMITER = FixedWindowRateLimiter(limit=10, window_seconds=60.0)
_DIRECT_PROFILE_KEY = "playground.direct"
_MAX_BODY_BYTES = 64 * 1024


def _error(code: str, message: str, status: int = 400, path: str | None = None):
    error: dict[str, str] = {"code": code, "message": message}
    if path:
        error["path"] = path
    return jsonify({"success": False, "error": error}), status


def _payload() -> Mapping[str, Any] | None:
    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
        return None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    try:
        if len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _MAX_BODY_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return data


class _PlaygroundDatabase:
    """Read-only facade used for a direct test's in-memory profile.

    The runtime only receives this facade, making it impossible for a direct test
    to persist its temporary schema/model settings through the normal profile
    repository.  Activity writes are intentionally delegated to the real bounded
    activity repository.
    """

    def __init__(self, profile: Mapping[str, Any] | None = None) -> None:
        self.profile = dict(profile) if profile else None

    def get_decision_profile(self, key: str):
        if self.profile and key == self.profile["profile_key"]:
            return dict(self.profile)
        return db.get_decision_profile(key)

    def validate_decision_profile(self, profile: Mapping[str, Any], *, existing=None):
        # Ephemeral direct configuration is validated below using the same schema
        # parser and model compatibility verification performed by DecisionRuntime.
        if self.profile and profile.get("profile_key") == _DIRECT_PROFILE_KEY:
            return dict(profile)
        return db.validate_decision_profile(profile, existing=existing)

    def get_model_by_id(self, model_id: str):
        return db.get_model_by_id(model_id)

    def resolve_model_route(self, model_id: str):
        # Without this the runtime falls back to the raw model row, which has no
        # base_url when the provider owns the endpoint.
        return db.resolve_model_route(model_id)

    def record_decision_activity(self, event: Mapping[str, Any]):
        return db.record_decision_activity(event)


def _direct_profile(data: Mapping[str, Any]) -> dict[str, Any]:
    model_id = data.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ContractValidationError("required", "model_id", "must reference a configured model")
    try:
        schema = parse_schema(data.get("schema"))
    except ContractValidationError:
        raise
    timeout_ms = data.get("timeout_ms", 30_000)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ContractValidationError("out_of_range", "timeout_ms", f"must be an integer from 1 through {MAX_TIMEOUT_MS}")
    minimum_confidence = data.get("minimum_confidence", 0.0)
    if isinstance(minimum_confidence, bool) or not isinstance(minimum_confidence, (int, float)) or not 0 <= float(minimum_confidence) <= 1:
        raise ContractValidationError("out_of_range", "minimum_confidence", "must be a number from 0 through 1")
    return {
        "profile_key": _DIRECT_PROFILE_KEY,
        "display_name": "Temporary Playground test",
        "description": "",
        "enabled": True,
        "mode": "enforce",
        "schema_version": SCHEMA_VERSION,
        "policy_version": "playground/v1",
        "schema": schema.to_dict(),
        "primary_model_id": model_id.strip(),
        "fallback_model_id": None,
        "timeout_ms": timeout_ms,
        "retry_limit": 0,
        "minimum_confidence": float(minimum_confidence),
    }


def _result_payload(result) -> dict[str, Any]:
    """Return only normalized runtime data; raw provider bodies never enter here."""
    data = result.to_dict()
    has_logits = any(field.provider_logits for field in result.fields)
    data["display"] = {
        "provider_logits": "available" if has_logits else "unavailable",
        "candidate_scores": "available" if any(field.candidates for field in result.fields) else "unavailable",
    }
    return data


@decision_playground_bp.route("/api/admin/decision-playground/run", methods=["POST"])
def run_playground():
    """Run one authorized, bounded inference-only profile or direct-model test."""
    data = _payload()
    if data is None:
        return _error("invalid_payload", "must be a JSON object no larger than 64 KiB")
    mode = data.get("mode")
    if mode not in {"profile", "direct"}:
        return _error("invalid_mode", "mode must be profile or direct", path="mode")
    context = data.get("input_context")
    if not isinstance(context, Mapping):
        return _error("invalid_context", "input_context must be a JSON object", path="input_context")

    try:
        if mode == "profile":
            profile_key = data.get("profile_key")
            if not isinstance(profile_key, str) or not profile_key:
                return _error("required", "profile_key is required", path="profile_key")
            profile = db.get_decision_profile(profile_key)
            if not profile:
                return _error("profile_not_found", "Decision profile was not found.", 404)
            runtime_db = _PlaygroundDatabase()
            source = "profile_test"
        else:
            profile = _direct_profile(data)
            runtime_db = _PlaygroundDatabase(profile)
            source = "playground"
        correlation_id = f"playground-{uuid.uuid4()}"
        result = DecisionRuntime(runtime_db, rate_limiter=_PLAYGROUND_RATE_LIMITER).invoke(RuntimeRequest(
            profile_key=profile["profile_key"],
            profile_version=profile["policy_version"],
            input_context=dict(context),
            policy_metadata={},
            correlation_id=correlation_id,
            channel="playground",
            rate_limit_key=request.remote_addr or "anonymous",
            activity_source=source,
        ))
        return jsonify({"success": True, "mode": mode, "result": _result_payload(result)})
    except ContractValidationError as exc:
        return _error(exc.code, str(exc), path=exc.path)
    except DecisionRuntimeError as exc:
        status = 429 if exc.code.value == "rate_limited" else 400
        return _error(exc.code.value, str(exc), status)
