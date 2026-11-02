import json
import sqlite3
from typing import Dict, Any, List, Optional


_MODEL_COLUMNS = (
    "id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, "
    "thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, "
    "model_max_concurrent, api_format, vision_supported, legacy_id, shortcode, context_window, capabilities, "
    "legacy_override_provenance"
)


def _decode_capabilities(model: Dict[str, Any]) -> Dict[str, Any]:
    """Decode persisted capability JSON defensively for legacy and malformed rows."""
    raw = model.get("capabilities")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    model["capabilities"] = raw if isinstance(raw, dict) else {}
    # SQLite stores BOOLEAN columns as 0/1 integers. Normalize the vision flag so
    # every caller gets a real boolean instead of a dialect-specific integer.
    model["vision_supported"] = bool(model.get("vision_supported", 0))
    return model


# A model is either a chat LLM or a structured decision model, never both.
# Pre-split rows stored descriptive text (e.g. "remote"/"local") here; read as LLM.
MODEL_TYPES = ("llm", "decision")
LEGACY_MODEL_TYPES = ("remote", "local")
_DECISION_OPERATIONS = ("decision", "strict_output")


def normalize_model_type(value: Any) -> str:
    return "decision" if value == "decision" else "llm"


def capabilities_for_type(model_type: str, vision_supported: Any, current: Any = None) -> Dict[str, Any]:
    """Derive model-capabilities/v1 from the model type.

    Existing evidence (e.g. a seeded ``verified`` decision model) is kept where
    it agrees with the type; operations belonging to the other type are dropped.
    """
    current = current if isinstance(current, dict) else {}
    operations = dict(current.get("operations") or {})
    if model_type == "decision":
        operations = {key: operations.get(key) or "advertised" for key in _DECISION_OPERATIONS}
        operations = {key: "advertised" if value == "unsupported" else value for key, value in operations.items()}
        schema_versions = current.get("schema_versions") or ["decision-schema/v1"]
    else:
        operations = {key: value for key, value in operations.items() if key not in _DECISION_OPERATIONS}
        operations.setdefault("chat", "advertised")
        if vision_supported:
            operations.setdefault("vision", "advertised")
        else:
            operations.pop("vision", None)
        schema_versions = []
    return {
        "version": "model-capabilities/v1",
        "operations": operations,
        "schema_versions": schema_versions,
        "confidence": current.get("confidence") or "unsupported",
        "data_egress": current.get("data_egress") or ["public"],
    }


