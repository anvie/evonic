"""Persistence and validation for versioned Decision Profiles."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, List, Mapping, Optional

from backend.tools.lib.decision_models import (
    BUILTIN_PROFILE_KEYS,
    PROFILE_KEY_PATTERN,
    SCHEMA_VERSION,
    ContractValidationError,
    parse_schema,
)

VALID_MODES = frozenset({"off", "shadow", "enforce"})
_MAX_DISPLAY_NAME_LENGTH = 160
_MAX_DESCRIPTION_LENGTH = 4000
_MAX_TIMEOUT_MS = 300_000
_MAX_RETRY_LIMIT = 10
_MAX_SAMPLE_INPUT_BYTES = 8 * 1024
MAX_FALLBACK_MODELS = 5
# Global default chain: tried in order by every profile that names no model of its own.
MAX_DEFAULT_MODELS = 5
DEFAULT_MODELS_SETTING = "decision_default_model_ids"


class DecisionProfileValidationError(ValueError):
    """Actionable validation failure safe to return from the management API."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        super().__init__(message)


def _fail(code: str, path: str, message: str) -> None:
    raise DecisionProfileValidationError(code, path, message)


def _public(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Decode SQLite values into the stable, credential-free API representation."""
    profile = dict(row)
    profile["enabled"] = bool(profile["enabled"])
    profile["minimum_confidence"] = float(profile["minimum_confidence"])
    profile["schema"] = json.loads(profile.pop("schema_json"))
    profile["fallback_model_ids"] = _stored_fallback_ids(profile.pop("fallback_model_ids_json", None),
                                                         profile.get("fallback_model_id"))
    raw_sample = profile.pop("sample_input_json", None)
    try:
        sample = json.loads(raw_sample) if raw_sample else None
    except ValueError:
        sample = None
    profile["sample_input"] = sample if isinstance(sample, dict) else None
    return profile


def _stored_fallback_ids(raw: Any, legacy: Any) -> List[str]:
    """The stored fallback chain; a row written before the chain existed reads its single fallback."""
    try:
        ids = json.loads(raw) if raw else None
    except ValueError:
        ids = None
    if not isinstance(ids, list):
        ids = [legacy] if isinstance(legacy, str) and legacy.strip() else []
    return [i for i in ids if isinstance(i, str) and i.strip()]


def _requested_fallback_ids(data: Mapping[str, Any], existing: Optional[Mapping[str, Any]]) -> tuple:
    """The fallback chain a write asks for, and whether it came as the full list.

    ``fallback_model_ids`` replaces the chain. A payload with only
    ``fallback_model_id`` (older callers, the single-fallback editor) replaces
    the first entry and keeps the rest; clearing it clears the chain.
    """
    current = list((existing or {}).get("fallback_model_ids") or [])
    if not current and (existing or {}).get("fallback_model_id"):
        current = [existing["fallback_model_id"]]
    if "fallback_model_ids" in data:
        ids = data.get("fallback_model_ids")
        if ids is None:
            return [], True
        if not isinstance(ids, (list, tuple)):
            _fail("invalid_list", "fallback_model_ids", "must be a list of model ids")
        return list(ids), True
    if "fallback_model_id" in data:
        first = data.get("fallback_model_id")
        if first is None or (isinstance(first, str) and not first.strip()):
            return [], False
        if current and current[0] == first:
            return current, False
        return [first] + [i for i in current[1:] if i != first], False
    return current, False


def _sample_input(value: Any) -> Optional[Dict[str, Any]]:
    """An optional example ``input_context`` (a small JSON object) for testing the profile."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _fail("invalid_sample_input", "sample_input", "must be a JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        _fail("invalid_sample_input", "sample_input", "must be valid JSON")
    if len(encoded.encode("utf-8")) > _MAX_SAMPLE_INPUT_BYTES:
        _fail("too_long", "sample_input", f"must be at most {_MAX_SAMPLE_INPUT_BYTES} bytes")
    return dict(value)


def _text(value: Any, path: str, *, required: bool, maximum: int) -> str:
    if not isinstance(value, str):
        _fail("invalid_string", path, "must be a string")
    value = value.strip()
    if required and not value:
        _fail("required", path, "is required")
    if len(value) > maximum:
        _fail("too_long", path, f"must be at most {maximum} characters")
    return value


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail("out_of_range", path, f"must be an integer from {minimum} through {maximum}")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_number", "minimum_confidence", "must be a finite number from 0 through 1")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        _fail("out_of_range", "minimum_confidence", "must be a finite number from 0 through 1")
    return value


class DecisionProfilesMixin:
    """Decision Profile repository. Requires ``_connect`` and model lookup methods."""

    def list_decision_profiles(self, include_disabled: bool = True) -> List[Dict[str, Any]]:
        query = "SELECT * FROM decision_profiles"
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY profile_key"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [_public(row) for row in conn.execute(query).fetchall()]

    def get_decision_profile(self, profile_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM decision_profiles WHERE profile_key = ?", (profile_key,)).fetchone()
            return _public(row) if row else None

    def get_default_decision_model_ids(self) -> List[str]:
        """The global default decision models, in the order they are tried."""
        try:
            ids = json.loads(self.get_setting(DEFAULT_MODELS_SETTING, "") or "[]")
        except ValueError:
            ids = []
        if not isinstance(ids, list):
            return []
        return [i for i in ids if isinstance(i, str) and i.strip()][:MAX_DEFAULT_MODELS]

    def set_default_decision_model_ids(self, ids: Any) -> List[str]:
        """Replace the global default chain with up to MAX_DEFAULT_MODELS decision models."""
        if not isinstance(ids, (list, tuple)):
            _fail("invalid_list", "model_ids", "must be a list of model ids")
        if len(ids) > MAX_DEFAULT_MODELS:
            _fail("too_many_default_models", "model_ids", f"must name at most {MAX_DEFAULT_MODELS} models")
        chain: List[str] = []
        for index, model_id in enumerate(ids):
            model_id = self._validate_model_reference(model_id, f"model_ids[{index}]", required=True)
            if model_id in chain:
                _fail("duplicate_default_model", f"model_ids[{index}]", "is already in the default models")
            chain.append(model_id)
        self.set_setting(DEFAULT_MODELS_SETTING, json.dumps(chain))
        return chain

    def _validate_model_reference(self, model_id: Any, path: str, *, required: bool) -> Optional[str]:
        if model_id is None and not required:
            return None
        if not isinstance(model_id, str) or not model_id.strip():
            _fail("required" if required else "invalid_model_reference", path, "must reference a configured model")
        model = self.get_model_by_id(model_id)
        if not model:
            _fail("unknown_model", path, "does not reference a configured model")
        if model.get("type") != "decision":
            _fail("incompatible_model", path, "must reference a decision model")
        capabilities = model.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            _fail("incompatible_model", path, "must reference a model enabled for Decision Models")

        # model-capabilities/v1 is the canonical registry contract used by the
        # model selector. Keep accepting the original nested form so persisted
        # profiles and administrator-created legacy model rows remain valid.
        operations = capabilities.get("operations")
        schema_versions = capabilities.get("schema_versions")
        if isinstance(operations, dict):
            supports_decision = (
                operations.get("decision") not in (None, "unsupported")
                and operations.get("strict_output") not in (None, "unsupported")
            )
        else:
            decision = capabilities.get("decision")
            supports_decision = isinstance(decision, dict) and bool(decision.get("enabled"))
            schema_versions = decision.get("schema_versions", [SCHEMA_VERSION]) if supports_decision else None
        if not supports_decision:
            _fail("incompatible_model", path, "must reference a model enabled for Decision Models")
        if not isinstance(schema_versions, list) or SCHEMA_VERSION not in schema_versions:
            _fail("incompatible_schema", path, f"does not support {SCHEMA_VERSION}")
        return model["id"]

    def validate_decision_profile(self, data: Mapping[str, Any], *, existing: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Merge and validate data, canonicalising schema and model aliases."""
        if not isinstance(data, Mapping):
            _fail("invalid_payload", "$", "must be a JSON object")
        merged = dict(existing or {})
        merged.update(data)
        key = merged.get("profile_key")
        if not isinstance(key, str) or not PROFILE_KEY_PATTERN.fullmatch(key):
            _fail("invalid_key", "profile_key", "must be a dot-separated lowercase stable key")
        if existing and key != existing["profile_key"]:
            _fail("immutable_key", "profile_key", "cannot be renamed; create a new profile instead")
        merged["display_name"] = _text(merged.get("display_name"), "display_name", required=True, maximum=_MAX_DISPLAY_NAME_LENGTH)
        merged["description"] = _text(merged.get("description", ""), "description", required=False, maximum=_MAX_DESCRIPTION_LENGTH)
        if not isinstance(merged.get("enabled"), bool):
            _fail("invalid_boolean", "enabled", "must be true or false")
        if merged.get("mode") not in VALID_MODES:
            _fail("invalid_mode", "mode", "must be one of: off, shadow, enforce")
        raw_schema = merged.get("schema")
        try:
            schema = parse_schema(raw_schema)
        except ContractValidationError as exc:
            raise DecisionProfileValidationError(exc.code, f"schema.{exc.path}", str(exc)) from exc
        merged["schema"] = schema.to_dict()
        if merged.get("schema_version") != SCHEMA_VERSION:
            _fail("unsupported_schema_version", "schema_version", f"must be {SCHEMA_VERSION}")
        merged["policy_version"] = _text(merged.get("policy_version"), "policy_version", required=True, maximum=80)
        requires_model = bool(merged["enabled"]) and merged["mode"] != "off"
        requested_fallbacks, as_list = _requested_fallback_ids(data, existing)
        if len(requested_fallbacks) > MAX_FALLBACK_MODELS:
            _fail("too_many_fallback_models", "fallback_model_ids",
                  f"must name at most {MAX_FALLBACK_MODELS} models")
        primary = merged.get("primary_model_id")
        if requires_model and not (isinstance(primary, str) and primary.strip()) \
                and self.get_default_decision_model_ids():
            # No model of its own: the profile runs on the global default chain.
            merged["primary_model_id"] = ""
            fallback_ids = []
        elif requires_model:
            merged["primary_model_id"] = self._validate_model_reference(
                merged.get("primary_model_id"), "primary_model_id", required=True,
            )
            fallback_ids: List[str] = []
            for index, model_id in enumerate(requested_fallbacks):
                path = (f"fallback_model_ids[{index}]" if as_list or index
                        else "fallback_model_id")
                model_id = self._validate_model_reference(model_id, path, required=True)
                if model_id == merged["primary_model_id"]:
                    _fail("duplicate_fallback_model", path, "must differ from primary_model_id")
                if model_id in fallback_ids:
                    _fail("duplicate_fallback_model", path, "is already in the fallback models")
                fallback_ids.append(model_id)
        else:
            # Inactive built-ins are seeded before any compatible model exists.
            # Preserve an existing configured model so an administrator can
            # temporarily disable and later re-enable a profile without having
            # to select it again. Empty references remain valid for first-run
            # bootstrap rows and stale references remain inert while disabled.
            primary_model_id = merged.get("primary_model_id")
            merged["primary_model_id"] = primary_model_id.strip() if isinstance(primary_model_id, str) else ""
            fallback_ids = []
            for model_id in requested_fallbacks:
                model_id = model_id.strip() if isinstance(model_id, str) else ""
                if model_id and model_id not in fallback_ids:
                    fallback_ids.append(model_id)
        merged["fallback_model_ids"] = fallback_ids
        merged["fallback_model_id"] = fallback_ids[0] if fallback_ids else None
        merged["timeout_ms"] = _integer(merged.get("timeout_ms"), "timeout_ms", 1, _MAX_TIMEOUT_MS)
        merged["retry_limit"] = _integer(merged.get("retry_limit", 0), "retry_limit", 0, _MAX_RETRY_LIMIT)
        merged["minimum_confidence"] = _confidence(merged.get("minimum_confidence", 0.0))
        merged["sample_input"] = _sample_input(merged.get("sample_input"))
        return merged

    def create_decision_profile(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        profile = self.validate_decision_profile(data)
        try:
            with self._connect() as conn:
                conn.execute("""INSERT INTO decision_profiles (
                    profile_key, display_name, description, enabled, mode, schema_version,
                    policy_version, schema_json, primary_model_id, fallback_model_id, fallback_model_ids_json,
                    timeout_ms, retry_limit, minimum_confidence, sample_input_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    profile["profile_key"], profile["display_name"], profile["description"],
                    int(profile["enabled"]), profile["mode"], profile["schema_version"],
                    profile["policy_version"], json.dumps(profile["schema"], sort_keys=True, separators=(",", ":")),
                    profile["primary_model_id"], profile["fallback_model_id"],
                    json.dumps(profile["fallback_model_ids"]), profile["timeout_ms"],
                    profile["retry_limit"], profile["minimum_confidence"],
                    json.dumps(profile["sample_input"], ensure_ascii=False) if profile["sample_input"] else None,
                ))
        except sqlite3.IntegrityError as exc:
            _fail("already_exists", "profile_key", "already exists")
        return self.get_decision_profile(profile["profile_key"])

    def update_decision_profile(self, profile_key: str, data: Mapping[str, Any], revision: int) -> Optional[Dict[str, Any]]:
        existing = self.get_decision_profile(profile_key)
        if not existing:
            return None
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            _fail("invalid_revision", "revision", "must be a positive integer")
        profile = self.validate_decision_profile(data, existing=existing)
        with self._connect() as conn:
            cursor = conn.execute("""UPDATE decision_profiles SET
                display_name = ?, description = ?, enabled = ?, mode = ?, schema_version = ?,
                policy_version = ?, schema_json = ?, primary_model_id = ?, fallback_model_id = ?,
                fallback_model_ids_json = ?, timeout_ms = ?, retry_limit = ?, minimum_confidence = ?, sample_input_json = ?, revision = revision + 1,
                updated_at = CURRENT_TIMESTAMP
                WHERE profile_key = ? AND revision = ?""", (
                profile["display_name"], profile["description"], int(profile["enabled"]), profile["mode"],
                profile["schema_version"], profile["policy_version"],
                json.dumps(profile["schema"], sort_keys=True, separators=(",", ":")),
                profile["primary_model_id"], profile["fallback_model_id"],
                json.dumps(profile["fallback_model_ids"]), profile["timeout_ms"],
                profile["retry_limit"], profile["minimum_confidence"],
                json.dumps(profile["sample_input"], ensure_ascii=False) if profile["sample_input"] else None,
                profile_key, revision,
            ))
            if not cursor.rowcount:
                _fail("conflict", "revision", "does not match the latest profile revision")
        return self.get_decision_profile(profile_key)

    def delete_decision_profile(self, profile_key: str) -> bool:
        if profile_key in BUILTIN_PROFILE_KEYS:
            _fail("builtin_profile", "profile_key", "built-in profiles cannot be deleted; disable them instead")
        with self._connect() as conn:
            return conn.execute("DELETE FROM decision_profiles WHERE profile_key = ?", (profile_key,)).rowcount > 0
