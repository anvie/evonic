"""Anthropic API key and Claude Code subscription setup routes."""

from flask import Blueprint, jsonify, request

from backend.provider.claude_code import (
    clear_credentials,
    complete_oauth_flow,
    read_claude_code_credentials,
    resolve_credential,
    save_secret,
    start_oauth_flow,
    use_claude_code_credentials,
)
from models.db import db


claude_bp = Blueprint("claude", __name__)


def _provider(provider_id):
    provider = db.get_provider(provider_id)
    return provider if provider and provider.get("api_format") == "anthropic" else None


@claude_bp.route("/api/providers/<provider_id>/auth/claude/status", methods=["GET"])
def claude_status(provider_id):
    provider = _provider(provider_id)
    if not provider:
        return jsonify({"error": "Anthropic provider not found."}), 404
    token, _ = resolve_credential(db, provider_id)
    existing = read_claude_code_credentials()
    return jsonify({
        "connected": bool(token),
        "provider_id": provider_id,
        "provider_kind": "claude",
        "credential_source": provider.get("credential_source") or "api_key",
        "existing_available": bool(existing and (existing.get("access_token") or existing.get("refresh_token"))),
        "existing_source": existing.get("source") if existing else None,
    })


@claude_bp.route("/api/providers/<provider_id>/auth/claude/use-existing", methods=["POST"])
def claude_use_existing(provider_id):
    if not _provider(provider_id):
        return jsonify({"error": "Anthropic provider not found."}), 404
    result = use_claude_code_credentials(db, provider_id)
    return jsonify(result), (400 if result.get("error") else 200)


@claude_bp.route("/api/providers/<provider_id>/auth/claude/oauth", methods=["POST"])
def claude_oauth_start(provider_id):
    if not _provider(provider_id):
        return jsonify({"error": "Anthropic provider not found."}), 404
    return jsonify(start_oauth_flow(provider_id))


@claude_bp.route("/api/providers/<provider_id>/auth/claude/oauth/complete", methods=["POST"])
def claude_oauth_complete(provider_id):
    if not _provider(provider_id):
        return jsonify({"error": "Anthropic provider not found."}), 404
    data = request.get_json(silent=True) or {}
    result = complete_oauth_flow(db, provider_id, data.get("code", ""))
    return jsonify(result), (400 if result.get("error") else 200)


@claude_bp.route("/api/providers/<provider_id>/auth/claude/secret", methods=["POST"])
def claude_secret(provider_id):
    if not _provider(provider_id):
        return jsonify({"error": "Anthropic provider not found."}), 404
    data = request.get_json(silent=True) or {}
    result = save_secret(db, provider_id, data.get("secret", ""))
    return jsonify(result), (400 if result.get("error") else 200)


@claude_bp.route("/api/providers/<provider_id>/auth/claude/disconnect", methods=["POST"])
def claude_disconnect(provider_id):
    if not _provider(provider_id):
        return jsonify({"error": "Anthropic provider not found."}), 404
    clear_credentials(db, provider_id)
    return jsonify({"success": True})
