"""Shared helpers: plugin config, auth, validation and error shapes.

Everything the console can trigger goes through here first, so the rules
(auth, agent-id prefix, phone format) are defined once.
"""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

PLUGIN_ID = "tiyasancloud"
API_PREFIXES = ("/plugin/tiyasancloud/v1", "/plugin/tiyasan/v1")

_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PHONE_RE = re.compile(r"^[1-9][0-9]{6,17}$")

DEFAULTS = {
    "ACCESS_TOKEN": "",
    "SHARED_CHANNEL_ID": "",
    "SHARED_NUMBER": "",
    "MAX_LINKED_PER_AGENT": 1,
    "PAIR_CODE_TTL_MINUTES": 10,
    # Comma-separated. tc_<user> = one agent per console user; id_<slug> = the
    # older per-server agents, still served.
    "AGENT_ID_PREFIX": "tc_,id_",
    "ENFORCE_QUOTA": True,
    "PAIRING_POLL_SECONDS": 3,
    # Tools that execute through the workplace, i.e. inside the instance
    # sandbox. Everything else runs on the Evonic host and is never granted.
    "ALLOWED_TOOLS": "bash,read_file,write_file,str_replace,patch,runpy,calculator",
}


def config() -> dict:
    """Plugin settings from Evonic, with defaults filled in."""
    try:
        from backend.plugin_manager import plugin_manager
        stored = plugin_manager.get_plugin_config(PLUGIN_ID) or {}
    except Exception:
        stored = {}
    merged = dict(DEFAULTS)
    for key, value in stored.items():
        if value not in ("", None):
            merged[key] = value
    return merged


def config_int(key: str) -> int:
    try:
        return int(config().get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def config_bool(key: str) -> bool:
    value = config().get(key, DEFAULTS[key])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def authorized(request) -> bool:
    """Bearer or X-Access-Token, compared in constant time."""
    expected = str(config().get("ACCESS_TOKEN") or "")
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else ""
    supplied = supplied or request.headers.get("X-Access-Token", "").strip()
    return bool(expected and supplied) and hmac.compare_digest(supplied, expected)


# ----------------------------------------------------------------- validation

def agent_prefixes() -> Tuple[str, ...]:
    raw = str(config().get("AGENT_ID_PREFIX") or "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def valid_agent_id(value: Any) -> Optional[str]:
    """Managed agent id: matches the id pattern and carries one of the configured prefixes."""
    agent_id = str(value or "").strip()
    prefixes = agent_prefixes()
    if not _AGENT_ID_RE.fullmatch(agent_id) or not prefixes or not agent_id.startswith(prefixes):
        return None
    return agent_id


def valid_phone(value: Any) -> Optional[str]:
    """E.164 digits without '+'; tolerates '+', spaces and dashes on input."""
    phone = re.sub(r"[\s+\-()]", "", str(value or ""))
    return phone if _PHONE_RE.fullmatch(phone) else None


def parse_time(value: Any, field: str) -> datetime:
    """ISO-8601 with a timezone → aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must be ISO-8601")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def in_minutes(minutes: int) -> datetime:
    return now() + timedelta(minutes=minutes)


# --------------------------------------------------------------------- errors

class ApiError(Exception):
    """Error with the wire shape the console expects: {error, message}."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def response(self) -> Tuple[dict, int]:
        return {"error": self.code, "message": self.message}, self.status


def invalid_body(message: str) -> ApiError:
    return ApiError(400, "invalid_body", message)


def agent_not_found(agent_id: str) -> ApiError:
    return ApiError(404, "agent_not_found", f"agent '{agent_id}' does not exist")


def evonic_unavailable(detail: str) -> ApiError:
    return ApiError(503, "evonic_unavailable", detail)
