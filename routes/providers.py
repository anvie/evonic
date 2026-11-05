from typing import Any, Dict

import requests
from flask import Blueprint, jsonify, request

from backend.tools.lib.model_route_adapters import DEFAULT_ROUTE_ADAPTER_REGISTRY
from backend.tools.lib.model_routing_contracts import AdapterKey, validate_endpoint
from models.db import db

providers_bp = Blueprint("providers", __name__)

_SENSITIVE_KEYS = frozenset({"api_key", "refresh_token"})


def _sanitize(provider: Dict[str, Any]) -> Dict[str, Any]:
    if provider.get("api_format") == "claude_cli":
        from backend.provider.claude_cli import get_status
        provider["credential_configured"] = get_status()["logged_in"]
    else:
        provider["credential_configured"] = bool(provider.get("api_key"))
    for key in _SENSITIVE_KEYS:
        provider.pop(key, None)
    return provider


def _provider_adapter(provider: Dict[str, Any]):
    """Resolve a provider diagnostic through the versioned adapter registry."""
    try:
        adapter = DEFAULT_ROUTE_ADAPTER_REGISTRY.get(AdapterKey(provider["adapter_key"]))
        endpoint = validate_endpoint(provider.get("endpoint") or provider.get("base_url", ""))
    except (KeyError, ValueError):
        return None, None
    return adapter, endpoint


@providers_bp.route("/api/providers", methods=["GET"])
def api_list_providers():
    providers = db.get_providers()
    for p in providers:
        _sanitize(p)
        p["model_count"] = len(db.get_models_by_provider(p["id"]))
    return jsonify({"providers": providers})


@providers_bp.route("/api/providers/<provider_id>", methods=["GET"])
def api_get_provider(provider_id):
    provider = db.get_provider(provider_id)
    if not provider:
        return jsonify({"error": "Provider not found"}), 404
    return jsonify(_sanitize(provider))


