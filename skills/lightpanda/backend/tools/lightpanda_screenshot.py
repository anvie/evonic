"""lightpanda_screenshot — render the page to a PNG file (base64 kept out of context)."""

import os
from pathlib import Path

from ._lp import (
    DEFAULT_TIMEOUT,
    LightpandaError,
    mcp_call,
    save_png,
    validate_url,
)


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def execute(agent: dict, args: dict) -> dict:
    try:
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
        agent_id = _agent_id(agent)

        url = args.get("url")
        if url:
            url = validate_url(url)
            mcp_call("goto", {"url": url}, agent_id=agent_id, timeout=timeout)

        call_args: dict = {}
        if args.get("selector"):
            call_args["selector"] = str(args["selector"])
        if args.get("full_page"):
            call_args["fullPage"] = True

        result = mcp_call("screenshot", call_args, agent_id=agent_id, timeout=timeout)
        images = [b for b in result.get("content") or []
                  if isinstance(b, dict) and b.get("type") == "image" and b.get("data")]
        if not images:
            text = " ".join(str(b.get("text", "")) for b in result.get("content") or []
                            if isinstance(b, dict))
            raise LightpandaError(f"screenshot returned no image data. {text}".strip())

        blob = images[0]
        target = args.get("path")
        if target:
            path = Path(os.path.expanduser(str(target)))
            path.parent.mkdir(parents=True, exist_ok=True)
            import base64
            path.write_bytes(base64.b64decode(blob["data"]))
        else:
            path = save_png(blob["data"], prefix="page")

        out = {"path": str(path), "bytes": path.stat().st_size,
               "mime_type": blob.get("mimeType", "image/png")}
        if url:
            out["url"] = url
        return out
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
