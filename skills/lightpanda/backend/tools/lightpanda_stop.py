"""lightpanda_stop — stop the auto-started MCP server and drop all sessions."""

from ._lp import LightpandaError, clear_sessions, stop_server


def execute(agent: dict, args: dict) -> dict:
    try:
        result = stop_server()
        clear_sessions()
        return {"ok": True, **result}
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
