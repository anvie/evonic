"""Privacy-bounded Decision Activity persistence and query helpers.

Decision activity is diagnostic metadata plus, while the ``decision_activity_record_input`` setting is on
(the default), the *validated input* of each invocation, so an event can be reproduced in the Playground.
Policy metadata, provider responses, credentials, and free-form error messages are never accepted here.

The input is stored only if it passes the same checks the runtime applies before calling a provider
(size and secret-like keys or values); anything else is withheld and the event says why.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Mapping

_MAX_TEXT = 256
_MAX_CORRELATION = 256
_MAX_SUMMARY_FIELDS = 32
_MAX_CANDIDATES = 16
_MAX_ROW_BYTES = 12 * 1024
_MAX_INPUT_BYTES = 16 * 1024
RECORD_INPUT_SETTING = "decision_activity_record_input"
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCES = frozenset({"production", "playground", "profile_test"})
_SECRET_KEY = re.compile(r"(?i)(?:api[_-]?key|authorization|password|secret|token)")
_UNSAFE_SNAPSHOT_KEY = re.compile(r"(?i)(?:raw|input|context|payload|prompt|response|provider)")
_SECRET_VALUE = re.compile(r"(?i)(?:api[_-]?key|authorization|password|secret|token)\s*[:=]")


def _bounded_text(value: Any, maximum: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] if value else None


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str):
            return value[:_MAX_TEXT]
        return value
    return None


def _summary(value: Any) -> dict[str, Any]:
    """Keep only bounded normalized field values; never provider candidates/logits."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(value):
        if (len(result) >= _MAX_SUMMARY_FIELDS or not isinstance(key, str)
                or not _SAFE_KEY.fullmatch(key) or _SECRET_KEY.search(key)
                or _UNSAFE_SNAPSHOT_KEY.search(key)):
            continue
        scalar = _safe_scalar(value[key])
        if scalar is not None and (not isinstance(scalar, str) or not _SECRET_VALUE.search(scalar)):
            result[key] = scalar
    return result


def _analysis(value: Any) -> dict[str, Any]:
    """Per-field confidence and the probability of each option the model weighed (what the Playground draws as bars).

    Only the schema's own option values and numbers are kept; provider logits and any other payload are never accepted.
    """
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(value):
        item = value[key]
        if (len(result) >= _MAX_SUMMARY_FIELDS or not isinstance(key, str) or not isinstance(item, Mapping)
                or not _SAFE_KEY.fullmatch(key) or _SECRET_KEY.search(key) or _UNSAFE_SNAPSHOT_KEY.search(key)):
            continue
        entry: dict[str, Any] = {}
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1:
            entry["confidence"] = float(confidence)
        candidates = []
        for candidate in (item.get("candidates") or [])[:_MAX_CANDIDATES]:
            if not isinstance(candidate, Mapping):
                continue
            option, probability = _safe_scalar(candidate.get("value")), candidate.get("probability")
            if (option is None or isinstance(option, str) and _SECRET_VALUE.search(option) or isinstance(probability, bool)
                    or not isinstance(probability, (int, float)) or not 0 <= probability <= 1):
                continue
            candidates.append({"value": option, "probability": float(probability)})
        if candidates:
            entry["candidates"] = candidates
        if entry:
            result[key] = entry
    return result


