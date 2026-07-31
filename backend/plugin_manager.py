"""
Plugin Manager — orchestrates plugin lifecycle and hook registries.

Split into three files for maintainability:
- plugin_hooks.py   — 6 hook registries (tool guard, message interceptor, turn context,
                       busy message provider, builtin suppressor, state handler)
- plugin_lifecycle.py — PluginManager class (load/unload/reload, install/uninstall,
                          enable/disable, config, discovery)
- plugin_manager.py  — this file, thin orchestrator wiring the two above.

All public APIs are re-exported here for backward compatibility.
Existing imports like `from backend.plugin_manager import plugin_manager` continue to work.
"""

import logging

_logger = logging.getLogger(__name__)

# ── Import & re-export hooks ────────────────────────────────────────────────

from backend.plugin_hooks import (  # noqa: F401
    # Synchronous lifecycle gates
    register_turn_gate, unregister_turn_gate, run_turn_gates,
    register_tool_result_gate, unregister_tool_result_gate, run_tool_result_gates,
    # Tool Guard
    register_tool_guard, unregister_tool_guard, check_tool_guards,
    # Message Interceptor
    register_message_interceptor, unregister_message_interceptor,
    run_message_interceptors,
    # Turn Context Provider
    register_turn_context_provider, unregister_turn_context_provider,
    get_turn_context,
    # Busy Message Provider
    register_busy_message_provider, unregister_busy_message_provider,
    get_busy_message,
    # Builtin Suppressor
    register_builtin_suppressor, unregister_builtin_suppressor,
    should_suppress_builtin,
    # State Handler
    register_state_handler, unregister_state_handler,
    _unload_plugin_state_handlers, dispatch_state, get_state_summary,
)

# ── Import & instantiate lifecycle ───────────────────────────────────────────

from backend.plugin_lifecycle import PluginManager  # noqa: F401

plugin_manager = PluginManager()
