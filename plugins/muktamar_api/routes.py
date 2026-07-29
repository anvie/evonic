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
    try:
        supplied_bytes = supplied.encode("ascii")
    except UnicodeEncodeError:
        return False
    return bool(supplied_bytes) and any(
        hmac.compare_digest(supplied_bytes, expected.encode("ascii"))
        for expected in _keys() if expected.isascii()
    )


def _public_result(result: dict) -> dict:
    """Normalize validation output without discarding precise failure reasons."""
    success = bool(result.get("accepted"))
    message = result.get("user_message") or result.get("message")
    raw_codes = result.get("reason_code")
    if isinstance(raw_codes, str):
        reason_codes = [raw_codes] if raw_codes else []
    elif isinstance(raw_codes, (list, tuple)):
        reason_codes = [code for code in raw_codes if isinstance(code, str) and code]
    else:
        reason_codes = []
    if success and not reason_codes:
        reason_codes = ["OK"]

    public_result = {
        "success": success,
        "reason_code": reason_codes,
        **({"message": message} if isinstance(message, str) and message else {}),
    }
    model_index = result.get("model_index")
    if isinstance(model_index, int) and not isinstance(model_index, bool) and model_index >= 0:
        public_result["model_index"] = model_index
    return public_result


def create_blueprint():
    bp = Blueprint(PLUGIN_ID, __name__)

    @bp.post("/plugin/muktamar-api/v1/photo/validate")
    def validate_photo():
        if not _authorized():
            return jsonify({"error": "Unauthorized"}), 401
        try:
            cfg = _config()
        except Exception:
            return jsonify({"error": "Photo validator is unavailable"}), 503
        try:
            max_bytes = max(1, int(cfg.get("MAX_UPLOAD_BYTES", 8 * 1024 * 1024)))
        except (TypeError, ValueError):
            max_bytes = 8 * 1024 * 1024
        if request.content_length and request.content_length > max_bytes + 65536:
            return jsonify({"error": "Upload too large"}), 413
        upload = request.files.get("photo")
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
            # Standalone validation intentionally has no draft or agent dependency.
            try:
                module = tool_registry._load_tool_module("validate_photo", skill_id="muktamar-agent")
            except Exception:
                return jsonify({"error": "Photo validator is unavailable"}), 503
            if module is None or not hasattr(module, "execute_standalone"):
                return jsonify({"error": "Photo validator is unavailable"}), 503
            try:
                result = module.execute_standalone({}, {"attachment_path": path})
            except Exception:
                return jsonify({"error": "Photo validator is unavailable"}), 503
            if not isinstance(result, dict) or result.get("status") == "error":
                return jsonify({"error": "Photo validator is unavailable"}), 503
            return jsonify(_public_result(result)), 200
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    return bp
