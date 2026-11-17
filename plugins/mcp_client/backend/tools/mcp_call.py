"""
mcp_call.py — invoke a tool on a configured MCP server.

Companion to mcp_list_tools. The agent supplies only a server *name*; the
command/args/env behind it are admin-configured via the mcp_client plugin.
"""

from __future__ import annotations

import json
from typing import Any

# Defensive cap on the raw result stored in the DB/timeline; the LLM-facing
# string is truncated separately by the runtime (AGENT_MAX_TOOL_RESULT_CHARS).
_MAX_RESULT_CHARS = 64_000


def execute(agent: dict, args: dict) -> Any:
    from models.db import db
    if db.get_setting("plugin_enabled:mcp_client") != "1":
        return {"error": "MCP Client plugin is disabled. Enable it in Plugins settings."}

    from ._mcp_lib import MCPError, content_to_text, mcp_manager

    server = args.get("server")
    server = server.strip() if isinstance(server, str) else ""
    tool = args.get("tool")
    tool = tool.strip() if isinstance(tool, str) else ""

    if not server:
        return {"error": "'server' parameter is required. Use mcp_list_tools to see configured servers."}
    if not tool:
        return {"error": "'tool' parameter is required. Use mcp_list_tools to see available tools."}

    # Guard against the LLM passing arguments as a JSON string instead of an object.
    arguments = args.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {"error": "'arguments' must be a JSON object matching the tool's input_schema."}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"error": "'arguments' must be a JSON object matching the tool's input_schema."}

    call_timeout, startup_timeout = mcp_manager.get_timeouts()
    timeout = call_timeout
    timeout_sec = args.get("timeout_sec")
    if isinstance(timeout_sec, (int, float)) and timeout_sec > 0:
        timeout = max(1.0, min(float(timeout_sec), call_timeout))

    try:
        srv = mcp_manager.get(server)
        result = srv.call_tool(tool, arguments, timeout, startup_timeout)
    except MCPError as e:
        return {"error": str(e), "server": server, "tool": tool}

    text = content_to_text(result.get("content"))
    structured = result.get("structuredContent")
    if structured is not None:
        try:
            sjson = json.dumps(structured, ensure_ascii=False, indent=1)
        except (TypeError, ValueError):
            sjson = str(structured)
        if sjson not in text:
            text = (text + "\n" if text else "") + sjson
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n...[output truncated by mcp_call]"

    if result.get("isError"):
        return {"error": text or "MCP tool reported an error", "server": server, "tool": tool}
    return {"result": text, "server": server, "tool": tool}
