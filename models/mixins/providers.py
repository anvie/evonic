import json
import sqlite3
from typing import Dict, Any, List, Optional

from backend.tools.lib.model_routing_contracts import (
    AdapterKey, CredentialResolution, CredentialResolutionState,
    ProviderRoute, ResolvedModelRoute, parse_capabilities, validate_endpoint,
)
from models.mixins.models import _MODEL_COLUMNS, _decode_capabilities


_ADAPTER_TO_LEGACY_FORMAT = {
    AdapterKey.OPENAI_CHAT_COMPLETIONS_V1.value: "openai",
    AdapterKey.OPENAI_RESPONSES_V1.value: "openai",
    AdapterKey.ANTHROPIC_MESSAGES_V1.value: "anthropic",
    AdapterKey.OLLAMA_CHAT_V1.value: "ollama",
    AdapterKey.CODEX_RESPONSES_V1.value: "codex",
    AdapterKey.SYSTEMONE_QUESTIONS_V1.value: "systemone",
}
_LEGACY_FORMAT_TO_ADAPTER = {value: key for key, value in _ADAPTER_TO_LEGACY_FORMAT.items()}

# The Claude Code CLI has no HTTP adapter: it is selected by api_format alone and
# keeps the default adapter key so endpoint validation and routing stay unchanged.
_CLI_FORMAT = "claude_cli"


_SENSITIVE_PROVIDER_KEYS = frozenset({"api_key"})


