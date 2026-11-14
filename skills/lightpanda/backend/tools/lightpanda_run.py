"""lightpanda_run — execute a PandaScript in the session (deterministic, no LLM)."""

from ._lp import (
    DEFAULT_MAX_BYTES,
    LightpandaError,
    mcp_call,
    shape_result,
)


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def execute(agent: dict, args: dict) -> dict:
    try:
        script = args.get("script")
        path = args.get("path")
        if not script and not path:
            raise LightpandaError("provide 'script' (source) or 'path' (a saved .js file)")
        if script is not None and not isinstance(script, str):
            raise LightpandaError("'script' must be a string")

        try:
            timeout_ms = int(args.get("timeout_ms") or 60000)
        except (TypeError, ValueError):
            timeout_ms = 60000
        try:
            max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES

        call_args: dict = {"timeout": timeout_ms}
        if script:
            call_args["script"] = script
        else:
            call_args["path"] = str(path)

        # MCP calls block for the script's lifetime; leave protocol headroom.
        result = mcp_call("run", call_args, agent_id=_agent_id(agent),
                          timeout=max(30.0, timeout_ms / 1000.0 + 15))
        return shape_result(result, tool="run", max_bytes=max_bytes)
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
