"""
llm_tool_executor.py — tool execution chokepoint.

Holds the injection-cap constant for the loop plus the *simulation-mode
outbound side-effect interceptor*: the single, authoritative place that
short-circuits durable / externally-visible tool effects while an
agent-template simulation is running.

Part of the diet llm_loop.py refactor (Layout C / Pipeline).
"""

from __future__ import annotations

import contextvars
import datetime as _datetime
import json as _json
import logging
import threading as _threading
from typing import Any, Callable, Dict, List, Optional

_logger = logging.getLogger(__name__)

# Hard cap on how many times an injection may reset the iteration counter
# within a single loop run. Without this, continuous injections (e.g. autopilot
# kanban) could keep resetting _iteration forever — infinite loop.
MAX_INJECTIONS_PER_LOOP = 5


# ===========================================================================
# Simulation-mode outbound side-effect interceptor
# ===========================================================================
#
# An agent-template simulation (see backend/tools/lib/simulation_scope.py)
# recreates an agent with its FULL tool set so the model's reasoning stays
# faithful to what the real agent would do.  Workspace-scoped effects
# (bash/runpy/file/artifacts) are already contained by workspace redirection
# (task #24).  A second class of effect escapes that net because redirection
# cannot neutralise it: tools that reach OUTSIDE the workspace into durable or
# externally-visible state --
#
#   * create_schedule — durable, DELAYED and irreversible: the worst case,
#     because it fires *after* the simulation has been torn down;
#   * send_agent_message / send_channel_message / send_notification /
#     escalate_to_user / send_file — real messages delivered to other agents,
#     channels or the human user;
#   * remember / evomem / forget_memory — writes to durable long-term memory;
#   * kanban_* mutations — durable board state shared across agents;
#   * database_query / sshc — remote / external systems.
#
# The interceptor runs at the *single* tool-execution chokepoint
# (``llm_call._execute_tool_core``) so every execution path (serial loop,
# parallel pool, ATG DAG nodes) is covered.  It returns a synthetic success so
# the loop behaves as if the effect happened, while recording the call in a
# "simulated outbox" surface the UI can display.  It is a strict no-op for
# normal (non-simulation) agents.

#: Exact tool names whose execution is intercepted during a simulation.
SIMULATION_OUTBOUND_DENYLIST: frozenset = frozenset({
    # Durability + DELAYED + irreversible — worst case.
    "create_schedule", "update_schedule", "cancel_schedule",
    # Outbound messaging / delivery.
    "send_agent_message", "send_channel_message", "send_notification",
    "escalate_to_user", "send_file",
    # Durable memory writes.
    "remember", "evomem", "memorize", "save_memory", "forget_memory",
    # Remote / external state.
    "database_query", "sshc",
})

#: Whole tool namespaces (prefix match) that mutate durable shared state.
SIMULATION_OUTBOUND_PREFIXES: tuple = ("kanban_", "evomem_")

#: Read-only members of a denied namespace that stay allowed, so the model
#: keeps full situational awareness (fidelity).  Evaluated BEFORE the denylist.
SIMULATION_OUTBOUND_ALLOWLIST: frozenset = frozenset({
    "kanban_search_tasks", "kanban_get_task", "kanban_get_comments",
    "kanban_list_tasks", "kanban_my_tasks",
    "recall", "recall_sessions",
})

#: agent_context key the simulation runtime (T5) may set with a collector.
SIMULATED_OUTBOX_KEY = "simulated_outbox"

#
# Outbox collector plumbing
# -------------------------
# The collector captures intercepted calls.  It may be either a callable
# (``collector(entry: dict)``) or a list to which entries are appended.
#
# PRIMARY hook: ``agent_context[SIMULATED_OUTBOX_KEY]``.  Parallel tool calls
# run in worker threads where contextvars do not propagate, so the collector
# must ride on the agent-context dict the tools already receive.
#
# FALLBACK hook: :func:`set_simulated_outbox` (a contextvar) for code paths
# that lack the agent dict.
#
# A canonical in-memory surface (:func:`get_simulated_outbox`) is always
# maintained, keyed by simulation id, so the UI can render it even when no
# custom collector is supplied.

_active_outbox = contextvars.ContextVar("evonic_simulation_outbox", default=None)

