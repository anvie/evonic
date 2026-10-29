import json
import logging
from typing import Any, Dict, List

import requests
from flask import Blueprint, jsonify, request

from backend.tools.lib.model_routing_contracts import ContractValidationError, parse_capabilities
from models.db import db

_logger = logging.getLogger(__name__)

models_bp = Blueprint("models", __name__)

_SENSITIVE_MODEL_KEYS = frozenset({"api_key"})


def _sanitize_model(model: Dict[str, Any]) -> Dict[str, Any]:
    """Strip sensitive fields (api_key) from a model dict before API response."""
    for key in _SENSITIVE_MODEL_KEYS:
        model.pop(key, None)
    return model


def _sanitize_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for m in models:
        _sanitize_model(m)
    return models


@models_bp.route("/api/models", methods=["GET"])
def api_list_models():
    """List all models."""
    models = db.get_llm_models()
    return jsonify({"models": _sanitize_models(models)})


@models_bp.route("/api/models/<path:model_id>", methods=["GET"])
def api_get_model(model_id):
    """Get a single model."""
    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({"error": "Model not found"}), 404
    return jsonify(_sanitize_model(model))


@models_bp.route("/api/models", methods=["POST"])
def api_create_model():
    """Create a new model."""
    data = request.get_json()
    if (
        not data
        or not data.get("name")
        or not data.get("type")
        or not data.get("provider")
        or not data.get("model_name")
    ):
        return jsonify(
            {
                "success": False,
                "error": "name, type, provider, and model_name are required",
            }
        ), 400

    # Type is descriptive locality metadata only; retain legacy values without
    # allowing it to determine endpoint, protocol, or authentication behavior.
    if not isinstance(data["type"], str) or not data["type"].strip():
        return jsonify({"success": False, "error": "type must be descriptive text"}), 400

    if "base_url" in data or "api_key" in data:
        return jsonify({"success": False, "error": "new models must inherit endpoint and credentials from their provider"}), 400
    try:
        data["capabilities"] = parse_capabilities(data.get("capabilities") or {
            "version": "model-capabilities/v1", "operations": {"chat": "advertised"},
            "schema_versions": [], "confidence": "unsupported", "data_egress": ["public"],
        }).to_dict()
    except ContractValidationError as e:
        return jsonify({"success": False, "error": str(e), "code": e.code, "path": e.path}), 400

    # Validate provider exists in DB
    provider = db.get_provider(data["provider"])
    if not provider:
        return jsonify(
            {"success": False, "error": f"Unknown provider: {data['provider']}. Create it first via Settings > Providers."}
        ), 400

    try:
        new_id = db.create_model(
            {
                "id": data.get("id"),
                "name": data["name"],
                "type": data["type"],
                "provider": data["provider"],
                "base_url": data.get("base_url"),
                "api_key": data.get("api_key"),
                "model_name": data["model_name"],
                "max_tokens": data.get("max_tokens", 32768),
                "timeout": data.get("timeout", 60),
                "thinking": data.get("thinking", 0),
                "thinking_budget": data.get("thinking_budget", 0),
                "temperature": data.get("temperature"),
                "enabled": data.get("enabled", 1),
                "is_default": data.get("is_default", 0),
                "model_max_concurrent": data.get("model_max_concurrent", 3),
                "vision_supported": data.get("vision_supported", 0),
                "context_window": data.get("context_window", 0),
                "capabilities": data.get("capabilities", {}),
            }
        )
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 409

    # If this is set as default, unset other defaults
    if data.get("is_default"):
        with db._connect() as conn:
            conn.execute("UPDATE llm_models SET is_default = 0")
            conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = ?", (new_id,))
            conn.commit()

    return jsonify({"success": True, "model_id": new_id})


