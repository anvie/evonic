"""lightpanda_read — read the current page (or a URL) from the MCP session."""

from ._lp import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    LightpandaError,
    mcp_call,
    shape_result,
    validate_url,
)

FORMATS = {
    "markdown": "markdown",
    "tree": "tree",
    "html": "html",
    "links": "links",
    "structured_data": "structuredData",
}


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def execute(agent: dict, args: dict) -> dict:
    try:
        fmt = str(args.get("format") or "markdown").strip().lower()
        if fmt not in FORMATS:
            raise LightpandaError(f"unsupported format '{fmt}'; use one of {sorted(FORMATS)}")
        try:
            max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
        agent_id = _agent_id(agent)

        url = args.get("url")
        if url:
            url = validate_url(url)
            mcp_call("goto", {"url": url,
                              **({"waitUntil": args["wait_until"]} if args.get("wait_until") else {})},
                     agent_id=agent_id, timeout=timeout)

        tool = FORMATS[fmt]
        call_args: dict = {}
        # Scoping only applies to the DOM-returning tools.
        if tool in ("markdown", "html", "tree"):
            if args.get("selector"):
                call_args["selector"] = str(args["selector"])
            elif args.get("backend_node_id"):
                call_args["backendNodeId"] = int(args["backend_node_id"])

        if tool in ("markdown", "html"):
            call_args["maxBytes"] = max_bytes
        if tool == "tree":
            if args.get("max_depth"):
                call_args["maxDepth"] = int(args["max_depth"])
            if not url:
                call_args["url"] = ""
        if tool == "links" and args.get("limit"):
            call_args["limit"] = int(args["limit"])

        result = mcp_call(tool, call_args, agent_id=agent_id, timeout=timeout)
        out = shape_result(result, tool=fmt, max_bytes=max_bytes)
        out["format"] = fmt
        if url:
            out["url"] = url
        else:
            try:
                current = mcp_call("getUrl", {}, agent_id=agent_id, timeout=timeout)
                text = "".join(b.get("text", "") for b in current.get("content") or []
                               if isinstance(b, dict))
                if text.strip():
                    out["url"] = text.strip()
            except LightpandaError:
                pass
        return out
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
