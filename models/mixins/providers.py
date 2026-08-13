import sqlite3
from typing import Dict, Any, List, Optional


_SENSITIVE_PROVIDER_KEYS = frozenset({"api_key"})


class ProvidersMixin:
    """Provider CRUD operations. Requires self._connect() from the host class."""

    def get_providers(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, type, base_url, api_key, api_format, enabled, "
                "auth_type, refresh_token, token_expires_at, "
                "created_at, updated_at FROM providers ORDER BY name"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, type, base_url, api_key, api_format, enabled, "
                "auth_type, refresh_token, token_expires_at, "
                "created_at, updated_at FROM providers WHERE id = ?",
                (provider_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_provider(self, data: Dict[str, Any]) -> str:
        pid = data.get("id") or data.get("name", "custom").lower().replace(" ", "_")
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO providers (id, name, type, base_url, api_key, api_format, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    data.get("name", pid),
                    data.get("type", "remote"),
                    data.get("base_url", ""),
                    data.get("api_key", ""),
                    data.get("api_format", "openai"),
                    data.get("enabled", 1),
                ),
            )
            conn.commit()
        return pid

    def update_provider(self, provider_id: str, data: Dict[str, Any]) -> bool:
        allowed = {"name", "type", "base_url", "api_key", "api_format", "enabled",
                   "auth_type", "refresh_token", "token_expires_at"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [provider_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE providers SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
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

    def get_models_by_provider(self, provider_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, type, provider, base_url, api_key, model_name, "
                "max_tokens, timeout, thinking, thinking_budget, temperature, "
                "enabled, is_default, created_at, updated_at, model_max_concurrent, "
                "api_format, vision_supported, document_pdf_supported, document_office_supported, document_spreadsheet_supported, legacy_id, shortcode, context_window "
                "FROM llm_models WHERE provider = ? ORDER BY name",
                (provider_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def resolve_model_config(self, model: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in base_url/api_key/api_format from the provider if the model's own are empty."""
        provider = self.get_provider(model.get("provider", ""))
        if not provider:
            return model
        result = dict(model)
        if not result.get("base_url"):
            result["base_url"] = provider.get("base_url", "")
        if not result.get("api_key"):
            result["api_key"] = provider.get("api_key", "")
        if not result.get("api_format") or result["api_format"] == "openai":
            pf = provider.get("api_format")
            if pf and pf != "openai":
                result["api_format"] = pf
        return result
