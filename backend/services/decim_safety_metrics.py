"""Aggregate metrics for the Decim Safety dashboard.

Everything here is derived from sanitized ``decim_safety_events`` rows; no raw
command text or provider transport detail is ever read or returned.  The metric
helpers are intentionally read-only and tolerate an empty/absent table so the
dashboard can render before the first comparison is recorded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.services.decim_safety_telemetry import decided_by

logger = logging.getLogger(__name__)

# A final verdict is "unsafe" when a human must intervene or execution is
# refused: warning, requires_approval, or dangerous.
UNSAFE_LEVELS = ("warning", "requires_approval", "dangerous")
_DECISIONS = ("allow", "review", "block")
_AGREEMENTS = (
    "exact",
    "decim_more_conservative",
    "decim_more_permissive",
    "unavailable",
    "fallback",
)


_VERDICT_BUCKET = {"requires_approval": "approval", "dangerous": "blocked"}


def _connect():
    from models.db import db
    return db._connect()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _window_start(hours: int | None) -> str | None:
    if not hours or hours <= 0:
        return None
    from datetime import timedelta
    return _iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def _fetch(window_hours: int | None = None, *, mode: str | None = None,
           tool_type: str | None = None, agent_id: str | None = None) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    since = _window_start(window_hours)
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if mode in ("off", "shadow", "enforce"):
        where.append("mode = ?")
        params.append(mode)
    if tool_type in ("bash", "python"):
        where.append("tool_type = ?")
        params.append(tool_type)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT mode, tool_type, decim_attempted, decim_accepted, model_decision, "
            "model_confidence, model_latency_ms, deterministic_level, final_level, "
            "decision_source, agreement, fallback_reason, error_category, "
            "final_unsafe, model_unsafe, deterministic_unsafe, occurred_at, disposition "
            f"FROM decim_safety_events{clause} ORDER BY occurred_at ASC",
            params,
        ).fetchall()
    columns = [
        "mode", "tool_type", "decim_attempted", "decim_accepted", "model_decision",
        "model_confidence", "model_latency_ms", "deterministic_level", "final_level",
        "decision_source", "agreement", "fallback_reason", "error_category",
        "final_unsafe", "model_unsafe", "deterministic_unsafe", "occurred_at", "disposition",
    ]
    return [dict(zip(columns, row)) for row in rows]


def summary(window_hours: int | None = None, *, mode: str | None = None,
            tool_type: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    """Compute aggregate statistics for the dashboard."""
    try:
        rows = _fetch(window_hours, mode=mode, tool_type=tool_type, agent_id=agent_id)
    except Exception:
        logger.exception("Failed to compute Decim Safety summary")
        return _empty_summary()

    total = len(rows)
    if total == 0:
        return _empty_summary()

    attempted = sum(1 for r in rows if r["decim_attempted"])
    accepted = sum(1 for r in rows if r["decim_accepted"])
    fallbacks = sum(1 for r in rows if r["decision_source"] == "deterministic_fallback")

    decision_distribution = {d: 0 for d in _DECISIONS}
    for r in rows:
        if r["model_decision"] in decision_distribution:
            decision_distribution[r["model_decision"]] += 1

    agreement_matrix = {a: 0 for a in _AGREEMENTS}
    for r in rows:
        if r["agreement"] in agreement_matrix:
            agreement_matrix[r["agreement"]] += 1

    final_verdicts = {"allowed": 0, "approval": 0, "blocked": 0}
    for r in rows:
        final_verdicts[_VERDICT_BUCKET.get(r["final_level"], "allowed")] += 1

    decided = {"dmss": 0, "hmads": 0}
    for r in rows:
        decided[decided_by(r["decision_source"], r["model_decision"], r["final_level"])] += 1

    error_categories: dict[str, int] = {}
    for r in rows:
        reason = r["fallback_reason"] or r["error_category"]
        if reason:
            error_categories[reason] = error_categories.get(reason, 0) + 1

    final_unsafe = sum(1 for r in rows if r["final_unsafe"])
    model_unsafe = sum(1 for r in rows if r["model_unsafe"])
    deterministic_unsafe = sum(1 for r in rows if r["deterministic_unsafe"])

    confidences = [float(r["model_confidence"]) for r in rows
                   if r["model_confidence"] is not None]
    latencies = [float(r["model_latency_ms"]) for r in rows
                 if r["model_latency_ms"] is not None]

    return {
        "total_comparisons": total,
        "decim_attempted": attempted,
        "decim_accepted": accepted,
        "fallback_count": fallbacks,
        "fallback_rate": round(fallbacks / total, 4) if total else 0.0,
        "decision_distribution": decision_distribution,
        "agreement_matrix": agreement_matrix,
        "final_verdicts": final_verdicts,
        "decided_by": decided,
        "unsafe": {
            "final": final_unsafe,
            "final_rate": round(final_unsafe / total, 4) if total else 0.0,
            "model": model_unsafe,
            "deterministic": deterministic_unsafe,
        },
        "confidence": {
            "count": len(confidences),
            "min": min(confidences) if confidences else None,
            "max": max(confidences) if confidences else None,
            "mean": round(sum(confidences) / len(confidences), 4) if confidences else None,
        },
        "latency_ms": {
            "count": len(latencies),
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
        },
        "error_categories": error_categories,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "total_comparisons": 0,
        "decim_attempted": 0,
        "decim_accepted": 0,
        "fallback_count": 0,
        "fallback_rate": 0.0,
        "decision_distribution": {d: 0 for d in _DECISIONS},
        "agreement_matrix": {a: 0 for a in _AGREEMENTS},
        "final_verdicts": {"allowed": 0, "approval": 0, "blocked": 0},
        "decided_by": {"dmss": 0, "hmads": 0},
        "unsafe": {"final": 0, "final_rate": 0.0, "model": 0, "deterministic": 0},
        "confidence": {"count": 0, "min": None, "max": None, "mean": None},
        "latency_ms": {"count": 0, "p50": None, "p95": None, "max": None},
        "error_categories": {},
    }


def trend(window_hours: int = 24 * 7, bucket: str = "day", *, mode: str | None = None,
          tool_type: str | None = None) -> dict[str, Any]:
    """Time-bucketed comparisons, unsafe rate, and fallback rate."""
    try:
        rows = _fetch(window_hours, mode=mode, tool_type=tool_type)
    except Exception:
        logger.exception("Failed to compute Decim Safety trend")
        return {"bucket": bucket, "points": []}

    buckets: dict[str, dict[str, int]] = {}
    for r in rows:
        key = _bucket_key(r["occurred_at"], bucket)
        if key is None:
            continue
        slot = buckets.setdefault(key, {"total": 0, "unsafe": 0, "fallback": 0})
        slot["total"] += 1
        slot["unsafe"] += 1 if r["final_unsafe"] else 0
        slot["fallback"] += 1 if r["decision_source"] == "deterministic_fallback" else 0

    points = []
    for key in sorted(buckets):
        slot = buckets[key]
        total = slot["total"] or 1
        points.append({
            "bucket": key,
            "total": slot["total"],
            "unsafe": slot["unsafe"],
            "unsafe_rate": round(slot["unsafe"] / total, 4),
            "fallback": slot["fallback"],
            "fallback_rate": round(slot["fallback"] / total, 4),
        })
    return {"bucket": bucket, "points": points}


def _bucket_key(occurred_at: str | None, bucket: str) -> str | None:
    if not occurred_at:
        return None
    text = str(occurred_at).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if bucket == "hour":
        return dt.strftime("%Y-%m-%dT%H:00")
    return dt.strftime("%Y-%m-%d")
