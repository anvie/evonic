"""Authenticated external API for Muktamar photo validation."""
from __future__ import annotations

import hmac
import os
import tempfile
from typing import Any

from flask import Blueprint, jsonify, request

from backend.tools import tool_registry
from models.db import db

PLUGIN_ID = "muktamar_api"


def _config():
    from backend.plugin_manager import plugin_manager
    return plugin_manager.get_plugin_config(PLUGIN_ID) or {}


def _keys():
    # Config is the supported storage boundary; never log or return these values.
    raw = _config().get("API_KEYS") or os.environ.get("MUKTAMAR_API_KEYS", "")
    return tuple(key.strip() for key in str(raw).split(",") if key.strip())


def _authorized():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    supplied = header[7:].strip()
    return bool(supplied) and any(hmac.compare_digest(supplied, expected) for expected in _keys())


def _public_result(result: dict) -> dict:
    """Remove internal paths/fingerprints/provider details from the API response."""
    return {
        "accepted": bool(result.get("accepted", False)),
        "reason_code": result.get("reason_code") if result.get("accepted") is not None else "ERROR",
        "user_message": result.get("user_message", ""),
        "checks": result.get("checks", {}),
    }


def create_blueprint():
    bp = Blueprint(PLUGIN_ID, __name__)

    @bp.post("/plugin/muktamar-api/v1/photo/validate")
    def validate_photo():
        if not _authorized():
            return jsonify({"error": "Unauthorized"}), 401
        cfg = _config()
        try:
            max_bytes = max(1, int(cfg.get("MAX_UPLOAD_BYTES", 8 * 1024 * 1024)))
        except (TypeError, ValueError):
            max_bytes = 8 * 1024 * 1024
        if request.content_length and request.content_length > max_bytes + 65536:
            return jsonify({"error": "Upload too large"}), 413
        draft_raw = request.form.get("draft_id", "")
        try:
            draft_id = int(draft_raw)
            if draft_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "draft_id must be a positive integer"}), 400
        upload = request.files.get("photo") or request.files.get("foto")
        if upload is None or not upload.filename:
            return jsonify({"error": "multipart field 'photo' is required"}), 400

        # The existing tool resolves only trusted internal paths. Store the request
        # in a private temporary file and remove it regardless of validator outcome.
        fd, path = tempfile.mkstemp(prefix="muktamar-photo-", suffix=".upload")
        try:
            with os.fdopen(fd, "wb") as target:
                total = 0
                while True:
                    chunk = upload.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        return jsonify({"error": "Upload too large"}), 413
                    target.write(chunk)
            agent_id = cfg.get("AGENT_ID") or "muktamar-agent"
            # Skill directories use a hyphen in their on-disk ID and cannot be
            # imported as a normal Python package. Use the same registry boundary
            # as the agent runtime so relative imports and future tool changes stay
            # consistent with normal tool execution.
            agent = db.get_agent(agent_id)
            if not agent or not agent.get("enabled", True):
                return jsonify({"error": "Configured Muktamar agent is unavailable"}), 503
            module = tool_registry._load_tool_module("validate_photo", skill_id="muktamar-agent")
            if module is None or not hasattr(module, "execute"):
                return jsonify({"error": "Photo validator is unavailable"}), 503
            result = module.execute(agent, {"draft_id": draft_id, "attachment_path": path})
            if result.get("status") == "error":
                return jsonify({"error": result.get("error", "Validation failed")}), 503
            return jsonify(_public_result(result)), 200
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    return bp