@models_bp.route("/api/models/<path:model_id>", methods=["PUT"])
def api_update_model(model_id):
    """Update a model."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No data provided"}), 400

    # Resolve to the canonical id (accepts legacy ids)
    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({"success": False, "error": "Model not found"}), 404
    model_id = model["id"]

    # Model connection values are migration compatibility data only. Existing
    # overrides remain readable by the resolver but administration cannot add or
    # alter them after the provider-owned connection migration.
    if "base_url" in data or "api_key" in data:
        return jsonify({"success": False, "error": "model endpoint and credentials are legacy compatibility fields; edit the provider instead"}), 400
    if "capabilities" in data:
        try:
            data["capabilities"] = parse_capabilities(data["capabilities"]).to_dict()
        except ContractValidationError as e:
            return jsonify({"success": False, "error": str(e), "code": e.code, "path": e.path}), 400

    # If setting as default, unset other defaults
    if data.get("is_default"):
        with db._connect() as conn:
            conn.execute("UPDATE llm_models SET is_default = 0")
            conn.execute(
                "UPDATE llm_models SET is_default = 1 WHERE id = ?", (model_id,)
            )
            conn.commit()

    success = db.update_model(model_id, data)
    if not success:
        return jsonify(
            {"success": False, "error": "Model not found or no changes made"}
        ), 404

    if "model_max_concurrent" in data:
        try:
            from backend.agent_runtime.runtime import AgentRuntime

            if AgentRuntime._concurrency_mgr:
                AgentRuntime._concurrency_mgr.refresh_model_limit(model_id)
        except Exception:
            pass

    return jsonify({"success": True})


@models_bp.route("/api/models/<path:model_id>", methods=["DELETE"])
def api_delete_model(model_id):
    """Delete a model."""
    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({"success": False, "error": "Model not found"}), 404
    references = db.get_model_references(model["id"])
    db.delete_model(model["id"])
    return jsonify({"success": True, "affected_references": references})


@models_bp.route("/api/models/<path:model_id>/clone", methods=["POST"])
def api_clone_model(model_id):
    """Clone an existing model with new ID, keeping all config except default flag."""
    source = db.get_model_by_id(model_id)
    if not source:
        return jsonify({"success": False, "error": "Model not found"}), 404

    clone_data = {
        "name": f"Copy of {source['name']}",
        "type": source.get("type"),
        "provider": source.get("provider"),
        "model_name": source.get("model_name"),
        "max_tokens": source.get("max_tokens", 32768),
        "timeout": source.get("timeout", 60),
        "thinking": source.get("thinking", 0),
        "thinking_budget": source.get("thinking_budget", 0),
        "temperature": source.get("temperature"),
        "enabled": source.get("enabled", 1),
        "is_default": 0,
        "model_max_concurrent": source.get("model_max_concurrent", 1),
        "api_format": source.get("api_format", "openai"),
        "vision_supported": source.get("vision_supported", 0),
        "context_window": source.get("context_window", 0),
        "capabilities": source.get("capabilities", {}),
    }
    new_id = db.create_model(clone_data)
    return jsonify({"success": True, "model_id": new_id})


@models_bp.route("/api/models/<path:model_id>/set-default", methods=["POST"])
def api_set_default_model(model_id):
    """Set a model as global default."""
    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({"error": "Model not found"}), 404

    with db._connect() as conn:
        conn.execute("UPDATE llm_models SET is_default = 0")
        conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = ?", (model["id"],))
        conn.commit()

    return jsonify({"success": True})


@models_bp.route("/api/models/<path:model_id>/ping", methods=["POST"])
def api_ping_model(model_id):
    """Send a one-word prompt through the model itself to prove it can be reached.

    Unlike /test (which only checks the provider endpoint) this goes through the
    same client agents use, so credentials, routing and the model name are all
    exercised. No fallback is allowed: a dead model must not look alive because
    another one answered.

    Reachability is decided by whether the server answered at all: an HTTP 200
    completion whose budget was consumed by reasoning still proves the endpoint,
    the credentials and the model name are good, so it is reported as reachable
    with a note instead of as "Unreachable".
    """
    import time
    from backend.llm_client import LLMClient

    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({"success": False, "error": "Model not found"}), 404
    model = db.resolve_model_config(model)
    config = {
        "provider": model.get("provider"),
        "base_url": model.get("base_url"),
        "api_key": model.get("api_key"),
        "model_name": model.get("model_name"),
        # A ping that hangs for minutes is itself an answer.
        "timeout": min(int(model.get("timeout") or 60), 30),
        "thinking": False,
        "thinking_budget": 0,
        "max_tokens": 32,
        "context_window": model.get("context_window"),
        "temperature": model.get("temperature"),
        "vision_supported": bool(model.get("vision_supported")),
        "api_format": model.get("api_format", "openai"),
    }
    # Thinking models (Groot/Qwen-style chat templates) open every reply with a
    # reasoning block, so a tiny ping budget is gone before any content exists.
    # Self-hosted OpenAI-compatible servers can skip that block when asked via
    # the chat template; cloud APIs reject the field, hence provider_type gate.
    if model.get("provider_type") == "local" and model.get("thinking"):
        config["chat_template_kwargs"] = {"enable_thinking": False}
    started = time.monotonic()
    try:
        result = LLMClient(model_config=config, _allow_fallback=False).chat_completion(
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
            tools=None, temperature=0.0, enable_thinking=False, max_tokens=32,
        )
    except Exception as exc:  # network errors, bad credentials, missing CLI ...
        _logger.info("Model ping failed for %s: %s", model_id, exc)
        return jsonify({"success": False, "error": str(exc)[:300],
                        "latency_ms": int((time.monotonic() - started) * 1000)})
    latency = int((time.monotonic() - started) * 1000)
    if not result.get("success"):
        # "answered" means the server returned HTTP 200 with a completion: it is
        # reachable, it just spent the whole budget on reasoning. Reporting that
        # as "Unreachable" is a false negative, so surface it as a reachable ping
        # with an explanatory note instead.
        if result.get("answered"):
            return jsonify({
                "success": True,
                "latency_ms": latency,
                "reply": "",
                "reasoning_only": True,
                "note": "Model answered, but reasoning used the whole token budget",
            })
        detail = result.get("error_detail") or result.get("error") or result.get("error_type") or "No response"
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail)
        return jsonify({"success": False, "error": str(detail)[:300], "latency_ms": latency})
    reply = ""
    try:
        message = (result.get("response") or {}).get("choices", [{}])[0].get("message", {})
        reply = (message.get("content") or "").strip()
    except (AttributeError, IndexError, TypeError):
        pass
    return jsonify({"success": True, "latency_ms": latency, "reply": reply[:80]})


@models_bp.route("/api/models/<path:model_id>/test", methods=["POST"])
def api_test_model(model_id):
    """Test connection to model endpoint."""
    model = db.get_model_by_id(model_id)
    if not model:
        return jsonify({"error": "Model not found"}), 404

    try:
        model = db.resolve_model_config(model)
        api_format = model.get("api_format", "openai")
        provider_id = model.get("provider", "")

        if api_format == "claude_cli":
            from backend.provider.claude_cli import get_status
            status = get_status(force=True)
            if status["logged_in"]:
                return jsonify({"success": True, "message": f"Claude Code CLI {status['version']} ready"})
            return jsonify({"success": False, "error": status["error"]})

        if api_format == "codex":
            from backend.provider.oauth_codex import get_valid_token
            token = get_valid_token(db, provider_id)
            if not token:
                return jsonify({"success": False, "error": "Not connected. Complete OAuth flow first."})
            from backend.provider.codex_client import CodexClient
            base_url = model.get("base_url") or "https://chatgpt.com/backend-api/codex"
            client = CodexClient(token, base_url)
            result = client.test_connection()
            return jsonify(result)

        # Try to reach the base URL
        base_url = model.get("base_url")
        if not base_url:
            return jsonify({"success": False, "error": "No base_url configured"}), 400

        if api_format == "ollama":
            models_url = f"{base_url}/tags"
        else:
            models_url = f"{base_url}/models"
        headers = {"Content-Type": "application/json"}
        if model.get("api_key"):
            headers["Authorization"] = f"Bearer {model['api_key']}"

        response = requests.get(models_url, headers=headers, timeout=10)

        if response.status_code == 200:
            try:
                data = response.json()
            except json.JSONDecodeError:
                raw_snippet = response.text[:500]
                _logger.error(
                    "Failed to parse JSON from %s (HTTP 200). Raw response: %s",
                    models_url,
                    raw_snippet,
                )
                return jsonify(
                    {
                        "success": False,
                        "error": f"Received invalid (non-JSON) response from {base_url}. "
                        f"The provider returned HTTP 200 but the body is not valid JSON. "
                        f"Raw response snippet: {raw_snippet}",
                        "status_code": 200,
                    }
                ), 502
            if api_format == "ollama":
                models_list = data.get("models") or []
            else:
                models_list = data.get("data") or data.get("models") or []
            return jsonify(
                {
                    "success": True,
                    "message": f"Connected to {base_url}",
                    "available_models": len(models_list),
                    "response_headers": dict(response.headers),
                }
            )
        else:
            return jsonify(
                {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                    "status_code": response.status_code,
                }
            )

    except requests.exceptions.Timeout:
        return jsonify(
            {
                "success": False,
                "error": f"Connection timed out to {model.get('base_url')}",
            }
        ), 408
    except requests.exceptions.ConnectionError as e:
        return jsonify(
            {"success": False, "error": f"Connection error: {str(e)[:200]}"}
        ), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"Error: {str(e)[:200]}"}), 500


@models_bp.route("/api/agents/<agent_id>/model", methods=["GET"])
def api_get_agent_model(agent_id):
    """Get agent's default model and fallback model."""
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    model = db.get_agent_model(agent_id)
    if model:
        _sanitize_model(model)
    fallback_model = db.get_agent_fallback_model(agent_id)
    if fallback_model:
        _sanitize_model(fallback_model)
    return jsonify(
        {
            "agent_id": agent_id,
            "model_id": agent.get("model_id"),
            "model": model,
            "fallback_model_id": agent.get("fallback_model_id"),
            "fallback_model": fallback_model,
        }
    )


