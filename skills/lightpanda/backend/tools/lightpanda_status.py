"""lightpanda_status — self-diagnostics: binary, server, sessions, safety settings."""

import os

from ._lp import (
    INSTALL_HINT,
    MIN_VERSION,
    SKILL_DIR,
    binary_source,
    binary_version,
    block_private,
    config,
    host_of,
    load_state,
    obey_robots,
    port_of,
    resolve_binary,
    server_alive,
    state_dir,
)


def execute(agent: dict, args: dict) -> dict:
    try:
        cfg = config()
        binary = resolve_binary(cfg)
        version = binary_version(binary, cfg) if binary else None
        state = load_state()
        alive = server_alive() if binary else False

        running_pid = state.get("pid")
        pid_alive = False
        if running_pid:
            try:
                os.kill(int(running_pid), 0)
                pid_alive = True
            except (OSError, ValueError):
                pid_alive = False

        hints = []
        if not binary:
            hints.append(f"No binary found. {INSTALL_HINT}")
        elif not version:
            hints.append("Binary is present but `lightpanda version` failed — "
                         "it may need a newer glibc. Run `./evonic doctor` for details.")
        if binary and not alive:
            hints.append("MCP server is not running; interactive tools start it automatically.")
        if running_pid and not pid_alive and not alive:
            hints.append("A stale server record exists; it is cleared on the next start.")

        return {
            "binary": {
                "found": bool(binary),
                "path": str(binary) if binary else None,
                "version": version,
                "source": binary_source(binary, cfg),
                "minimum_version": MIN_VERSION,
                "skill_dir": str(SKILL_DIR),
            },
            "server": {
                "running": alive,
                "host": host_of(cfg),
                "port": port_of(cfg),
                "pid": running_pid,
                "pid_alive": pid_alive,
                "log": str(state_dir() / "server.log"),
            },
            "sessions": state.get("sessions") or {},
            "safety": {
                "obey_robots": obey_robots(cfg),
                "block_private_networks": block_private(cfg),
            },
            "state_dir": str(state_dir()),
            "hints": hints,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