def _normalize_model_data(model_data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply type-derived invariants before a full-row insert."""
    data = dict(model_data)
    data["type"] = normalize_model_type(data.get("type"))
    if data["type"] == "decision":
        # Chat-only settings have no meaning for a decision model.
        data.update(is_default=0, thinking=0, thinking_budget=0, vision_supported=0)
    data["capabilities"] = capabilities_for_type(data["type"], data.get("vision_supported"), data.get("capabilities"))
    return data


class ModelsMixin:
    """LLM model CRUD and model selection. Requires self._connect() from the host class."""

    def get_llm_models(self) -> List[Dict[str, Any]]:
        """Return list of all model configs."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models ORDER BY name LIMIT 1000")
            return [_decode_capabilities(dict(row)) for row in cursor.fetchall()]

    def get_enabled_llm_models(self) -> List[Dict[str, Any]]:
        """Return enabled chat models (enabled=1); decision models are excluded."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE enabled = 1 AND COALESCE(type, '') != 'decision' ORDER BY name LIMIT 1000")
            return [_decode_capabilities(dict(row)) for row in cursor.fetchall()]

    def save_llm_models(self, models_list: List[Dict[str, Any]]) -> None:
        """Persist models to llm_models table."""
        with self._connect() as conn:
            cursor = conn.cursor()
            # Clear existing models
            cursor.execute("DELETE FROM llm_models")
            for m in map(_normalize_model_data, models_list):
                cursor.execute("""
                    INSERT INTO llm_models (id, name, type, provider, base_url, api_key,
                        model_name, max_tokens, timeout, thinking, thinking_budget,
                        temperature, enabled, is_default, model_max_concurrent, api_format,
                        vision_supported, legacy_id, shortcode, context_window, capabilities)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    m.get('id'),
                    m.get('name'),
                    m.get('type'),
                    m.get('provider'),
                    m.get('base_url'),
                    m.get('api_key'),
                    m.get('model_name'),
                    m.get('max_tokens', 32768),
                    m.get('timeout', 60),
                    m.get('thinking', 0),
                    m.get('thinking_budget', 0),
                    m.get('temperature'),
                    m.get('enabled', 1),
                    m.get('is_default', 0),
                    m.get('model_max_concurrent', 3),
                    m.get('api_format', 'openai'),
                    m.get('vision_supported', 0),
                    m.get('legacy_id'),
                    m.get('shortcode'),
                    m.get('context_window', 0),
                    json.dumps(m.get('capabilities') or {}, sort_keys=True),
                ))
            conn.commit()

    def get_default_model(self) -> Optional[Dict[str, Any]]:
        """Return global default model (is_default=1)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE is_default = 1 LIMIT 1")
            row = cursor.fetchone()
            return _decode_capabilities(dict(row)) if row else None

    def get_model_by_id(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Lookup model by ID, falling back to legacy_id for pre-rename references."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE id = ?", (model_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE legacy_id = ? LIMIT 1", (model_id,))
                row = cursor.fetchone()
            return _decode_capabilities(dict(row)) if row else None

    def _model_id_exists(self, model_id: str) -> bool:
        """True if an id is taken as a current id or as a legacy alias."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM llm_models WHERE id = ? OR legacy_id = ?", (model_id, model_id))
            return cursor.fetchone() is not None

    def generate_model_id(self, provider: Optional[str], model_name: Optional[str]) -> str:
        """Build a canonical 'provider/model_name' id, suffixing -2, -3... on collision."""
        base = f"{(provider or 'custom').strip('/')}/{(model_name or '').strip('/')}".rstrip('/')
        if not base:
            import uuid
            base = str(uuid.uuid4())
        candidate, n = base, 2
        while self._model_id_exists(candidate):
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def get_model_by_model_name(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Lookup model by its model_name field (the API model identifier)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE model_name = ? LIMIT 1", (model_name,))
            row = cursor.fetchone()
            return _decode_capabilities(dict(row)) if row else None

    def get_agent_model(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return agent's primary model (model_id) or global default or None."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # First try agent-specific model
            cursor.execute("SELECT model_id FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            if row and row['model_id']:
                cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE id = ?", (row['model_id'],))
                model_row = cursor.fetchone()
                if model_row:
                    return _decode_capabilities(dict(model_row))
            # Fallback to global default
            cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE is_default = 1 LIMIT 1")
            row = cursor.fetchone()
            return _decode_capabilities(dict(row)) if row else None

    def get_agent_default_model(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Deprecated alias — use get_agent_model()."""
        return self.get_agent_model(agent_id)

    def set_agent_model(self, agent_id: str, model_id: Optional[str]) -> bool:
        """Set agent's primary model. model_id can be None to clear."""
        if model_id:
            model = self.get_model_by_id(model_id)
            if not model or model.get('type') == 'decision':
                return False
            model_id = model['id']  # canonicalize legacy ids
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agents SET model_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (model_id, agent_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_agent_default_model(self, agent_id: str, model_id: Optional[str]) -> bool:
        """Deprecated alias — use set_agent_model()."""
        return self.set_agent_model(agent_id, model_id)

    def get_agent_fallback_model(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return agent's fallback model or None.

        Unlike get_agent_default_model, there is no global fallback —
        if the agent has no fallback configured, returns None.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT fallback_model_id FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            if row and row["fallback_model_id"]:
                cursor.execute(f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE id = ?", (row["fallback_model_id"],))
                model_row = cursor.fetchone()
                if model_row:
                    return _decode_capabilities(dict(model_row))
            return None

    def set_agent_fallback_model(self, agent_id: str, model_id: Optional[str]) -> bool:
        """Set agent's fallback model. model_id can be None to clear."""
        if model_id:
            model = self.get_model_by_id(model_id)
            if not model or model.get('type') == 'decision':
                return False
            model_id = model['id']  # canonicalize legacy ids
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agents SET fallback_model_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (model_id, agent_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def create_model(self, model_data: Dict[str, Any]) -> str:
        """Create a new model. Returns model ID.

        Without an explicit id, generates 'provider/model_name'.
        Raises ValueError if an explicit id is already taken.
        """
        model_data = _normalize_model_data(model_data)
        model_id = model_data.get('id')
        if model_id:
            if self._model_id_exists(model_id):
                raise ValueError(f"Model id '{model_id}' already exists")
        else:
            model_id = self.generate_model_id(
                model_data.get('provider'), model_data.get('model_name')
            )
        with self._connect() as conn:
            cursor = conn.cursor()
            # Auto-assign shortcode
            cursor.execute("SELECT COALESCE(MAX(shortcode), 0) FROM llm_models")
            next_code = cursor.fetchone()[0] + 1
            cursor.execute("""
                INSERT INTO llm_models (id, name, type, provider, base_url, api_key,
                    model_name, max_tokens, timeout, thinking, thinking_budget,
                    temperature, enabled, is_default, model_max_concurrent, api_format,
                    vision_supported, shortcode, context_window, capabilities)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                model_id,
                model_data.get('name'),
                model_data.get('type'),
                model_data.get('provider'),
                model_data.get('base_url'),
                model_data.get('api_key'),
                model_data.get('model_name'),
                model_data.get('max_tokens', 32768),
                model_data.get('timeout', 60),
                model_data.get('thinking', 0),
                model_data.get('thinking_budget', 0),
                model_data.get('temperature'),
                model_data.get('enabled', 1),
                model_data.get('is_default', 0),
                model_data.get('model_max_concurrent', 3),
                model_data.get('api_format', 'openai'),
                model_data.get('vision_supported', 0),
                next_code,
                model_data.get('context_window', 0),
                json.dumps(model_data.get('capabilities') or {}, sort_keys=True),
            ))
            conn.commit()
        return model_id

    def update_model(self, model_id: str, model_data: Dict[str, Any]) -> bool:
        """Update an existing model."""
        allowed = {'name', 'type', 'provider', 'base_url', 'api_key', 'model_name',
                   'max_tokens', 'timeout', 'thinking', 'thinking_budget', 'temperature', 'enabled', 'is_default',
                   'model_max_concurrent', 'api_format', 'vision_supported', 'context_window'}
        allowed = allowed | {'capabilities'}
        updates = {k: v for k, v in model_data.items() if k in allowed}
        if not updates:
            return False
        if updates.keys() & {'type', 'capabilities', 'vision_supported', 'is_default'}:
            current = self.get_model_by_id(model_id)
            if current:
                merged = _normalize_model_data({**current, **updates})
                updates['type'] = merged['type']
                updates['capabilities'] = merged['capabilities']
                if merged['type'] == 'decision':
                    updates.update({k: merged[k] for k in ('is_default', 'thinking', 'thinking_budget', 'vision_supported')})
        if 'capabilities' in updates:
            updates['capabilities'] = json.dumps(updates['capabilities'] or {}, sort_keys=True)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [model_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE llm_models SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_model_references(self, model_id: str) -> Dict[str, List[str]]:
        """Report soft references so callers can warn before registry changes."""
        references: Dict[str, List[str]] = {"agents": [], "profiles": [], "settings": []}
        with self._connect() as conn:
            cursor = conn.cursor()
            for column in ("model_id", "fallback_model_id", "vision_model_id", "default_model_id"):
                try:
                    cursor.execute(f"SELECT id FROM agents WHERE {column} = ?", (model_id,))
                    references["agents"].extend(f"{column}:{row[0]}" for row in cursor.fetchall())
                except sqlite3.OperationalError:
                    continue
            cursor.execute("SELECT profile_key FROM decision_profiles WHERE primary_model_id = ? OR fallback_model_id = ?", (model_id, model_id))
            references["profiles"] = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT key FROM app_settings WHERE value = ? AND key LIKE '%model%id%'", (model_id,))
            references["settings"] = [row[0] for row in cursor.fetchall()]
        return references

    def delete_model(self, model_id: str) -> bool:
        """Delete a model."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM llm_models WHERE id = ?", (model_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_model_by_shortcode(self, shortcode: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT {_MODEL_COLUMNS} FROM llm_models WHERE shortcode = ?",
                (shortcode,),
            )
            row = cursor.fetchone()
            return _decode_capabilities(dict(row)) if row else None