@models_bp.route("/api/agents/<agent_id>/model", methods=["POST"])
def api_set_agent_model(agent_id):
    """Set agent's default model and/or fallback model."""
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    data = request.get_json()
    model_id = data.get("model_id") if data else None
    fallback_model_id = data.get("fallback_model_id") if data else None

    # Set primary model (None means "clear" → use global default)
    success = db.set_agent_model(agent_id, model_id)
    if not success and model_id:
        return jsonify({"success": False, "error": "Failed to set primary model"}), 400

    # Set fallback model (None means "clear" → no fallback)
    # Only set if explicitly provided (even if None)
    if "fallback_model_id" in (data or {}):
        fb_success = db.set_agent_fallback_model(agent_id, fallback_model_id)
        if not fb_success and fallback_model_id:
            return jsonify({"success": False, "error": "Failed to set fallback model"}), 400

    return jsonify({"success": True})


@models_bp.route("/api/agents/<agent_id>/model/reset", methods=["POST"])
def api_reset_agent_active_model(agent_id):
    """Reset the agent's active fallback model, returning to the primary model."""
    import json as _json

    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    raw = db.get_agent_state(agent_id=agent_id)
    if not raw:
        return jsonify({"result": "No agent state found — nothing to reset."})

    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return jsonify({"error": "Failed to parse agent state"}), 500

    if "active_fallback_model_id" not in data:
        return jsonify({"result": "No active fallback model to reset."})

    fb_id = data.pop("active_fallback_model_id", None)
    db.upsert_agent_state(_json.dumps(data), agent_id=agent_id)

    return jsonify({
        "success": True,
        "result": f"Fallback model ({fb_id}) has been cleared. The agent will use its primary model on the next turn."
    })
