"""
mcp_list_servers.py — list the MCP servers configured for this platform.

Companion to mcp_list_tools and mcp_call. Reads the mcp_client plugin's
MCP_SERVERS variable only; nothing is spawned or connected, and no commands,
URLs or headers are exposed.
"""

from __future__ import annotations

from typing import Any


def execute(agent: dict, args: dict) -> Any:
    from models.db import db
    if db.get_setting("plugin_enabled:mcp_client") != "1":
        return {"error": "MCP Client plugin is disabled. Enable it in Plugins settings."}

    from ._mcp_lib import MCPError, mcp_manager

    try:
        servers = mcp_manager.list_servers()
    except MCPError as e:
        return {"error": str(e)}

    if not servers:
        return {
            "error": "No MCP servers configured. Ask an admin to configure "
                     "MCP_SERVERS in the MCP Client plugin."
        }

    return {
        "servers": servers,
        "hint": "Call mcp_list_tools(server) to see a server's tools, then mcp_call to use one.",
    }
