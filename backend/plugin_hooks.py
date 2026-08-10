"""
Plugin Hooks — registries for extending agent behavior.

Tool Guards, Message Interceptors, Turn Context Providers, Busy Message
Providers, Builtin Suppressors, and State Handlers — all live here as
module-level registries with register/unregister/dispatch functions.
"""

import logging
from typing import Dict, Optional, Callable

_logger = logging.getLogger(__name__)

# Generic synchronous short-circuit hooks. Context is trusted runtime data.
_turn_gates: list = []
_tool_result_gates: list = []

# Attachment-policy hooks run after send_file canonicalizes a path and before
# file metadata or bytes are exposed. Hooks receive (agent, canonical_path) and
# return None to allow or {"error": "..."} to reject the attachment.
_attachment_policies: list = []


def register_attachment_policy(fn: Callable) -> None:
    """Register a synchronous attachment policy hook."""
    if fn not in _attachment_policies:
        _attachment_policies.append(fn)


def unregister_attachment_policy(fn: Callable) -> None:
    """Remove a previously registered attachment policy hook."""
    if fn in _attachment_policies:
        _attachment_policies.remove(fn)


def check_attachment_policies(agent: dict, canonical_path: str) -> Optional[dict]:
    """Return the first rejection from attachment policy hooks."""
    for policy in list(_attachment_policies):
        try:
            result = policy(agent, canonical_path)
            if result:
                return result
        except Exception:
            _logger.exception("Plugin attachment policy failed")
    return None


def register_turn_gate(fn: Callable) -> None:
    if fn not in _turn_gates:
        _turn_gates.append(fn)


def unregister_turn_gate(fn: Callable) -> None:
    if fn in _turn_gates:
        _turn_gates.remove(fn)


def run_turn_gates(context: dict) -> Optional[dict]:
    directives = {}
    for gate in list(_turn_gates):
        try:
            result = gate(context)
            if not result:
                continue
            if result.get('handled'):
                return {**directives, **result}
            if result.get('suppress_intermediate'):
                directives['suppress_intermediate'] = True
            if result.get('required_tool'):
                directives['required_tool'] = str(result['required_tool'])
        except Exception:
            _logger.exception("Plugin turn gate failed")
    return directives or None


def register_tool_result_gate(fn: Callable) -> None:
    if fn not in _tool_result_gates:
        _tool_result_gates.append(fn)


def unregister_tool_result_gate(fn: Callable) -> None:
    if fn in _tool_result_gates:
        _tool_result_gates.remove(fn)


def run_tool_result_gates(context: dict, tool_name: str, args: dict,
                          result) -> Optional[dict]:
    for gate in list(_tool_result_gates):
        try:
            decision = gate(context, tool_name, args, result)
            if decision and decision.get('terminate_turn'):
                return decision
        except Exception:
            _logger.exception("Plugin tool-result gate failed")
    return None


# ═══════════════════════════════════════════════════════════════════
# Tool Guard Registry
# ═══════════════════════════════════════════════════════════════════
# A generic synchronous pre-execution hook that plugins can use to block
# specific tool calls. Guards are plain callables:
#   guard(agent_id: str, tool_name: str, args: dict) -> Optional[dict]
# Return {'block': True, 'error': '...'} to block, or None to allow.

_tool_guards: list = []


def register_tool_guard(fn: Callable) -> None:
    """Register a synchronous pre-execution tool guard."""
    if fn not in _tool_guards:
        _tool_guards.append(fn)


def unregister_tool_guard(fn: Callable) -> None:
    """Remove a previously registered tool guard."""
    if fn in _tool_guards:
        _tool_guards.remove(fn)


def check_tool_guards(agent_id: str, tool_name: str, args: dict,
                      context: dict = None) -> Optional[dict]:
    """Run guards, passing trusted context to guards that accept it."""
    for guard in list(_tool_guards):
        try:
            try:
                result = guard(agent_id, tool_name, args, context or {})
            except TypeError:
                result = guard(agent_id, tool_name, args)
            if result and (result.get('block') or result.get('level') == 'requires_approval'):
                return result
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════
# Message Interceptor Registry
# ═══════════════════════════════════════════════════════════════════
# A generic synchronous hook that plugins can use to inject system messages into
# the LLM conversation after an intermediate agent response. Interceptors are
# called after tool execution and before the next LLM call (intermediate only).
#   interceptor(agent_id: str, content: str, messages: list) -> Optional[dict]
# Return None to pass, or {'inject': 'text'} / {'inject': [msg_dicts]} to inject.

