"""Persistence and comparison evidence for Decim Safety telemetry.

This module owns the sanitized ``decim_safety_events`` rows and the single-row
``decim_safety_telemetry_state`` aggregate.  It is deliberately free of any
provider transport details: callers hand it the already-normalized decision and
the deterministic verdict, and it stores only non-reversible, bounded metadata.

Recording policy (enforced by the caller in ``safety_pipeline``):

* ``off`` / disabled  -> no event is ever written here;
* ``shadow``          -> an event is written for every scoped comparison;
* ``enforce``         -> an event is written only when
  ``decim_safety.record_enforce_events`` is true.

Clearing here is retention management only: it cannot disable Decim, change the
mode/configuration, rewrite execution history, or affect an in-flight decision.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# A verdict is "unsafe" when a human must intervene or execution is refused.
UNSAFE_LEVELS = frozenset({"warning", "requires_approval", "dangerous"})
_SEVERITY = {"safe": 0, "warning": 1, "requires_approval": 2, "dangerous": 3}
_MODEL_SEVERITY = {"allow": 0, "review": 2, "block": 3}
_FINAL_TO_LEVEL = {"allow": "safe", "review": "requires_approval", "block": "dangerous"}


def _connect():
    from models.db import db
    return db._connect()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_unsafe(level: str | None) -> bool:
    """True when *level* represents warning / approval / dangerous."""
    return level in UNSAFE_LEVELS


def agreement_class(model_decision: str | None, deterministic_level: str | None,
                    *, accepted: bool) -> str:
    """Classify how the model verdict relates to the deterministic verdict."""
    if not accepted or model_decision is None:
        return "fallback"
    model_rank = _MODEL_SEVERITY.get(model_decision)
    det_rank = _SEVERITY.get(deterministic_level or "safe", 0)
    if model_rank is None:
        return "unavailable"
    if model_rank == det_rank:
        return "exact"
    return "decim_more_conservative" if model_rank > det_rank else "decim_more_permissive"


def _disposition_for(final_level: str | None) -> str:
    if final_level == "dangerous":
        return "blocked"
    if final_level == "requires_approval":
        return "approval_requested"
    return "executed"


def _current_epoch(conn) -> int:
    row = conn.execute(
        "SELECT telemetry_epoch FROM decim_safety_telemetry_state WHERE id = 1"
    ).fetchone()
    return int(row[0]) if row else 0


_EVENT_COLUMNS = [
    "id", "occurred_at", "telemetry_epoch", "mode", "tool_type", "execution_boundary",
    "decim_enabled", "decim_attempted", "decim_accepted", "provider_key", "model_id",
    "policy_version", "model_decision", "model_confidence", "model_latency_ms",
    "deterministic_level", "deterministic_score", "deterministic_categories",
    "final_level", "decision_source", "agreement", "fallback_reason", "error_category",
    "final_unsafe", "model_unsafe", "deterministic_unsafe", "disposition",
    "command_fingerprint", "command_length", "correlation_id", "expires_at", \
            "detection_type", "detection_command",
]


def record_comparison(
    *,
    settings: Any,
    code: str,
    tool_type: str,
    agent_context: dict[str, Any] | None,
    deterministic: dict[str, Any],
    model_decision: str | None = None,
    confidence: float | None = None,
    latency_ms: int | None = None,
    fallback_reason: str | None = None,
    decision_source: str,
    provider_key: str | None = None,
    model_id: str | None = None,
    policy_version: str | None = None,
    correlation_id: str | None = None,
    final_level: str | None = None,
) -> str | None:
    """Insert one sanitized comparison event.  Returns the event id or None.

    ``model_decision`` is the normalized ``allow``/``review``/``block`` choice,
    or ``None`` when the provider was unusable (a fallback).  Only fingerprints
    and lengths of the command are stored, never its text.
    """
    try:
        from backend.tools.lib.decim_safety import POLICY_VERSION, _execution_boundary, fingerprint_code
    except Exception:
        logger.exception("Decim Safety telemetry unavailable")
        return None

    try:
        accepted = model_decision is not None
        attempted = decision_source != "deterministic"
        if final_level is None:
            final_level = _FINAL_TO_LEVEL.get(model_decision) if accepted else deterministic.get("level")
        det_level = deterministic.get("level")
        mode = getattr(settings, "mode", "off")

        event_id = str(uuid4())
        occurred = _iso(_now())
        retention_days = int(getattr(settings, "retention_days", 30) or 30)
        expires_at = _iso(_now() + timedelta(days=retention_days))
        categories = deterministic.get("blocked_patterns") or []

        with _connect() as conn:
            epoch = _current_epoch(conn)
            conn.execute(
                "INSERT INTO decim_safety_events ("
                "id, occurred_at, telemetry_epoch, mode, tool_type, execution_boundary, "
                "decim_enabled, decim_attempted, decim_accepted, provider_key, model_id, "
                "policy_version, model_decision, model_confidence, model_latency_ms, "
                "deterministic_level, deterministic_score, deterministic_categories, "
                "final_level, decision_source, agreement, fallback_reason, error_category, "
                "final_unsafe, model_unsafe, deterministic_unsafe, disposition, "
                "command_fingerprint, command_length, correlation_id, expires_at, "
                "detection_type, detection_command"
                ") VALUES (" + ", ".join("?" for _ in range(33)) + ")",
                (
                    event_id, occurred, epoch, mode, tool_type, _execution_boundary(agent_context),
                    1 if getattr(settings, "enabled", False) else 0,
                    1 if attempted else 0, 1 if accepted else 0,
                    provider_key or getattr(settings, "provider", None),
                    model_id, policy_version or POLICY_VERSION,
                    model_decision, confidence, latency_ms,
                    det_level, deterministic.get("score"), ",".join(sorted(categories)),
                    final_level, decision_source,
                    agreement_class(model_decision, det_level, accepted=accepted),
                    fallback_reason, None,
                    1 if is_unsafe(final_level) else 0,
                    1 if is_unsafe(_FINAL_TO_LEVEL.get(model_decision)) else 0,
                    1 if is_unsafe(det_level) else 0,
                    _disposition_for(final_level),
                    fingerprint_code(code), len(code),
                    correlation_id, expires_at,
                    "", ""
                ),
            )
            conn.execute(
                "UPDATE decim_safety_telemetry_state SET total_recorded = total_recorded + 1, "
                "last_decision_at = ? WHERE id = 1",
                (occurred,),
            )
            conn.commit()
        return event_id
    except Exception:
        logger.exception("Failed to record Decim Safety telemetry event")
        return None


def update_disposition(event_id: str | None, disposition: str) -> bool:
    """Update the lifecycle disposition of an already-recorded event."""
    if not event_id or disposition not in {"executed", "approval_requested", "blocked", "execution_failed"}:
        return False
    try:
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE decim_safety_events SET disposition = ? WHERE id = ?",
                (disposition, event_id),
            )
            conn.commit()
            return bool(cursor.rowcount)
    except Exception:
        logger.exception("Failed to update Decim Safety event disposition")
        return False


def _row_to_dict(row, columns: list[str]) -> dict[str, Any]:
    return {name: row[idx] for idx, name in enumerate(columns)}


def get_event(event_id: str) -> dict[str, Any] | None:
    """Fetch a single stored event (sanitized; never includes raw code)."""
    try:
        with _connect() as conn:
            conn.row_factory = None
            row = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM decim_safety_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return _row_to_dict(row, _EVENT_COLUMNS) if row else None
    except Exception:
        logger.exception("Failed to read Decim Safety event")
        return None


def list_events(*, limit: int = 50, offset: int = 0, mode: str | None = None,
                tool_type: str | None = None, final_level: str | None = None,
                unsafe_only: bool = False, since: str | None = None) -> dict[str, Any]:
    """List sanitized events with server-side validated filters and pagination."""
    where: list[str] = []
    params: list[Any] = []
    if mode in {"off", "shadow", "enforce"}:
        where.append("mode = ?")
        params.append(mode)
    if tool_type in {"bash", "python"}:
        where.append("tool_type = ?")
        params.append(tool_type)
    if final_level in _SEVERITY:
        where.append("final_level = ?")
        params.append(final_level)
    if unsafe_only:
        where.append("final_unsafe = 1")
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    try:
        with _connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM decim_safety_events{clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM decim_safety_events{clause} "
                "ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {"events": [_row_to_dict(r, _EVENT_COLUMNS) for r in rows],
                "total": int(total), "limit": limit, "offset": offset}
    except Exception:
        logger.exception("Failed to list Decim Safety events")
        return {"events": [], "total": 0, "limit": limit, "offset": offset}


def clear(actor: str = "admin") -> dict[str, Any]:
    """Delete stored telemetry and roll the epoch.  Retention management only."""
    try:
        from backend.audit_logger import audit
        auditor = audit
    except Exception:
        auditor = None
    try:
        with _connect() as conn:
            deleted = conn.execute("SELECT COUNT(*) FROM decim_safety_events").fetchone()[0]
            conn.execute("DELETE FROM decim_safety_events")
            conn.execute(
                "UPDATE decim_safety_telemetry_state "
                "SET telemetry_epoch = telemetry_epoch + 1, cleared_at = ?, total_recorded = 0 WHERE id = 1",
                (_iso(_now()),),
            )
            conn.commit()
        if auditor is not None:
            try:
                auditor.log_setting_change(user_id=actor, key="decim_safety_telemetry.clear",
                                           old_value=str(deleted), new_value="0", ip="")
            except Exception:
                pass
        return {"success": True, "deleted": int(deleted)}
    except Exception:
        logger.exception("Failed to clear Decim Safety telemetry")
        return {"success": False, "deleted": 0}


def cleanup_expired() -> int:
    """Delete events past their retention expiry.  Returns the deleted count."""
    try:
        with _connect() as conn:
            cursor = conn.execute("DELETE FROM decim_safety_events WHERE expires_at < ?", (_iso(_now()),))
            conn.commit()
            return int(cursor.rowcount or 0)
    except Exception:
        logger.exception("Failed to clean up Decim Safety telemetry")
        return 0


def get_state() -> dict[str, Any]:
    """Return the aggregate telemetry state (epoch, last decision, counters)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT telemetry_epoch, cleared_at, last_decision_at, total_recorded "
                "FROM decim_safety_telemetry_state WHERE id = 1"
            ).fetchone()
        if not row:
            return {"telemetry_epoch": 0, "cleared_at": None, "last_decision_at": None, "total_recorded": 0}
        return {"telemetry_epoch": int(row[0]), "cleared_at": row[1],
                "last_decision_at": row[2], "total_recorded": int(row[3])}
    except Exception:
        return {"telemetry_epoch": 0, "cleared_at": None, "last_decision_at": None, "total_recorded": 0}
