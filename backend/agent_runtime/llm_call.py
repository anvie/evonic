"""
llm_call.py — LLM call preparation: tool classification & parallel execution primitives.

Part of the diet llm_loop.py refactor (Layout C / Pipeline).
"""

from backend.agent_runtime.llm_tool_executor import (
    intercept_simulation_outbound,
)

# ── Tool classification for parallel execution ──────────────────────────────

_READ_ONLY_TOOLS: frozenset = frozenset({
    'read_file', 'calculator', 'find', 'stats', 'tree',
    'which',
})

_ALWAYS_SERIAL_TOOLS: frozenset = frozenset({
    'use_skill', 'unload_skill', 'write_file', 'patch',
    'str_replace', 'runpy', 'bash', 'remember', 'recall',
    'send_notification', 'clear_log_file',
})

_MAX_PARALLEL_TOOL_WORKERS = 6

# ── Tool execution core ──────────────────────────────────────────────────────

def _execute_tool_core(fn_name: str, args: dict,
                       builtin_exec, real_exec,
                       agent_context: dict | None = None) -> dict:
    """Execute a single tool call — pure execution, no side-effects.

    This is the parallelisable core. Guard checks, approval handling,
    use_skill/unload_skill injections, DB writes, event emits — all of
    those remain in the serial post-processing phase.

    ``agent_context`` carries the simulation activation: when it (or the
    :mod:`backend.tools.lib.simulation_scope` contextvar) marks a simulation
    run, the outbound side-effect interceptor short-circuits durable /
    externally-visible tools to a synthetic success instead of touching live
    state.  Normal agents pass no simulation marker, so this is a no-op.
    """
    sim_result = intercept_simulation_outbound(fn_name, args, agent_context)
    if sim_result is not None:
        return sim_result
    try:
        result = builtin_exec(fn_name, args)
        if result is None:
            result = real_exec(fn_name, args)
        return result
    except Exception as e:
        return {'error': f'Tool execution error: {e}'}