_message_interceptors: list = []


def register_message_interceptor(fn: Callable) -> None:
    """Register a synchronous post-response message interceptor."""
    if fn not in _message_interceptors:
        _message_interceptors.append(fn)


def unregister_message_interceptor(fn: Callable) -> None:
    """Remove a previously registered message interceptor."""
    if fn in _message_interceptors:
        _message_interceptors.remove(fn)


def run_message_interceptors(agent_id: str, content: str, messages: list) -> list:
    """Run all registered interceptors. Returns a flat list of message dicts to inject."""
    injections = []
    for interceptor in list(_message_interceptors):
        try:
            result = interceptor(agent_id, content, messages)
            if not result:
                continue
            inject = result.get('inject')
            if not inject:
                continue
            _logger.debug("Interceptor injecting: %s", inject)
            if isinstance(inject, str):
                injections.append({"role": "user", "content": inject})
            elif isinstance(inject, list):
                injections.extend(inject)
        except Exception:
            pass
    return injections


# Final-response handlers run before a no-tool response becomes visible or is
# persisted. A handler may accept the response, replace its content, or request
# one ephemeral provider retry with a revised message list.
_final_response_handlers: Dict[str, Callable] = {}


def register_final_response_handler(namespace: str, fn: Callable) -> None:
    _final_response_handlers[namespace] = fn


def unregister_final_response_handler(namespace: str) -> None:
    _final_response_handlers.pop(namespace, None)


def run_final_response_handlers(context: dict) -> Optional[dict]:
    for namespace, handler in list(_final_response_handlers.items()):
        try:
            result = handler(context)
            if isinstance(result, dict) and (
                    result.get("retry") or isinstance(result.get("content"), str)):
                return {**result, "namespace": namespace}
        except Exception:
            _logger.exception("Plugin final-response handler failed: %s", namespace)
    return None


# ═══════════════════════════════════════════════════════════════════
# User Message Transformer Registry
# ═══════════════════════════════════════════════════════════════════

_user_message_transformers: Dict[str, Callable] = {}


def register_user_message_transformer(namespace: str, fn: Callable) -> None:
    """Register a named transformer for the newest user message."""
    _user_message_transformers[namespace] = fn


def unregister_user_message_transformer(namespace: str) -> None:
    _user_message_transformers.pop(namespace, None)


def apply_user_message_transformers(agent_id: str, session_id: str,
                                    messages: list) -> bool:
    """Transform only the newest user message, preserving non-text media parts."""
    target = next(
        (message for message in reversed(messages)
         if isinstance(message, dict) and message.get("role") == "user"),
        None,
    )
    if target is None:
        return False

    changed = False

    def transform(text: str) -> str:
        nonlocal changed
        current = text
        for namespace, fn in list(_user_message_transformers.items()):
            try:
                result = fn(agent_id, session_id, current)
                if isinstance(result, str):
                    changed = changed or result != current
                    current = result
            except Exception:
                _logger.exception("Plugin user-message transformer failed: %s", namespace)
        return current

    content = target.get("content")
    if isinstance(content, str):
        target["content"] = transform(content)
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" \
                    and isinstance(part.get("text"), str):
                part = {**part, "text": transform(part["text"])}
            parts.append(part)
        target["content"] = parts
    return changed


# ═══════════════════════════════════════════════════════════════════
# Turn Context Provider Registry
# ═══════════════════════════════════════════════════════════════════
# Plugins can supply tools + system messages that should be present at the start of
# each agent turn. Providers are called once per _run_tool_loop call.
#   provider(agent_id: str, session_id: str) -> Optional[dict]
# Return None to skip, or:
#   {"id": "x", "tools": [...tool_defs...], "system_md": "...",
#    "system_mode": "preserve|append|replace",
#    "prefill_messages": [{"role": "user|assistant", "content": "..."}]}

