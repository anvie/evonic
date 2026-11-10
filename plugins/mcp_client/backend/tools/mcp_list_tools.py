"""
mcp_list_tools.py — list tools exposed by configured MCP servers.

Companion to mcp_call. Servers are configured via the mcp_client plugin's
MCP_SERVERS variable; the plugin's enabled flag gates both tools.

The listing is compact by default: a big toolset with every raw JSON schema
runs to tens of thousands of characters and is truncated by the runtime before
the model ever sees the tool names it needs. Each tool is therefore rendered as
name + short description + argument signature. Use ``filter`` or
``include_schema`` to fetch full ``input_schema`` payloads for a narrow subset.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

# Hard cap on a single tool description inside a listing.
_DESC_LIMIT = 200

# Enum values rendered inline in an argument signature before "+N".
_MAX_ENUM_VALUES = 8

# Listings are kept below this size so the runtime never truncates them.
_SOFT_CAP = 20000

_HINT = (
    "Compact listing: 'args' is a signature — 'name:type!' is required, "
    "'name:type?' optional, 'mode[a|b]' lists allowed values. "
    "Call mcp_list_tools(server, filter='<substring>') for the full "
    "input_schema of matching tools, names_only=true for names alone, or "
    "include_schema=true for every tool's schema."
)


def _enabled() -> bool:
    from models.db import db
    return db.get_setting("plugin_enabled:mcp_client") == "1"


def _flag(value: Any) -> bool:
    """Lenient boolean — models occasionally send 1 / "true" / "yes"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


def _trim(text: Any, limit: int = _DESC_LIMIT) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _type_label(spec: dict) -> str:
    """Render one JSON-schema property as a short type label."""
    label = spec.get("type")
    if isinstance(label, list):
        label = "|".join(str(x) for x in label)
    if not label:
        for key in ("anyOf", "oneOf", "allOf"):
            alts = spec.get(key)
            if not isinstance(alts, list):
                continue
            labels = [lab for lab in (_type_label(a) for a in alts if isinstance(a, dict)) if lab]
            if labels:
                label = "|".join(dict.fromkeys(labels))
                break
    label = str(label or "")
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        shown = [str(v) for v in enum[:_MAX_ENUM_VALUES]]
        extra = f"|+{len(enum) - _MAX_ENUM_VALUES}" if len(enum) > _MAX_ENUM_VALUES else ""
        return f"{label or 'enum'}[{'|'.join(shown)}{extra}]"
    return label


def _arg_signature(schema: Any) -> str:
    """Compact ``name:type!`` / ``name:type?`` signature for an input schema."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = schema.get("required")
    must = set(required) if isinstance(required, list) else set()
    parts = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        label = _type_label(spec)
        mark = "!" if name in must else "?"
        parts.append(f"{name}:{label}{mark}" if label else f"{name}{mark}")
    return ", ".join(parts)


def _compile_matcher(expr: str) -> Optional[Callable[[dict], bool]]:
    """Build a matcher for ``filter=``.

    A plain word (letters, digits, ``_``, ``-``, ``.``, space) matches as a
    case-insensitive substring of the tool name or description. Anything else
    is tried as a regex first, falling back to substring matching when the
    pattern does not compile.
    """
    pat = (expr or "").strip()
    if not pat:
        return None

    def _by_substring(tool: dict) -> bool:
        needle = pat.lower()
        return any(needle in (tool.get(field) or "").lower()
                   for field in ("name", "description"))

    if re.fullmatch(r"[A-Za-z0-9_.\- ]+", pat):
        return _by_substring
    try:
        rx = re.compile(pat, re.IGNORECASE)
    except re.error:
        return _by_substring

    def _by_regex(tool: dict) -> bool:
        return (bool(rx.search(tool.get("name") or ""))
                or bool(rx.search(tool.get("description") or "")))

    return _by_regex


def _entry(tool: dict, want_schema: bool) -> dict:
    schema = tool.get("inputSchema")
    schema = schema if isinstance(schema, dict) else {}
    entry: dict = {
        "name": tool.get("name", ""),
        "description": _trim(tool.get("description")),
    }
    signature = _arg_signature(schema)
    if signature:
        entry["args"] = signature
    if want_schema:
        entry["input_schema"] = schema
    return entry


def _render(name: str, tools: list, names_only: bool,
            matcher: Optional[Callable[[dict], bool]], want_schema: bool) -> dict:
    entries = [_entry(t, want_schema) for t in tools if isinstance(t, dict)]
    total = len(entries)
    if matcher is not None:
        entries = [e for e in entries if matcher(e)]

    if names_only:
        return {
            "server": name,
            "tool_count": total,
            "tools": [e["name"] for e in entries],
        }

    payload: dict = {
        "server": name,
        "tool_count": total,
        "shown": len(entries),
        "tools": entries,
    }
    if matcher is not None and len(entries) < total:
        payload["note"] = (
            f"{total - len(entries)} tool(s) hidden by filter; "
            "drop filter to see the whole list."
        )

    # Never hand the runtime a payload it would truncate mid-JSON: degrade in
    # steps until the listing fits, and say so in "note".
    if len(json.dumps(payload)) > _SOFT_CAP and any("input_schema" in e for e in entries):
        for entry in entries:
            entry.pop("input_schema", None)
        payload["note"] = (
            "Full input schemas omitted to stay under the tool-output limit; "
            "call with filter='<substring>' to fetch them for a narrow subset."
        )
    if len(json.dumps(payload)) > _SOFT_CAP:
        for entry in entries:
            if "description" in entry:
                entry["description"] = _trim(entry["description"], 80)
        payload["note"] = (
            "Descriptions shortened to stay under the tool-output limit; "
            "narrow with filter='<substring>' or names_only=true."
        )
    if len(json.dumps(payload)) > _SOFT_CAP:
        for entry in entries:
            entry.pop("description", None)
        payload["note"] = (
            "Descriptions omitted to stay under the tool-output limit; "
            "call with filter='<substring>' for one tool's details."
        )

    payload["hint"] = _HINT
    return payload


def execute(agent: dict, args: dict) -> Any:
    if not _enabled():
        return {"error": "MCP Client plugin is disabled. Enable it in Plugins settings."}

    from ._mcp_lib import MCPError, mcp_manager

    server = args.get("server")
    server = server.strip() if isinstance(server, str) else ""

    names_only = _flag(args.get("names_only"))
    matcher = _compile_matcher(args.get("filter") if isinstance(args.get("filter"), str) else "")
    want_schema = _flag(args.get("include_schema")) or matcher is not None

    try:
        names = [server] if server else mcp_manager.server_names()
    except MCPError as e:
        return {"error": str(e)}

    if not names:
        return {
            "error": "No MCP servers configured. Ask an admin to configure "
                     "MCP_SERVERS in the MCP Client plugin."
        }

    call_timeout, startup_timeout = mcp_manager.get_timeouts()
    results = []
    for name in names:
        try:
            srv = mcp_manager.get(name)
            tools = srv.list_tools(call_timeout, startup_timeout)
            results.append(_render(name, tools, names_only, matcher, want_schema))
        except MCPError as e:
            results.append({"server": name, "error": str(e)})

    return results[0] if server else {"servers": results}
