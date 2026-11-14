"""lightpanda_act — drive the page: goto, click, fill, press, select, scroll, wait."""

from ._lp import (
    DEFAULT_TIMEOUT,
    LightpandaError,
    mcp_call,
    shape_result,
    validate_url,
)

ACTIONS = {
    "goto", "click", "fill", "press", "select_option", "set_checked",
    "hover", "scroll", "wait_for_selector", "wait_for_script", "wait_for_state",
}

# action -> (mcp tool, required tool arguments)
_MAP = {
    "click": ("click", ()),
    "fill": ("fill", ("value",)),
    "press": ("press", ("key",)),
    "select_option": ("selectOption", ("value",)),
    "set_checked": ("setChecked", ("checked",)),
    "hover": ("hover", ()),
    "scroll": ("scroll", ()),
    "wait_for_selector": ("waitForSelector", ("selector",)),
    "wait_for_script": ("waitForScript", ("script",)),
    "wait_for_state": ("waitForState", ("state",)),
}

_NEEDS_NODE = {"click", "fill", "press", "select_option", "set_checked", "hover"}


def _agent_id(agent: dict) -> str:
    return str((agent or {}).get("id") or "shared")


def execute(agent: dict, args: dict) -> dict:
    try:
        action = str(args.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise LightpandaError(f"unsupported action '{action}'; use one of {sorted(ACTIONS)}")
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
        agent_id = _agent_id(agent)

        if action == "goto":
            url = validate_url(args.get("url"))
            call_args: dict = {"url": url}
            if args.get("wait_until"):
                call_args["waitUntil"] = str(args["wait_until"])
            if args.get("timeout_ms"):
                call_args["timeout"] = int(args["timeout_ms"])
            tool = "goto"
        else:
            tool, required = _MAP[action]
            call_args = {}
            if args.get("selector"):
                call_args["selector"] = str(args["selector"])
            elif args.get("backend_node_id"):
                call_args["backendNodeId"] = int(args["backend_node_id"])
            if action in _NEEDS_NODE and not call_args:
                raise LightpandaError(f"action '{action}' needs a selector or backend_node_id")

            for key in required:
                if key not in args or args[key] is None:
                    raise LightpandaError(f"action '{action}' needs '{key}'")
                call_args[key] = args[key]
            if action == "set_checked":
                call_args["checked"] = bool(call_args["checked"])
            if action == "scroll":
                for axis in ("x", "y"):
                    if args.get(axis) is not None:
                        call_args[axis] = int(args[axis])
            if action in ("wait_for_selector", "wait_for_script", "wait_for_state") \
                    and args.get("timeout_ms"):
                call_args["timeout"] = int(args["timeout_ms"])

        result = mcp_call(tool, call_args, agent_id=agent_id, timeout=timeout)
        out = shape_result(result, tool=tool)
        out["action"] = action
        return out
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