_turn_context_providers: list = []


def register_turn_context_provider(fn: Callable) -> None:
    """Register a turn context provider."""
    if fn not in _turn_context_providers:
        _turn_context_providers.append(fn)


def unregister_turn_context_provider(fn: Callable) -> None:
    """Remove a previously registered turn context provider."""
    if fn in _turn_context_providers:
        _turn_context_providers.remove(fn)


def get_turn_context(agent_id: str, session_id: str) -> list:
    """Run all registered turn context providers. Returns a list of context dicts."""
    results = []
    for provider in list(_turn_context_providers):
        try:
            ctx = provider(agent_id, session_id)
            if ctx:
                results.append(ctx)
        except Exception:
            _logger.exception("Plugin turn-context provider failed")
    return results


def apply_turn_context(messages: list, tools: list, contexts: list) -> None:
    """Apply validated, ephemeral plugin context before conversation history."""
    additions = []
    replacement = None
    known_tools = {t.get("function", {}).get("name") for t in tools}
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        system_md = ctx.get("system_md")
        if isinstance(system_md, str) and system_md.strip():
            mode = ctx.get("system_mode", "append")
            if mode == "replace":
                replacement = system_md[:32000]
            elif mode != "preserve":
                additions.append({"role": "system", "content": system_md[:32000]})
        prefill = ctx.get("prefill_messages")
        if isinstance(prefill, list):
            for msg in prefill[:8]:
                if not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}:
                    continue
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    additions.append({"role": msg["role"], "content": content[:16000]})
        for tool in ctx.get("tools") or []:
            name = tool.get("function", {}).get("name") if isinstance(tool, dict) else None
            if name and name not in known_tools:
                tools.append(tool)
                known_tools.add(name)
    if replacement is not None:
        system = next((message for message in messages
                       if isinstance(message, dict) and message.get("role") == "system"), None)
        if system is None:
            messages.insert(0, {"role": "system", "content": replacement})
        else:
            system["content"] = replacement
    messages[1:1] = additions


# Read-only plugin status shown in the chat Agent State panel.
_agent_state_summary_providers: Dict[str, Callable] = {}


def register_agent_state_summary_provider(namespace: str, fn: Callable) -> None:
    _agent_state_summary_providers[namespace] = fn


def unregister_agent_state_summary_provider(namespace: str) -> None:
    _agent_state_summary_providers.pop(namespace, None)


def get_agent_state_summaries(agent_id: str, session_id: str) -> dict:
    summaries = {}
    for namespace, provider in list(_agent_state_summary_providers.items()):
        try:
            summary = provider(agent_id, session_id)
            if isinstance(summary, dict) and summary.get("state"):
                summaries[namespace] = summary
        except Exception:
            _logger.exception("Plugin agent-state summary failed: %s", namespace)
    return summaries


# ═══════════════════════════════════════════════════════════════════
# Busy Message Provider Registry
# ═══════════════════════════════════════════════════════════════════
# Plugins can register a provider that returns a human-readable message explaining
# why the agent is busy and cannot process the incoming session.
#   provider(agent_id: str, agent_state) -> Optional[str]
# Return a non-empty string to supply the message, or None to pass.
# Last-registered provider wins (highest priority first via reversed list).

_busy_message_providers: list = []


def register_busy_message_provider(fn: Callable) -> None:
    """Register a busy message provider."""
    if fn not in _busy_message_providers:
        _busy_message_providers.append(fn)


def unregister_busy_message_provider(fn: Callable) -> None:
    """Remove a previously registered busy message provider."""
    if fn in _busy_message_providers:
        _busy_message_providers.remove(fn)


def get_busy_message(agent_id: str, agent_state) -> Optional[str]:
    """Ask registered providers for a contextual busy rejection message.
    Returns the first non-empty response (last registered = highest priority)."""
    for provider in reversed(list(_busy_message_providers)):
        try:
            msg = provider(agent_id, agent_state)
            if msg:
                return msg
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════
# Builtin Tool Suppressor Registry
# ═══════════════════════════════════════════════════════════════════
# Plugins can register a suppressor that inspects a builtin/global tool and
# decides whether it should be hidden from a specific agent.
#   suppressor(agent_id: str, tool_name: str, builtin_tool_def: dict) -> bool
# Return True if the tool should be suppressed (hidden), False to keep it visible.

