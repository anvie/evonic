"""Monthly token quota: limits, usage and enforcement.

Usage comes from Token Monitor's public API (``usage_db.by_agent``), which
already folds sub-agents into their parent. Enforcement uses two Evonic hooks:
a turn gate (hard cap and suspension answer without calling the LLM) and a
message interceptor (soft-cap warning injected into the running turn).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .common import ApiError, config_bool, invalid_body, iso, now, parse_time
from . import store

DEFAULT_MESSAGES = {
    "en": {
        "soft": "Your AI quota for this month is almost used up. Keep answers short and avoid large jobs.",
        "hard": "The AI quota for this month is used up. Upgrade the plan at tiyasancloud.com/console/billing or wait for the next period.",
        "suspended": "This subscription has ended. Renew it at tiyasancloud.com/console/billing.",
    },
    "id": {
        "soft": "Kuota AI bulan ini tinggal sedikit. Jawab ringkas dan hindari pekerjaan besar.",
        "hard": "Kuota AI bulan ini sudah habis. Upgrade paket di tiyasancloud.com/console/billing atau tunggu periode berikutnya.",
        "suspended": "Langganan kamu sudah berakhir. Perpanjang di tiyasancloud.com/console/billing.",
    },
}

# A tiny cache keeps the gate cheap when several turns start at once.
_CACHE_SECONDS = 30
_cache: Dict[str, Tuple[float, int]] = {}


def _int_or_none(value: Any, field: str, *, low: int, high: int) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise invalid_body(f"'{field}' must be a whole number")
    if number < low or number > high:
        raise invalid_body(f"'{field}' must be between {low} and {high}")
    return number


def parse_limits(body: Dict[str, Any], locale: str = "en") -> Dict[str, Any]:
    if "tokens_monthly" not in body:
        raise invalid_body("'tokens_monthly' is required (null = unlimited)")
    tokens_monthly = _int_or_none(body.get("tokens_monthly"), "tokens_monthly", low=0, high=10 ** 12)
    soft_pct = _int_or_none(body.get("soft_pct", 80), "soft_pct", low=1, high=99) or 80
    # Limits are sent once, at registration, so the period rolls by itself:
    # the calendar month (UTC) unless the console names the first period.
    period = body.get("period")
    if period is None:
        start, end = calendar_month()
    elif isinstance(period, dict):
        try:
            start = parse_time(period.get("start"), "period.start")
            end = parse_time(period.get("end"), "period.end")
        except ValueError as exc:
            raise invalid_body(str(exc))
        if end <= start:
            raise invalid_body("'period.end' must be after 'period.start'")
    else:
        raise invalid_body("'period' must be an object with 'start' and 'end'")
    messages = body.get("messages") or {}
    if not isinstance(messages, dict):
        raise invalid_body("'messages' must be an object")
    defaults = DEFAULT_MESSAGES.get(locale, DEFAULT_MESSAGES["en"])
    resolved = {key: str(messages.get(key) or defaults[key])[:1000] for key in ("soft", "hard", "suspended")}
    return {
        "tokens_monthly": tokens_monthly,
        "soft_pct": soft_pct,
        "hard": bool(body.get("hard", True)),
        "suspended": bool(body.get("suspended", False)),
        "period_start": iso(start),
        "period_end": iso(end),
        "rollup_subagents": bool(body.get("rollup_subagents", True)),
        "messages": resolved,
    }


def calendar_month(at: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """The calendar month (UTC) that contains ``at``: [start, end)."""
    at = at or now()
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def current_limits(agent_id: str) -> Optional[Dict[str, Any]]:
    """Stored limits with the period rolled forward: once a period has ended,
    the calendar month we are in takes its place (no renewal call exists)."""
    limits = store.get_limits(agent_id)
    if limits and parse_time(limits["period_end"], "period_end") <= now():
        start, end = calendar_month()
        limits = {**limits, "period_start": iso(start), "period_end": iso(end)}
    return limits


def save(agent_id: str, limits: Dict[str, Any]) -> None:
    store.save_limits(agent_id, limits)
    _cache.pop(agent_id, None)


def used_tokens(agent_id: str, since_iso: str, rollup: bool = True, until_iso: Optional[str] = None) -> int:
    """Tokens spent by the agent (and its sub-agents) since ``since_iso``."""
    try:
        from plugins.token_monitor.db import usage_db
    except Exception as exc:
        raise ApiError(503, "token_monitor_unavailable", f"Token Monitor is unavailable: {exc}")
    since = since_iso.replace("Z", "+00:00")
    rows = usage_db.by_agent(since, rollup_subagents=rollup)
    total = 0
    for row in rows:
        if row.get("agent_id") == agent_id:
            total += int(row.get("total_tokens") or 0)
    if until_iso:
        # by_agent has no upper bound; a period that already ended is rare, so
        # trade exactness for one cheap call and report the open-ended total.
        pass
    return total


def _cached_used(agent_id: str, limits: Dict[str, Any]) -> int:
    import time
    hit = _cache.get(agent_id)
    stamp = time.time()
    if hit and stamp - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        total = used_tokens(agent_id, limits["period_start"], limits["rollup_subagents"])
    except ApiError:
        # Never block a customer because the monitor is down.
        total = hit[1] if hit else 0
    _cache[agent_id] = (stamp, total)
    return total


def state_of(limits: Dict[str, Any], used: int) -> Tuple[str, Optional[int]]:
    """Returns (state, percentage). state: ok | soft | hard | suspended."""
    if limits["suspended"]:
        return "suspended", None
    limit = limits["tokens_monthly"]
    if limit in (None, 0):
        return "ok", None
    # Compare exactly (not on the rounded percentage) so 79.9% is not "soft".
    pct = int(used * 100 // limit)
    if used >= limit and limits["hard"]:
        return "hard", pct
    if used * 100 >= limits["soft_pct"] * limit:
        return "soft", pct
    return "ok", pct


def usage_view(agent_id: str, since: Optional[str] = None, until: Optional[str] = None,
               strict: bool = True) -> Dict[str, Any]:
    """Usage payload for GET /agents/:id/usage (and the summary in GET /agents/:id).

    ``strict=False`` is used by the agent view: a Token Monitor outage must not
    hide the agent itself, so usage comes back as state "unknown" instead.
    """
    # No limits (an agent registered without them, or made before this plugin):
    # usage is still counted, over the calendar month, with no cap.
    limits = current_limits(agent_id)
    if not limits:
        month_start, month_end = calendar_month()
        limits = {"suspended": False, "tokens_monthly": None, "hard": False, "soft_pct": 80,
                  "rollup_subagents": True, "period_start": iso(month_start), "period_end": iso(month_end)}
    start = since or limits["period_start"]
    try:
        used = used_tokens(agent_id, start, limits["rollup_subagents"], until)
    except ApiError:
        if strict:
            raise
        return {
            "period": {"start": limits["period_start"], "end": limits["period_end"]},
            "summary": {"used": None, "limit": limits["tokens_monthly"], "pct": None, "state": "unknown"},
            "used": None, "limit": limits["tokens_monthly"], "pct": None, "state": "unknown",
        }
    state, pct = state_of(limits, used)
    return {
        "period": {"start": limits["period_start"], "end": limits["period_end"]},
        "summary": {"used": used, "limit": limits["tokens_monthly"], "pct": pct, "state": state},
        "used": used,
        "limit": limits["tokens_monthly"],
        "pct": pct,
        "state": state,
        "soft_pct": limits["soft_pct"],
        "hard": limits["hard"],
        "suspended": limits["suspended"],
    }


# ------------------------------------------------------------------ enforcement

def _managed(agent_id: str) -> Optional[Dict[str, Any]]:
    from .common import valid_agent_id
    if not agent_id or not valid_agent_id(agent_id):
        return None
    return current_limits(agent_id)


def _parent_of(agent_id: str) -> str:
    """Sub-agents inherit their parent's quota (spec section 4, rule 6)."""
    import re
    return re.sub(r"_(sub|explorer|organizer)_\d+$", "", agent_id or "")