def _looks_secret(value: Any) -> bool:
    """True if any key or text anywhere in ``value`` resembles a credential."""
    if isinstance(value, str):
        return bool(_SECRET_VALUE.search(value))
    if isinstance(value, Mapping):
        return any((isinstance(k, str) and bool(_SECRET_KEY.search(k))) or _looks_secret(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_looks_secret(v) for v in value)
    return False


def _input_snapshot(value: Any) -> tuple[str | None, str | None]:
    """(json, status) for an invocation input. status: recorded | withheld | too_large | invalid | None."""
    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        return None, "invalid"
    if _looks_secret(value):
        return None, "withheld"
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, "invalid"
    if len(encoded.encode("utf-8")) > _MAX_INPUT_BYTES:
        return None, "too_large"
    return encoded, "recorded"


def _public(row: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    event = dict(row)
    event["fallback_used"] = bool(event["fallback_used"])
    event["redacted"] = bool(event["redacted"])
    event["outcome_summary"] = json.loads(event.pop("outcome_json")) if detail else None
    analysis = event.pop("analysis_json", None)
    if detail:
        event["field_analysis"] = json.loads(analysis) if analysis else {}
    if not detail:
        event.pop("outcome_json", None)
    raw_input = event.pop("input_json", None)
    event["has_input"] = raw_input is not None
    if detail:
        event["input_context"] = json.loads(raw_input) if raw_input is not None else None
    return event


class DecisionActivityMixin:
    """SQLite repository for bounded, admin-observable Decision Activity events."""

    def record_decision_activity(self, event: Mapping[str, Any]) -> str:
        """Persist a sanitized diagnostic event and return its opaque identifier.

        This method rejects malformed operational metadata but silently omits
        unsafe snapshot values. Callers should treat failure as non-fatal.
        """
        source = event.get("source")
        if source not in _SOURCES:
            raise ValueError("invalid decision activity source")
        profile_key = _bounded_text(event.get("profile_key"))
        profile_version = _bounded_text(event.get("profile_version"), 80)
        if not profile_key or not profile_version:
            raise ValueError("decision activity requires profile identity")
        event_id = str(uuid.uuid4())
        summary = _summary(event.get("outcome_summary"))
        if self.get_setting(RECORD_INPUT_SETTING, "1") == "0":
            input_json, input_status = None, ("off" if event.get("input_context") is not None else None)
        else:
            input_json, input_status = _input_snapshot(event.get("input_context"))
        raw = {
            "id": event_id, "source": source, "profile_key": profile_key,
            "profile_version": profile_version,
            "model_id": _bounded_text(event.get("model_id")),
            "adapter_key": _bounded_text(event.get("adapter_key")),
            "result_class": "error" if event.get("error_code") else "success",
            "error_code": _bounded_text(event.get("error_code"), 80),
            "confidence": event.get("confidence"), "latency_ms": event.get("latency_ms"),
            "attempts": event.get("attempts", 0),
            "fallback_used": 1 if event.get("fallback_used") else 0,
            "fallback_reason": _bounded_text(event.get("fallback_reason"), 80),
            "correlation_id": _bounded_text(event.get("correlation_id"), _MAX_CORRELATION),
            "outcome_json": json.dumps(summary, sort_keys=True, separators=(",", ":")),
            "created_at": float(event.get("created_at", time.time())),
        }
        if not isinstance(raw["confidence"], (int, float)) or isinstance(raw["confidence"], bool): raw["confidence"] = None
        if raw["confidence"] is not None and not 0 <= raw["confidence"] <= 1: raw["confidence"] = None
        if not isinstance(raw["latency_ms"], int) or raw["latency_ms"] < 0: raw["latency_ms"] = None
        if not isinstance(raw["attempts"], int) or raw["attempts"] < 0: raw["attempts"] = 0
        if len(json.dumps(raw, separators=(",", ":")).encode()) > _MAX_ROW_BYTES:
            raw["outcome_json"] = "{}"
        raw["input_json"], raw["input_status"] = input_json, input_status
        analysis = json.dumps(_analysis(event.get("field_analysis")), sort_keys=True, separators=(",", ":"))
        raw["analysis_json"] = analysis if len(analysis.encode()) <= _MAX_ROW_BYTES else None
        with self._connect() as conn:
            conn.execute("""INSERT INTO decision_activity
                (id, source, profile_key, profile_version, model_id, adapter_key,
                 result_class, error_code, confidence, latency_ms, attempts,
                 fallback_used, fallback_reason, correlation_id, outcome_json, created_at,
                 input_json, input_status, analysis_json)
                VALUES (:id, :source, :profile_key, :profile_version, :model_id, :adapter_key,
                        :result_class, :error_code, :confidence, :latency_ms, :attempts,
                        :fallback_used, :fallback_reason, :correlation_id, :outcome_json, :created_at,
                        :input_json, :input_status, :analysis_json)""", raw)
        return event_id

    def list_decision_activity(self, *, limit: int = 50, cursor: tuple[float, str] | None = None,
                               filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        filters = filters or {}
        clauses, values = ["redacted = 0"], []
        columns = {"source", "profile_key", "model_id", "result_class", "error_code", "correlation_id"}
        for key in columns:
            value = filters.get(key)
            if value is not None:
                clauses.append(f"{key} = ?")
                values.append(value)
        if filters.get("fallback_used") is not None:
            clauses.append("fallback_used = ?"); values.append(1 if filters["fallback_used"] else 0)
        for key, operator in (("from", ">="), ("to", "<=")):
            if filters.get(key) is not None:
                clauses.append(f"created_at {operator} ?"); values.append(float(filters[key]))
        if cursor:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            values.extend([cursor[0], cursor[0], cursor[1]])
        sql = "SELECT * FROM decision_activity WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, id DESC LIMIT ?"
        values.append(limit)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [_public(row) for row in conn.execute(sql, values).fetchall()]

    def get_decision_activity(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM decision_activity WHERE id = ? AND redacted = 0", (event_id,)).fetchone()
            return _public(row, detail=True) if row else None

    def redact_decision_activity(self, event_id: str) -> bool:
        with self._connect() as conn:
            result = conn.execute("UPDATE decision_activity SET outcome_json = '{}', analysis_json = NULL, correlation_id = NULL, input_json = NULL, redacted = 1 WHERE id = ? AND redacted = 0", (event_id,))
            return result.rowcount == 1

    def clear_decision_activity(self) -> int:
        """Delete every recorded activity event, redacted ones included."""
        with self._connect() as conn:
            return conn.execute("DELETE FROM decision_activity").rowcount

    def purge_decision_activity(self, before: float) -> int:
        with self._connect() as conn:
            return conn.execute("DELETE FROM decision_activity WHERE created_at < ?", (float(before),)).rowcount