_builtin_suppressors: list = []


def register_builtin_suppressor(fn: Callable) -> None:
    """Register a builtin tool suppressor."""
    if fn not in _builtin_suppressors:
        _builtin_suppressors.append(fn)


def unregister_builtin_suppressor(fn: Callable) -> None:
    """Remove a previously registered builtin tool suppressor."""
    if fn in _builtin_suppressors:
        _builtin_suppressors.remove(fn)


def should_suppress_builtin(agent_id: str, tool_name: str, tool_def: dict) -> bool:
    """Return True if any suppressor votes to hide this builtin tool."""
    for suppressor in list(_builtin_suppressors):
        try:
            if suppressor(agent_id, tool_name, tool_def):
                return True
        except Exception:
            pass
    return False


# ═══════════════════════════════════════════════════════════════════
# State Handler Registry
# ═══════════════════════════════════════════════════════════════════
# Plugins (like the kanban board) register state handlers to intercept
# slash-command /state calls. The handler receives state transitions and
# can inspect or mutate the agent's workflow state.
#   handler(agent_id, session_id, agent_state, label, data) -> dict
# Return dict with at minimum: {success: bool, state: str, message: str}
# Optional fields: blocked_tools, allowed_tools, data (stored in the state slot).

_state_handlers: Dict[str, Callable] = {}  # namespace -> handler_fn


def register_state_handler(namespace: str, fn: Callable) -> None:
    """Register a state handler for a namespace. One handler per namespace."""
    _state_handlers[namespace] = fn


def unregister_state_handler(namespace: str) -> None:
    """Remove a state handler for a namespace."""
    _state_handlers.pop(namespace, None)


def _unload_plugin_state_handlers(plugin_id: str) -> None:
    """Remove all state handlers registered by a specific plugin.

    Reserved for future use when plugin hot-unload/reload is implemented.
    Identifies handlers by checking if their __module__ contains the plugin's package name.
    """
    prefix = f'plugin_pkg_{plugin_id}_'
    for ns in [ns for ns, fn in list(_state_handlers.items())
               if getattr(fn, '__module__', '').startswith(prefix)]:
        del _state_handlers[ns]


def dispatch_state(agent_id: str, session_id: str, agent_state,
                   label: str, data) -> dict:
    """Dispatch a state transition to the handler whose namespace matches the label prefix.

    Labels use 'namespace:action' format (e.g. 'kanban:pick'). Falls back to
    checking if any handler's namespace is a prefix of the label.
    Returns an error dict if no handler claims the label.
    """
    # Try exact namespace match first (label starts with "namespace:")
    for namespace, handler in list(_state_handlers.items()):
        if label == namespace or label.startswith(namespace + ':'):
            try:
                result = handler(agent_id, session_id, agent_state, label, data)
                if result is not None:
                    result['namespace'] = namespace
                    return result
            except Exception as e:
                return {"error": f"State handler error ({namespace}): {e}",
                        "namespace": namespace}
    return {
        "error": f"No state handler registered for label '{label}'. "
        f"Registered namespaces: {list(_state_handlers.keys())}. "
        "Call `use_skill({id: \"kanban\"})` for more detail."
    }


def get_state_summary(agent_state) -> dict:
    """Return a summary of all current state slots from the agent state."""
    if agent_state is None or not hasattr(agent_state, 'states'):
        return {"states": {}}
    return {
        "states": agent_state.states,
        "registered_namespaces": list(_state_handlers.keys()),
    }

# Re-export registry lists for lifecycle cleanup (plugin unload)
def _get_all_registries():
    return (_tool_guards, _message_interceptors, _builtin_suppressors,
            _turn_context_providers, _busy_message_providers, _turn_gates,
            _tool_result_gates, _attachment_policies,
            _agent_state_summary_providers, _user_message_transformers,
            _final_response_handlers)
