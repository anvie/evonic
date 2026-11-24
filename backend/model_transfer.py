"""Export and import of model configuration as a JSON file.

A file carries the models and the providers they point at, so a fresh Evonic
instance can be set up without creating each model by hand. It never carries
credentials: no api_key, no OAuth tokens, no provider connection options (their
content is free-form and could hold a secret), and no URL with userinfo or a
query string. Credentials are added again on the importing instance.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

FORMAT = "evonic-models/v1"
MAX_PROVIDERS = 100
MAX_MODELS = 500

_PROVIDER_KEYS = ("id", "name", "type", "adapter_key", "api_format")
_MODEL_KEYS = (
    "id", "name", "type", "provider", "model_name", "max_tokens", "timeout", "thinking",
    "thinking_budget", "reasoning_effort", "temperature", "enabled", "is_default",
    "model_max_concurrent", "vision_supported", "context_window", "capabilities",
)
_BOOL_KEYS = ("thinking", "enabled", "is_default", "vision_supported")


def _safe_url(url: Optional[str]) -> Optional[str]:
    """Keep an endpoint only if it cannot be hiding a secret."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return url


def build_export(providers: List[Dict[str, Any]], models: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the export document from provider and model rows (secrets are left out)."""
    used = {m.get("provider") for m in models}
    out_models = []
    for m in models:
        entry = {key: m.get(key) for key in _MODEL_KEYS}
        entry["base_url"] = _safe_url(m.get("base_url"))
        for key in _BOOL_KEYS:
            entry[key] = bool(entry[key])
        entry["is_default"] = False   # which model is the default is each instance's own choice
        out_models.append(entry)
    out_providers = []
    for p in providers:
        if p.get("id") not in used:
            continue
        entry = {key: p.get(key) for key in _PROVIDER_KEYS}
        entry["enabled"] = bool(p.get("enabled", 1))
        entry["base_url"] = _safe_url(p.get("base_url") or p.get("endpoint"))
        out_providers.append(entry)
    return {
        "format": FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "providers": out_providers,
        "models": out_models,
    }


def parse_payload(payload: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Check the shape of an uploaded document; raises ValueError with a readable reason."""
    if not isinstance(payload, dict):
        raise ValueError("the file is not a model configuration export")
    if payload.get("format") != FORMAT:
        raise ValueError(f"unsupported format {payload.get('format')!r}; expected {FORMAT!r}")
    providers, models = payload.get("providers", []), payload.get("models", [])
    if not isinstance(providers, list) or not isinstance(models, list):
        raise ValueError("providers and models must be lists")
    if len(providers) > MAX_PROVIDERS or len(models) > MAX_MODELS:
        raise ValueError(f"too many entries (limit {MAX_PROVIDERS} providers and {MAX_MODELS} models)")
    if not all(isinstance(item, dict) for item in providers + models):
        raise ValueError("every provider and model must be an object")
    return providers, models