#: sim_id -> list[dict]; the canonical simulated-outbox surface.
_SIM_OUTBOXES: Dict[str, List[dict]] = {}
_SIM_OUTBOXES_LOCK = _threading.Lock()


# ---- public API -----------------------------------------------------------

def set_simulated_outbox(collector: Any, sim_id: Optional[str] = None):
    """Register *collector* for the current context; returns a reset token.

    *collector* is a callable ``f(entry: dict)`` or a list to append to.  This
    is the fallback hook for contexts without an agent dict — prefer
    ``agent_context[SIMULATED_OUTBOX_KEY]`` (thread-safe for parallel tools).
    """
    return _active_outbox.set({"collector": collector, "sim_id": sim_id})


def clear_simulated_outbox(token: Any = None) -> None:
    """Deactivate the contextvar outbox hook (optionally via its token)."""
    if token is not None:
        try:
            _active_outbox.reset(token)
            return
        except (ValueError, LookupError):
            pass
    _active_outbox.set(None)


def get_simulated_outbox(sim_id: Optional[str] = None) -> List[dict]:
    """Return the canonical in-memory outbox list for *sim_id* (lazily created)."""
    key = str(sim_id) if sim_id else "__default__"
    with _SIM_OUTBOXES_LOCK:
        return _SIM_OUTBOXES.setdefault(key, [])


def reset_simulated_outbox(sim_id: Optional[str] = None) -> None:
    """Drop the stored outbox for *sim_id* (teardown helper)."""
    key = str(sim_id) if sim_id else "__default__"
    with _SIM_OUTBOXES_LOCK:
        _SIM_OUTBOXES.pop(key, None)


def is_outbound_side_effect(fn_name: str) -> bool:
    """True when *fn_name* is a denylisted outbound/durable side effect.

    The allowlist wins over both the exact denylist and the namespace prefixes.
    """
    if not fn_name:
        return False
    if fn_name in SIMULATION_OUTBOUND_ALLOWLIST:
        return False
    if fn_name in SIMULATION_OUTBOUND_DENYLIST:
        return True
    return any(fn_name.startswith(p) for p in SIMULATION_OUTBOUND_PREFIXES)


# ---- internals ------------------------------------------------------------

def _resolve_sim_id(agent_context: Optional[dict]) -> Optional[str]:
    if agent_context:
        sid = agent_context.get("simulation_id") or agent_context.get("sim_id")
        if sid:
            return str(sid)
    ctx = _active_outbox.get()
    if ctx and ctx.get("sim_id"):
        return str(ctx["sim_id"])
    # Fall back to the simulation_scope activation contextvar so entries are
    # keyed correctly even when only ``set_active_simulation`` was called (no
    # agent dict carries ``simulation_id``, e.g. background worker threads).
    try:
        from backend.tools.lib import simulation_scope as _sim
        sid = _sim.simulation_id(agent_context)
        if sid:
            return str(sid)
    except Exception:  # pragma: no cover - simulation_scope always importable
        pass
    return None


def _simulation_active(agent_context: Optional[dict]) -> bool:
    """True when this call must be intercepted (simulation is running).

    Authoritative source is :mod:`simulation_scope` (agent dict / contextvar).
    A registered outbox collector is also treated as an explicit opt-in.
    """
    try:
        from backend.tools.lib import simulation_scope as _sim
        if _sim.is_simulation(agent_context):
            return True
    except Exception:  # pragma: no cover - simulation_scope always importable
        pass
    if _active_outbox.get() is not None:
        return True
    if agent_context and agent_context.get(SIMULATED_OUTBOX_KEY) is not None:
        return True
    return False


def _resolve_collector(agent_context: Optional[dict]) -> Any:
    if agent_context:
        collector = agent_context.get(SIMULATED_OUTBOX_KEY)
        if collector is not None:
            return collector
    ctx = _active_outbox.get()
    if ctx:
        return ctx.get("collector")
    return None


def _safe_args(args: Any, limit: int = 2000) -> Any:
    """Return *args* if small enough to store, else a truncated JSON blob."""
    try:
        blob = _json.dumps(args, default=str)
    except Exception:
        return {"_unserializable": True}
    if len(blob) <= limit:
        return args
    return {"_truncated": blob[:limit]}


