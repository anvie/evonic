"""lightpanda_extract — schema-based structured extraction from the page."""

import json

from ._lp import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    LightpandaError,
    mcp_call,
    shape_result,
    validate_url,
)


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def execute(agent: dict, args: dict) -> dict:
    try:
        schema = args.get("schema")
        if isinstance(schema, dict):
            schema = json.dumps(schema)
        if not isinstance(schema, str) or not schema.strip():
            raise LightpandaError("schema is required (a JSON object mapping field names to CSS specs)")

        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
        try:
            max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES
        agent_id = _agent_id(agent)

        url = args.get("url")
        if url:
            url = validate_url(url)
            mcp_call("goto", {"url": url}, agent_id=agent_id, timeout=timeout)

        call_args = {"schema": schema}
        if args.get("save"):
            call_args["save"] = str(args["save"])

        result = mcp_call("extract", call_args, agent_id=agent_id, timeout=timeout)
        out = shape_result(result, tool="extract", max_bytes=max_bytes)
        if url:
            out["url"] = url
        return out
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
