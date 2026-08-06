"""Codex OAuth routes — PKCE flow, status, disconnect."""

from flask import Blueprint, jsonify, request, render_template_string

from models.db import db
from backend.provider.oauth_codex import (
    REDIRECT_URI,
    start_auth_flow,
    check_auth_status,
    exchange_code_for_tokens,
    store_tokens,
    get_valid_token,
    clear_tokens,
    receive_callback,
    process_callback_url,
    poll_device_auth_flow,
    read_codex_cli_credentials,
    start_device_auth_flow,
    use_codex_cli_credentials,
)

codex_bp = Blueprint("codex", __name__)


def _find_codex_provider(provider_id=None):
    """Resolve only Codex-format providers; OAuth alone is not provider identity."""
    if provider_id:
        provider = db.get_provider(provider_id)
        return provider if provider and provider.get("api_format") == "codex" else None
    for p in db.get_providers():
        if p.get("api_format") == "codex":
            return p
    return None


_CALLBACK_HTML = """<!DOCTYPE html>
<html><head><title>Evonic - Codex Auth</title></head>
<body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f9fafb">
<div style="text-align:center;padding:2rem"><h2>{{ message }}</h2>
<p>Returning to Evonic...</p>
<script>setTimeout(function(){window.close()},2000)</script>
</div></body></html>"""


@codex_bp.route("/auth/callback", methods=["GET"])
def codex_auth_callback():
    """Receive the OAuth redirect from OpenAI and store the authorization code."""
    provider = _find_codex_provider()
    if not provider:
        return render_template_string(_CALLBACK_HTML, message="No Codex provider configured."), 404

    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        desc = request.args.get("error_description", error)
        return render_template_string(_CALLBACK_HTML, message=f"Authentication failed: {desc}")

    if not code:
        return render_template_string(_CALLBACK_HTML, message="No authorization code received.")

    result = receive_callback(provider["id"], code, state)
    if not result["success"]:
        return render_template_string(_CALLBACK_HTML, message=f"Authentication failed: {result['error']}")

    return render_template_string(_CALLBACK_HTML, message="Authentication successful! You can close this tab.")


@codex_bp.route("/api/provider/codex/status", methods=["GET"], defaults={"provider_id": None})
@codex_bp.route("/api/providers/<provider_id>/auth/codex/status", methods=["GET"])
def codex_status(provider_id):
    provider = _find_codex_provider(provider_id)
    if not provider:
        return jsonify({"connected": False, "provider_id": None})

    token = get_valid_token(db, provider["id"])
    connected = token is not None
    expires_at = provider.get("token_expires_at", 0) or 0

    cli_creds = read_codex_cli_credentials()
    return jsonify({
        "connected": connected,
        "provider_id": provider["id"],
        "expires_at": expires_at,
        "credential_source": provider.get("credential_source") or "",
        "existing_available": cli_creds is not None,
        "existing_source": cli_creds.get("source") if cli_creds else None,
        "provider_kind": "codex",
    })


@codex_bp.route("/api/providers/<provider_id>/auth/codex/use-existing", methods=["POST"])
def codex_use_existing(provider_id):
    provider = _find_codex_provider(provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Codex provider not found."}), 404
    result = use_codex_cli_credentials(db, provider_id)
    return jsonify(result), (400 if result.get("error") else 200)


@codex_bp.route("/api/providers/<provider_id>/auth/codex/device", methods=["POST"])
def codex_device_start(provider_id):
    if not _find_codex_provider(provider_id):
        return jsonify({"success": False, "error": "Codex provider not found."}), 404
    result = start_device_auth_flow(provider_id)
    return jsonify(result), (502 if result.get("error") else 200)


@codex_bp.route("/api/providers/<provider_id>/auth/codex/device/poll", methods=["POST"])
def codex_device_poll(provider_id):
    if not _find_codex_provider(provider_id):
        return jsonify({"status": "error", "error": "Codex provider not found."}), 404
    result = poll_device_auth_flow(provider_id)
    if result.get("status") == "complete":
        store_tokens(db, provider_id, result.pop("tokens"), credential_source="evonic_oauth")
    return jsonify(result)


@codex_bp.route("/api/provider/codex/connect", methods=["POST"])
def codex_connect():
    """Start the OAuth PKCE flow — returns an auth URL to open in the browser."""
    provider = _find_codex_provider()
    if not provider:
        return jsonify({"error": "No Codex provider configured. Create one first."}), 404

    result = start_auth_flow(provider["id"])
    return jsonify({
        "success": True,
        "auth_url": result["auth_url"],
    })


@codex_bp.route("/api/provider/codex/poll", methods=["POST"])
def codex_poll():
    """Poll to check if the user has completed OAuth authorization."""
    provider = _find_codex_provider()
    if not provider:
        return jsonify({"status": "error", "error": "No Codex provider"}), 404

    pid = provider["id"]
    status = check_auth_status(pid)

    if status["status"] == "code_received":
        tokens = exchange_code_for_tokens(pid)
        if "error" in tokens:
            return jsonify({"status": "error", "error": tokens["error"]})

        store_tokens(db, pid, tokens)
        return jsonify({"status": "complete"})

    if status["status"] == "error":
        return jsonify({"status": "error", "error": status.get("error", "Unknown error")})

    if status["status"] == "expired":
        return jsonify({"status": "expired", "error": "Authorization timed out. Please try again."})

    if status["status"] == "no_pending":
        return jsonify({"status": "error", "error": "No pending authorization. Start the flow again."})

    return jsonify({"status": "pending"})


@codex_bp.route("/api/provider/codex/callback", methods=["POST"])
def codex_paste_callback():
    """Receive a pasted callback URL from a remote user."""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"success": False, "error": "No callback URL provided."}), 400

    provider = _find_codex_provider()
    if not provider:
        return jsonify({"success": False, "error": "No Codex provider configured."}), 404

    result = process_callback_url(url)
    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error", "Failed to process callback.")}), 400

    # Exchange the code for tokens immediately
    tokens = exchange_code_for_tokens(provider["id"])
    if "error" in tokens:
        return jsonify({"success": False, "error": tokens["error"]}), 500

    store_tokens(db, provider["id"], tokens)
    return jsonify({"success": True, "status": "complete"})


@codex_bp.route("/api/provider/codex/disconnect", methods=["POST"], defaults={"provider_id": None})
@codex_bp.route("/api/providers/<provider_id>/auth/codex/disconnect", methods=["POST"])
def codex_disconnect(provider_id):
    provider = _find_codex_provider(provider_id)
    if not provider:
        return jsonify({"success": False, "error": "No Codex provider found"}), 404

    clear_tokens(db, provider["id"])
    return jsonify({"success": True})