@providers_bp.route("/api/providers", methods=["POST"])
def api_create_provider():
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"success": False, "error": "name is required"}), 400

    pid = data.get("id") or data["name"].lower().replace(" ", "_")
    existing = db.get_provider(pid)
    if existing:
        return jsonify({"success": False, "error": f"Provider '{pid}' already exists"}), 409

    try:
        new_id = db.create_provider(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception:
        return jsonify({"success": False, "error": "Unable to create provider"}), 500

    return jsonify({"success": True, "provider_id": new_id})


@providers_bp.route("/api/providers/<provider_id>", methods=["PUT"])
def api_update_provider(provider_id):
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No data provided"}), 400

    provider = db.get_provider(provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    if "api_key" in data and not data["api_key"]:
        del data["api_key"]

    try:
        success = db.update_provider(provider_id, data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    if not success:
        return jsonify({"success": False, "error": "No changes made"}), 400

    return jsonify({"success": True})


@providers_bp.route("/api/providers/<provider_id>", methods=["DELETE"])
def api_delete_provider(provider_id):
    provider = db.get_provider(provider_id)
    if not provider:
        return jsonify({"success": False, "error": "Provider not found"}), 404

    if request.args.get("cascade") in ("1", "true"):
        try:
            removed = db.delete_provider_cascade(provider_id)
        except ValueError:
            return jsonify({
                "success": False,
                "error": "One of its models is the global default. Set another model as default first.",
            }), 409
        return jsonify({"success": True, "models_removed": removed})

    success = db.delete_provider(provider_id)
    if not success:
        return jsonify(
            {"success": False, "error": "Cannot delete provider that has models. Remove its models first."}
        ), 409

    return jsonify({"success": True})


@providers_bp.route("/api/providers/<provider_id>/delete-impact", methods=["GET"])
def api_provider_delete_impact(provider_id):
    """Preview for the delete dialog: its models, the agents using them, and any default."""
    if not db.get_provider(provider_id):
        return jsonify({"success": False, "error": "Provider not found"}), 404
    return jsonify({"success": True, **db.get_provider_delete_impact(provider_id)})


@providers_bp.route("/api/providers/<provider_id>/fetch-models", methods=["POST"])
def api_fetch_provider_models(provider_id):
    """Fetch available models from a provider's API endpoint."""
    provider = db.get_provider(provider_id)
    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    if provider.get("api_format") == "claude_cli":
        from backend.provider.claude_cli import KNOWN_MODELS, get_status
        if not get_status()["logged_in"]:
            return jsonify({"success": False, "error": "Not connected. Run `claude auth login` first."}), 401
        existing = {m["model_name"] for m in db.get_models_by_provider(provider_id)}
        models = [dict(m, already_added=m["id"] in existing) for m in KNOWN_MODELS]
        return jsonify({"success": True, "models": models, "total": len(models)})

    adapter, base_url = _provider_adapter(provider)
    if adapter is None or not base_url:
        return jsonify({"success": False, "error": "Provider has an invalid endpoint or adapter configuration"}), 400

    # Discovery is deliberately prompt-free. The adapter key, not hostname or
    # locality metadata, determines its protocol-specific probe path.
    models_url = f"{base_url}{adapter.discovery_path()}"
    api_format = provider.get("api_format", "openai")  # legacy auth bridge only

    headers = {"Content-Type": "application/json"}
    if api_format == "codex":
        from backend.provider.oauth_codex import get_valid_token
        token = get_valid_token(db, provider_id)
        if not token:
            return jsonify({"success": False, "error": "Not connected. Click Connect first."}), 401
        headers["Authorization"] = f"Bearer {token}"
    elif api_format == "anthropic":
        from backend.provider.claude_code import auth_headers, resolve_api_key
        token = resolve_api_key(db, provider_id)
        if not token:
            return jsonify({"success": False, "error": "Not connected. Set up credentials first."}), 401
        headers = auth_headers(token)
    else:
        api_key = provider.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    params = {}
    if api_format == "codex":
        params["client_version"] = "0.1.0"

    if api_format == "codex":
        from backend.provider.oauth_codex import extract_account_id
        headers["User-Agent"] = "codex_cli_rs/0.0.0"
        headers["originator"] = "codex_cli_rs"
        acct = extract_account_id(headers.get("Authorization", "").replace("Bearer ", ""))
        if acct:
            headers["ChatGPT-Account-ID"] = acct

    try:
        if api_format == "codex":
            import httpx
            resp = httpx.get(models_url, headers=headers, params=params, timeout=15)
        else:
            resp = requests.get(models_url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            return jsonify({
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "status_code": resp.status_code,
            })

        data = resp.json()
        models = adapter.parse_discovered_models(data if isinstance(data, dict) else {})

        # Mark which ones are already added
        existing = {m["model_name"] for m in db.get_models_by_provider(provider_id)}
        for m in models:
            m["already_added"] = m["id"] in existing

        return jsonify({"success": True, "models": models, "total": len(models)})

    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Connection timed out"}), 408
    except requests.exceptions.ConnectionError as e:
        return jsonify({"success": False, "error": f"Connection error: {str(e)[:200]}"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"Error: {str(e)[:200]}"}), 500


@providers_bp.route("/api/providers/<provider_id>/test", methods=["POST"])
def api_test_provider(provider_id):
    """Test connection to a provider's endpoint."""
    provider = db.get_provider(provider_id)
    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    if provider.get("api_format") == "claude_cli":
        from backend.provider.claude_cli import get_status
        status = get_status(force=True)
        if status["logged_in"]:
            return jsonify({"success": True, "message": f"Claude Code CLI {status['version']} ready"})
        return jsonify({"success": False, "error": status["error"]})

    adapter, base_url = _provider_adapter(provider)
    if adapter is None or not base_url:
        return jsonify({"success": False, "error": "Provider has an invalid endpoint or adapter configuration"}), 400

    # This is a synthetic discovery probe; it never submits a production prompt.
    url = f"{base_url}{adapter.discovery_path()}"
    api_format = provider.get("api_format", "openai")  # legacy auth bridge only

    headers = {}
    if api_format == "codex":
        from backend.provider.oauth_codex import get_valid_token
        token = get_valid_token(db, provider_id)
        if not token:
            return jsonify({"success": False, "error": "Not connected. Click Connect first."}), 401
        headers["Authorization"] = f"Bearer {token}"
    elif api_format == "anthropic":
        from backend.provider.claude_code import auth_headers, resolve_api_key
        token = resolve_api_key(db, provider_id)
        if not token:
            return jsonify({"success": False, "error": "Not connected. Set up credentials first."}), 401
        headers = auth_headers(token)
    else:
        api_key = provider.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    params = {}
    if api_format == "codex":
        params["client_version"] = "0.1.0"

    if api_format == "codex":
        from backend.provider.oauth_codex import extract_account_id
        headers["User-Agent"] = "codex_cli_rs/0.0.0"
        headers["originator"] = "codex_cli_rs"
        acct = extract_account_id(headers.get("Authorization", "").replace("Bearer ", ""))
        if acct:
            headers["ChatGPT-Account-ID"] = acct

    try:
        if api_format == "codex":
            import httpx
            resp = httpx.get(url, headers=headers, params=params, timeout=10)
        else:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
                count = len(adapter.parse_discovered_models(data if isinstance(data, dict) else {}))
            except Exception:
                count = "?"
            return jsonify({"success": True, "message": f"Connected ({count} models available)"})
        else:
            return jsonify({
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "status_code": resp.status_code,
            })
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Connection timed out"}), 408
    except requests.exceptions.ConnectionError as e:
        return jsonify({"success": False, "error": f"Connection error: {str(e)[:200]}"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"Error: {str(e)[:200]}"}), 500


@providers_bp.route("/api/providers/<provider_id>/add-model", methods=["POST"])
def api_add_model_from_provider(provider_id):
    """Quick-add a model from the provider's discovered models list."""
    provider = db.get_provider(provider_id)
    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    data = request.get_json()
    model_name = data.get("model_name") if data else None
    if not model_name:
        return jsonify({"success": False, "error": "model_name is required"}), 400

    display_name = data.get("name") or model_name.split("/")[-1]

    model_data = {
        "name": display_name,
        "type": provider.get("type", "remote"),
        "provider": provider_id,
        "base_url": "",
        "api_key": "",
        "model_name": model_name,
        "max_tokens": data.get("max_tokens", 32768),
        "timeout": data.get("timeout", 60),
        "thinking": data.get("thinking", 0),
        "thinking_budget": data.get("thinking_budget", 0),
        "reasoning_effort": data.get("reasoning_effort"),
        "enabled": 1,
        "api_format": provider.get("api_format", "openai"),
    }
    if provider.get("api_format") in ("claude_cli", "anthropic"):
        model_data["vision_supported"] = 1

    try:
        new_id = db.create_model(model_data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 409

    return jsonify({"success": True, "model_id": new_id})