def turn_gate(context: dict) -> Optional[dict]:
    """Hard cap and suspension: answer without calling the LLM."""
    if not config_bool("ENFORCE_QUOTA"):
        return None
    agent_id = _parent_of(str(context.get("agent_id") or ""))
    limits = _managed(agent_id)
    if not limits:
        return None
    used = _cached_used(agent_id, limits)
    state, _pct = state_of(limits, used)
    if state in ("hard", "suspended"):
        _notify(agent_id, limits, state, used)
        return {"handled": True, "response": limits["messages"].get(state) or DEFAULT_MESSAGES["en"][state]}
    if state == "soft":
        _notify(agent_id, limits, state, used)
    return None


def message_interceptor(agent_id: str, content: str, messages: list) -> Optional[dict]:
    """Soft cap: nudge the agent once per turn, without ending it."""
    if not config_bool("ENFORCE_QUOTA"):
        return None
    parent = _parent_of(str(agent_id or ""))
    limits = _managed(parent)
    if not limits:
        return None
    used = _cached_used(parent, limits)
    state, _pct = state_of(limits, used)
    if state != "soft":
        return None
    warning = f"[system] {limits['messages'].get('soft') or DEFAULT_MESSAGES['en']['soft']}"
    # The interceptor runs after every tool step; warn once per turn.
    if any(isinstance(m, dict) and m.get("content") == warning for m in (messages or [])):
        return None
    return {"inject": warning}


def tool_result_gate(context: dict, tool_name: str, args: dict, result) -> Optional[dict]:
    """Mid-turn overrun: end the turn at the next tool result."""
    if not config_bool("ENFORCE_QUOTA"):
        return None
    agent_id = _parent_of(str(context.get("agent_id") or ""))
    limits = _managed(agent_id)
    if not limits or not limits["hard"]:
        return None
    used = _cached_used(agent_id, limits)
    state, _pct = state_of(limits, used)
    if state in ("hard", "suspended"):
        return {"terminate_turn": True,
                "response": limits["messages"].get(state) or DEFAULT_MESSAGES["en"][state]}
    return None


def _notify(agent_id: str, limits: Dict[str, Any], state: str, used: int) -> None:
    """Send token_soft / token_hard once per state per period."""
    if state not in ("soft", "hard"):
        return
    if store.notice_seen(agent_id, limits["period_start"], state):
        return
    from . import callbacks
    pct = None
    if limits["tokens_monthly"]:
        pct = int(round(used * 100 / limits["tokens_monthly"]))
    callbacks.send(agent_id, f"token_{state}", {"used": used, "limit": limits["tokens_monthly"], "pct": pct})


def invalidate(agent_id: str) -> None:
    _cache.pop(agent_id, None)


def touched() -> str:
    return iso(now())
