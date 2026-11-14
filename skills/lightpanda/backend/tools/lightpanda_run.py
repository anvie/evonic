"""lightpanda_run — execute a PandaScript in the session (deterministic, no LLM)."""

from __future__ import annotations

import os
import uuid

from ._lp import (
    DEFAULT_MAX_BYTES,
    LightpandaError,
    config,
    cookie_args,
    cookie_file,
    mcp_call,
    run_cli,
    shape_result,
    state_dir,
    truncate,
)


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def _cli_mode(script: str | None, path: str | None, timeout_ms: int,
              max_bytes: int, save_jar: bool) -> dict:
    """Run through `lightpanda run`, the only path that honours `--cookie`."""
    if script is not None:
        scripts = state_dir() / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        target = scripts / f"run-{uuid.uuid4().hex[:10]}.js"
        target.write_text(script, encoding="utf-8")
    else:
        target = os.path.expanduser(str(path))
        if not os.path.isabs(target):
            target = str(state_dir() / target)
        if not os.path.isfile(target):
            raise LightpandaError(f"script file not found: {target}")

    jar = cookie_file(config())
    has_jar = jar.is_file()
    # v1.0.4 `run` accepts --cookie but does not apply it to the Page context,
    # so we keep the flag for forward compatibility only.
    cmd = ["run", str(target)] + cookie_args(config(), save_jar=save_jar)
    proc = run_cli(cmd, timeout=max(30.0, timeout_ms / 1000.0 + 15))

    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    body, truncated = truncate(text, max_bytes)
    out = {"tool": "run", "mode": "cli", "text": body,
           "is_error": proc.returncode not in (0, None),
           "script_path": str(target),
           "cookies": {"applied": False, "file": str(jar) if has_jar else None,
                       "note": "lightpanda run ignores --cookie in 1.0.4; "
                               "use lightpanda_fetch(use_cookies=true, inject_script=...) "
                               "for authenticated pages."}}
    if truncated:
        out["truncated"] = True
    if proc.returncode not in (0, None) and proc.stderr:
        out["stderr"] = proc.stderr.strip()[:500]
    return out


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

        mode = str(args.get("mode") or "mcp").strip().lower()
        if mode not in ("mcp", "cli"):
            raise LightpandaError(f"unsupported mode '{mode}'; use 'mcp' or 'cli'")
        if mode == "cli":
            save_jar = args.get("save_jar")
            return _cli_mode(script, path, timeout_ms, max_bytes,
                             True if save_jar is None else bool(save_jar))

        call_args: dict = {"timeout": timeout_ms}
        if script:
            call_args["script"] = script
        else:
            call_args["path"] = str(path)

        # MCP calls block for the script's lifetime; leave protocol headroom.
        result = mcp_call("run", call_args, agent_id=_agent_id(agent),
                          timeout=max(30.0, timeout_ms / 1000.0 + 15))
        shaped = shape_result(result, tool="run", max_bytes=max_bytes)
        shaped["mode"] = "mcp"
        shaped["cookies"] = {"used": False,
                             "note": "MCP sessions cannot be seeded with cookies; "
                                     "use mode='cli' for authenticated pages."}
        return shaped
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