def _record_into_collector(collector: Any, entry: dict) -> None:
    if callable(collector):
        collector(entry)
    elif hasattr(collector, "append"):
        collector.append(entry)
    else:
        _logger.warning("Ignoring simulated-outbox collector of unsupported "
                        "type %r", type(collector).__name__)


def _synthetic_message(fn_name: str, args: dict) -> str:
    """Human-readable summary of the intercepted effect (shown as simulated)."""
    a = args or {}
    if fn_name == "create_schedule":
        name = a.get("name") or a.get("schedule_name") or "unnamed"
        return f"Schedule '{name}' created (simulated)."
    if fn_name == "update_schedule":
        return "Schedule updated (simulated)."
    if fn_name == "cancel_schedule":
        return "Schedule cancelled (simulated)."
    if fn_name == "send_agent_message":
        target = a.get("target_agent_id") or a.get("target") or "agent"
        return f"Message sent to '{target}' (simulated)."
    if fn_name == "send_channel_message":
        return "Channel message sent (simulated)."
    if fn_name == "send_notification":
        return "Notification sent (simulated)."
    if fn_name == "escalate_to_user":
        return "Escalation delivered to the originating user (simulated)."
    if fn_name == "send_file":
        return f"File '{a.get('file_path') or ''}' sent (simulated)."
    if fn_name in ("remember", "memorize", "save_memory", "evomem"):
        return "Memory stored (simulated)."
    if fn_name == "forget_memory":
        return "Memory entry removed (simulated)."
    if fn_name.startswith("kanban_"):
        return f"Kanban change '{fn_name}' applied (simulated)."
    if fn_name == "database_query":
        return "Database query executed (simulated)."
    if fn_name == "sshc":
        return "SSH command executed (simulated)."
    return f"'{fn_name}' executed (simulated)."


def _synthetic_success(fn_name: str, args: dict, entry: dict) -> dict:
    """Build a plausible success payload mirroring each tool's real shape."""
    result = {
        "success": True,
        "simulated": True,
        "message": entry["summary"],
        "tool": fn_name,
        "outbox_id": entry["outbox_id"],
    }
    if fn_name == "create_schedule":
        result["schedule_id"] = entry["outbox_id"]
    elif fn_name == "escalate_to_user":
        result["escalation_id"] = entry["outbox_id"]
    elif fn_name == "send_agent_message":
        result["tip"] = ("Reply will be forwarded to your session automatically. "
                         "To continue the conversation, call send_agent_message "
                         "again.")
    return result


def intercept_simulation_outbound(fn_name: str, args: Optional[dict] = None,
                                  agent_context: Optional[dict] = None,
                                  ) -> Optional[dict]:
    """Intercept a denylisted outbound side effect while a simulation runs.

    Returns a synthetic success dict (so the loop behaves as if the effect
    happened) and records the call in the simulated outbox.  Returns ``None``
    when *fn_name* is not a denylisted side effect, or when no simulation is
    active — the caller must then execute the tool normally.  Never raises.
    """
    if not is_outbound_side_effect(fn_name):
        return None
    if not _simulation_active(agent_context):
        return None

    args = args or {}
    sim_id = _resolve_sim_id(agent_context)
    msg = _synthetic_message(fn_name, args)

    try:
        store = get_simulated_outbox(sim_id)
        entry = {
            "outbox_id": f"sim-out-{len(store) + 1}",
            "timestamp": _datetime.datetime.now(
                _datetime.timezone.utc).isoformat(),
            "tool": fn_name,
            "args": _safe_args(args),
            "summary": msg,
            "simulated": True,
            "simulation_id": sim_id,
        }
        with _SIM_OUTBOXES_LOCK:
            store.append(entry)
        collector = _resolve_collector(agent_context)
        if collector is not None and collector is not store:
            _record_into_collector(collector, entry)
    except Exception:  # pragma: no cover - capture must never break the loop
        _logger.warning("Failed to record simulated-outbox entry for %s",
                        fn_name, exc_info=True)
        entry = {"outbox_id": "sim-out-?", "summary": msg, "tool": fn_name}

    _logger.info("Simulation intercepted outbound tool '%s' (sim=%s) -> %s",
                 fn_name, sim_id, msg)
    try:
        return _synthetic_success(fn_name, args, entry)
    except Exception:  # pragma: no cover - defensive
        return {"success": True, "simulated": True, "message": msg}
