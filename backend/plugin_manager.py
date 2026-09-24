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
import threading

_logger = logging.getLogger(__name__)

_plugin_manager_lock = threading.Lock()

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

_plugin_manager_instance = None


def get_plugin_manager():
    """Return the process-wide PluginManager, creating it on first use.

    Deferred on purpose: constructing a PluginManager loads and executes every
    enabled plugin handler, so merely importing this module must stay free of
    side effects. Short-lived processes (CLI commands, scripts, tests) import it
    too, and running plugin module-level code there can mutate shared state the
    live server owns.
    """
    global _plugin_manager_instance
    if _plugin_manager_instance is None:
        with _plugin_manager_lock:
            if _plugin_manager_instance is None:
                # Publish the instance before loading handlers: plugin module
                # level code may look the manager up while it is being populated.
                instance = PluginManager(load_plugins=False)
                _plugin_manager_instance = instance
                instance._load_all()
    return _plugin_manager_instance


def __getattr__(name):
    """Lazy accessor so ``from backend.plugin_manager import plugin_manager``
    keeps working for callers while staying side-effect free on import."""
    if name == 'plugin_manager':
        return get_plugin_manager()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
