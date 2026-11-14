"""lightpanda_mcp — raw passthrough to any of Lightpanda's 40 MCP tools."""

from ._lp import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    LightpandaError,
    mcp_call,
    save_png,
    shape_result,
)


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def execute(agent: dict, args: dict) -> dict:
    try:
        tool = str(args.get("tool") or "").strip()
        if not tool:
            raise LightpandaError("tool is required (e.g. 'interactiveElements', 'evaluate')")
        arguments = args.get("arguments") or {}
        if isinstance(arguments, str):
            import json
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                raise LightpandaError(f"arguments must be a JSON object: {e}") from e
        if not isinstance(arguments, dict):
            raise LightpandaError("arguments must be an object")

        try:
            timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        try:
            max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES

        if tool == "download":
            arguments = {**arguments}
            if not arguments.get("path"):
                import uuid
                arguments["path"] = f"evonic-download-{uuid.uuid4().hex[:8]}"

        result = mcp_call(tool, arguments, agent_id=_agent_id(agent), timeout=timeout)
        out = shape_result(result, tool=tool, max_bytes=max_bytes, include_images=True)

        saved = []
        for image in out.pop("images", []) or []:
            if image.get("data"):
                saved.append(str(save_png(image["data"], prefix=tool)))
        if saved:
            out["image_paths"] = saved
            out.pop("image_count", None)
        return out
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