class ProvidersMixin:
    """Provider CRUD operations. Requires self._connect() from the host class."""

    def get_providers(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, type, base_url, api_key, api_format, endpoint, adapter_key, "
                "connection_options, enabled, auth_type, refresh_token, token_expires_at, "
                "credential_source, created_at, updated_at FROM providers ORDER BY name"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, type, base_url, api_key, api_format, endpoint, adapter_key, "
                "connection_options, enabled, auth_type, refresh_token, token_expires_at, "
                "credential_source, created_at, updated_at FROM providers WHERE id = ?",
                (provider_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def _normalize_provider_data(data: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(data)
        adapter_key = result.get("adapter_key")
        if not adapter_key:
            adapter_key = _LEGACY_FORMAT_TO_ADAPTER.get(
                result.get("api_format", "openai"), AdapterKey.OPENAI_CHAT_COMPLETIONS_V1.value
            )
        try:
            adapter_key = AdapterKey(adapter_key).value
        except ValueError as exc:
            raise ValueError("adapter_key must name a supported versioned adapter") from exc
        endpoint = result.get("endpoint", result.get("base_url", ""))
        if endpoint:
            endpoint = validate_endpoint(endpoint)
        options = result.get("connection_options", {})
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except ValueError as exc:
                raise ValueError("connection_options must be a JSON object") from exc
        if not isinstance(options, dict):
            raise ValueError("connection_options must be an object")
        result.update(endpoint=endpoint, base_url=endpoint, adapter_key=adapter_key,
                      api_format=(_CLI_FORMAT if result.get("api_format") == _CLI_FORMAT
                                  else _ADAPTER_TO_LEGACY_FORMAT[adapter_key]),
                      connection_options=json.dumps(options, sort_keys=True))
        return result

    def create_provider(self, data: Dict[str, Any]) -> str:
        data = self._normalize_provider_data(data)
        pid = data.get("id") or data.get("name", "custom").lower().replace(" ", "_")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO providers (id, name, type, base_url, api_key, api_format, endpoint, adapter_key, connection_options, enabled, auth_type, credential_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, data.get("name", pid), data.get("type", "remote"), data["base_url"], data.get("api_key", ""),
                 data["api_format"], data["endpoint"], data["adapter_key"], data["connection_options"], data.get("enabled", 1),
                 data.get("auth_type", "api_key"), data.get("credential_source", "api_key")),
            )
            conn.commit()
        return pid

    def update_provider(self, provider_id: str, data: Dict[str, Any]) -> bool:
        current = self.get_provider(provider_id)
        if not current:
            return False
        # An explicit adapter_key or api_format in the request must drive the
        # adapter, so it takes precedence over the inherited provider value.
        update_keys = set(data)
        # An explicit adapter_key (or legacy api_format) request must drive the
        # adapter, taking precedence over the inherited provider adapter.
        requested_adapter = data.get("adapter_key") if "adapter_key" in update_keys else (
            _LEGACY_FORMAT_TO_ADAPTER.get(data.get("api_format")) if "api_format" in update_keys else None)
        requested_endpoint = data.get("endpoint") if "endpoint" in update_keys else (
            data.get("base_url") if "base_url" in update_keys else None)
        merged = self._normalize_provider_data({
            **current,
            **data,
            "adapter_key": requested_adapter or current.get("adapter_key"),
            "api_format": (_CLI_FORMAT if data.get("api_format") == _CLI_FORMAT
                           else _ADAPTER_TO_LEGACY_FORMAT.get(requested_adapter, data.get("api_format") or current.get("api_format"))),
            "endpoint": requested_endpoint if requested_endpoint is not None else current.get("endpoint") or current.get("base_url", ""),
        })
        allowed = {"name", "type", "base_url", "api_key", "api_format", "endpoint", "adapter_key", "connection_options", "enabled",
                   "auth_type", "refresh_token", "token_expires_at", "credential_source"}
        updates = {k: merged[k] for k in update_keys & allowed if k in merged}
        if {"endpoint", "base_url", "adapter_key", "api_format", "connection_options"} & update_keys:
            updates.update({key: merged[key] for key in ("endpoint", "base_url", "adapter_key", "api_format", "connection_options")})
        if not updates:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE providers SET {', '.join(f'{k} = ?' for k in updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [*updates.values(), provider_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_provider(self, provider_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM llm_models WHERE provider = ?", (provider_id,)
            )
            if cursor.fetchone()[0] > 0:
                return False
            cursor.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_provider_delete_impact(self, provider_id: str) -> Dict[str, Any]:
        """What deleting a provider together with its models would touch."""
        models = self.get_models_by_provider(provider_id)
        agents: Dict[str, List[str]] = {}
        profiles: set = set()
        for m in models:
            refs = self.get_model_references(m["id"])
            for ref in refs["agents"]:
                column, _, agent_id = ref.partition(":")
                agents.setdefault(agent_id, []).append(f"{column}={m['name']}")
            profiles.update(refs["profiles"])
        names = {}
        for agent_id in agents:
            agent = self.get_agent(agent_id) or {}
            names[agent_id] = agent.get("name") or agent_id
        return {
            "models": [{"id": m["id"], "name": m.get("name"), "is_default": bool(m.get("is_default"))} for m in models],
            "agents": [{"id": a, "name": names[a], "uses": uses} for a, uses in sorted(agents.items())],
            "profiles": sorted(profiles),
            "is_default": any(m.get("is_default") for m in models),
        }

    def delete_provider_cascade(self, provider_id: str) -> int:
        """Delete a provider and all its models; agents pointing at those models fall back to defaults.

        Returns the number of models removed. Refuses (ValueError) while one of them is the global default.
        """
        models = self.get_models_by_provider(provider_id)
        if any(m.get("is_default") for m in models):
            raise ValueError("default_model")
        ids = [m["id"] for m in models]
        with self._connect() as conn:
            cursor = conn.cursor()
            for model_id in ids:
                for column in ("model_id", "fallback_model_id", "vision_model_id", "default_model_id"):
                    try:
                        cursor.execute(
                            f"UPDATE agents SET {column} = NULL WHERE {column} = ?", (model_id,))
                    except sqlite3.OperationalError:
                        continue
                cursor.execute("DELETE FROM llm_models WHERE id = ?", (model_id,))
            cursor.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            conn.commit()
        return len(ids)

    def get_models_by_provider(self, provider_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE provider = ? ORDER BY name",
                (provider_id,),
            )
            return [_decode_capabilities(dict(row)) for row in cursor.fetchall()]

    def resolve_model_config(self, model: Dict[str, Any]) -> Dict[str, Any]:
        """Dual-read route configuration, preferring provider endpoint/adapter fields."""
        provider = self.get_provider(model.get("provider", ""))
        if not provider:
            return model
        result = dict(model)
        legacy_override = bool(result.get("base_url") or result.get("api_key"))
        result["base_url"] = result.get("base_url") or provider.get("endpoint") or provider.get("base_url", "")
        result["api_key"] = result.get("api_key") or provider.get("api_key", "")
        # Dual-read precedence: an explicit legacy model-level format keeps
        # routing until the compatibility window ends; otherwise the provider's
        # versioned adapter is authoritative.
        model_format = result.get("api_format")
        if model_format == "claude_cli":
            # Claude CLI is a direct local client, not a versioned HTTP adapter.
            # Keep the format while retaining the provider adapter for metadata;
            # LLMClient dispatches this format before any adapter lookup.
            adapter_key = provider.get("adapter_key")
        elif model_format and model_format != "openai":
            adapter_key = _LEGACY_FORMAT_TO_ADAPTER.get(model_format, provider.get("adapter_key"))
        else:
            adapter_key = provider.get("adapter_key") or _LEGACY_FORMAT_TO_ADAPTER.get(
                provider.get("api_format"), AdapterKey.OPENAI_CHAT_COMPLETIONS_V1.value)
        result["adapter_key"] = adapter_key
        if model_format == "claude_cli":
            result["api_format"] = "claude_cli"
        else:
            result["api_format"] = _ADAPTER_TO_LEGACY_FORMAT.get(
                adapter_key, model_format or provider.get("api_format", "openai"))
        result["legacy_provenance"] = "legacy_model_override" if legacy_override else None
        return result

    def resolve_model_route(self, model_id: str) -> Optional[ResolvedModelRoute]:
        """Resolve the provider-owned route without exposing it to public APIs."""
        model = self.get_model_by_id(model_id)
        if not model:
            return None
        provider = self.get_provider(model.get("provider", ""))
        if not provider:
            return None
        config = self.resolve_model_config(model)
        endpoint = config.get("base_url")
        if not endpoint:
            return None
        capabilities = parse_capabilities(model.get("capabilities") or {})
        credential = config.get("api_key")
        return ResolvedModelRoute(
            model_id=model["id"], provider=ProviderRoute(provider["id"], endpoint, AdapterKey(config["adapter_key"]), bool(provider.get("enabled", 1))),
            native_model_name=model["model_name"], capabilities=capabilities,
            credentials={"api_key": credential} if credential else {}, enabled=bool(model.get("enabled", 1)),
            credential_resolution=CredentialResolution(CredentialResolutionState.RESOLVED if credential else CredentialResolutionState.NOT_REQUIRED,
                                                       provider.get("credential_source") or None),
            legacy_provenance=config.get("legacy_provenance"),
        )
