"""Explore — spawn a read-only explorer sub-agent to investigate a directory.

The explorer runs independently with its own (centrally configured) model,
system prompt, and tools, confined to the target path, and reports its findings
back to the caller's session via agent messaging.
"""

import json
import posixpath
import logging
import threading
import time

_logger = logging.getLogger(__name__)

# Limits mirror agent_messaging.injected_system_vars validation.
_MAX_CONTEXT_VARS = 10
_MAX_VAR_VALUE_LEN = 1024

# How often the sync wait re-checks the caller's /stop flag.
_STOP_POLL_INTERVAL = 0.25

EXPLORER_TASK_DIRECTIVE = (
    "You are an explorer sub-agent. Your tools are confined to the target directory.\n"
    "Read your system prompt — it contains your question and rules. Your ONLY goal "
    "is to answer that question directly. Do NOT produce a general project overview. "
    "Do NOT make a plan or ask for approval — explore directly until you have an answer.\n\n"
    "--- EXPLORE ---\n"
)


def _sanitize_context_vars(raw) -> tuple:
    """Return (vars, error). Coerce to flat str→str, enforce limits."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "context_vars must be an object (key→value pairs)."
    if len(raw) > _MAX_CONTEXT_VARS:
        return None, f"context_vars may have at most {_MAX_CONTEXT_VARS} keys."
    clean = {}
    for k, v in raw.items():
        key = str(k)
        val = str(v) if v is not None else ""
        if len(val) > _MAX_VAR_VALUE_LEN:
            return None, f"context_vars['{key}'] exceeds {_MAX_VAR_VALUE_LEN} characters."
        clean[key] = val
    return clean, None


def _wait_for_explorer(done: threading.Event, timeout: int,
                       parent_session_id: str) -> str:
    """Block until the explorer finishes, the caller /stops, or timeout.

    Polls instead of a single ``done.wait(timeout)`` so a /stop on the caller's
    session — which the explorer's ``final_answer`` event never signals — is
    noticed within ``_STOP_POLL_INTERVAL`` rather than after the full timeout.

    Returns 'done', 'stopped', or 'timeout'.
    """
    from backend.agent_runtime import agent_runtime

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if done.wait(timeout=max(0.0, min(_STOP_POLL_INTERVAL, remaining))):
            return 'done'
        if parent_session_id and agent_runtime.is_stop_requested(parent_session_id):
            return 'stopped'
        if time.monotonic() >= deadline:
            return 'timeout'


def execute(agent: dict, args: dict) -> dict:
    from models.db import db
    from backend.subagent_manager import subagent_manager
    from backend.skills_manager import skills_manager
    from backend.agent_runtime import explorer
    from backend.agent_runtime.notifier import notify_agent
    from backend.agent_report_to import resolve_report_to_for_subagent_spawn
    from backend.tools.lib.exec_backend import registry

    parent_id = agent.get('id', '')
    if not parent_id:
        return {'error': 'Cannot determine the calling agent ID from context.'}

    # No nested exploration: explorers and sub-agents cannot spawn explorers.
    if agent.get('is_explorer') or agent.get('is_subagent'):
        return {'error': 'Sub-agents and explorers cannot spawn explorers.'}

    raw_path = (args.get('path') or '').strip()
    if not raw_path:
        return {'error': 'A "path" is required. Use Explore({path: "/abs/dir", ...}).'}

    # Resolve like the other file tools: the sandbox alias '/workspace' and
    # relative paths map to the caller's workspace; absolute host paths pass
    # through unchanged (exploring outside the workspace is the whole point).
    caller_ws = agent.get('workspace') or ''
    if raw_path == '/workspace' or raw_path.startswith('/workspace/'):
        path = posixpath.join(caller_ws, raw_path[len('/workspace'):].lstrip('/'))
    elif caller_ws and not posixpath.isabs(raw_path):
        path = posixpath.join(caller_ws, raw_path)
    else:
        path = raw_path
    host_path = posixpath.abspath(path)
    backend = registry.get_backend(agent.get('session_id') or 'default', agent)
    path = backend.resolve_path(host_path)
    check = backend.run_python(
        f'import json, os; p={path!r}; print(json.dumps({{"path": os.path.realpath(p), "is_dir": os.path.isdir(p)}}))',
        30, {},
    )
    if check.get('error') or check.get('exit_code', 0) != 0:
        return {'error': f"cannot validate path in execution workplace: {check.get('error') or check.get('stderr', 'unknown error')}"}
    try:
        path_info = json.loads(check.get('stdout', '').strip())
    except json.JSONDecodeError:
        return {'error': 'cannot validate path in execution workplace: invalid backend response'}
    path = path_info.get('path', path)
    if not path_info.get('is_dir'):
        suffix = f' (resolved to: {path})' if path != raw_path else ''
        return {'error': f'path is not an existing directory: {raw_path}{suffix}'}

    context_vars, cv_err = _sanitize_context_vars(args.get('context_vars'))
    if cv_err:
        return {'error': cv_err}

    # Top-level query parameter (mandatory, injected via {{query}} placeholder)
    query_arg = (args.get('query') or '').strip()
    if not query_arg:
        return {'error': 'A "query" is required. Use Explore({path: "/abs/dir", query: "your question"}).'}

    # Build injected system vars: query is required, context_vars provides extras
    injected_vars = dict(context_vars)
    injected_vars['query'] = query_arg

    # Explorers run with the DirExplorer worker skill's read-only tools.
    if not explorer.worker_skill_enabled():
        return {'error': (
            f"The '{explorer.WORKER_SKILL_ID}' (DirExplorer) skill must be enabled — "
            f"explorer sub-agents use its Grep/Read/Glob tools to do the work."
        )}

    parent_agent = db.get_agent(parent_id)
    if not parent_agent:
        return {'error': f'Calling agent "{parent_id}" not found in DB.'}

    # Resolve config + tool set from the skill settings.
    skill_cfg = skills_manager.get_skill_config(explorer.SKILL_ID)
    explorer_tool_ids, tool_err = explorer.resolve_tool_ids(skill_cfg.get('tool_ids', ''))
    if tool_err:
        return {'error': tool_err}

    def _build(explorer_id: str) -> dict:
        config = explorer.build_config(
            parent_agent, explorer_id, host_path, skill_cfg, explorer_tool_ids,
        )
        if agent.get('sandbox_enabled', 1) and not agent.get('workplace_id'):
            config['_sandbox_parent_session_id'] = agent.get('session_id') or 'default'
            config['_sandbox_parent_workspace'] = caller_ws or None
        return config

    try:
        explorer_id = subagent_manager.spawn_explorer(parent_agent, _build)
    except ValueError as e:
        return {'error': str(e)}

    parent_name = parent_agent.get('name', parent_id)
    report_to_id, report_to_channel_id, _ = resolve_report_to_for_subagent_spawn(
        parent_id,
        agent.get('user_id', ''),
        agent.get('channel_id', '') or '',
    )

    sync = bool(skill_cfg.get('sync', False))

    metadata = {
        'agent_message': True,
        'from_agent_id': parent_id,
        'from_agent_name': parent_name,
        'agent_message_depth': 1,
        'subagent_spawn': True,
        'injected_system_vars': injected_vars,
        'report_to_id': report_to_id,
        'report_to_channel_id': report_to_channel_id,
    }
    if sync:
        metadata['skip_auto_forward'] = True

    result = notify_agent(
        agent_id=explorer_id,
        tag=f"AGENT/{parent_name}",
        message=f"{EXPLORER_TASK_DIRECTIVE}Target directory: {path}\nQuestion: {query_arg}",
        external_user_id=f"__agent__{parent_id}",
        channel_id=None,
        dedup=False,
        trigger_llm=True,
        metadata=metadata,
    )

    session_id = result.get('session_id')

    _logger.info(
        "Explorer %s spawned by %s for path=%s (notify_result=%s)",
        explorer_id, parent_id, path, result,
    )

    # --- Sync mode: block until the explorer finishes and return findings directly ---
    if sync:
        if not result.get('success'):
            return {
                'error': f"Failed to dispatch explorer task: {result.get('reason', 'unknown')}",
                'explorer_id': explorer_id,
                'path': path,
            }
        if not session_id:
            return {
                'error': 'Explorer dispatched but no session allocated. Cannot track completion.',
                'explorer_id': explorer_id,
                'path': path,
            }

        timeout = int(skill_cfg.get('timeout', 300))
        done = threading.Event()
        answer_data = {}

        from backend.event_stream import event_stream
        from backend.agent_runtime.concurrency import paused_model_gate

        def _on_explorer_done(data):
            if data.get('agent_id') == explorer_id:
                answer_data['answer'] = data.get('answer', '')
                answer_data['tool_trace'] = data.get('tool_trace', [])
                answer_data['error'] = data.get('error', False)
                done.set()

        event_stream.on('final_answer', _on_explorer_done)

        try:
            # Release our turn's model-gate while blocked so the explorer (which needs
            # the same gate) can run — otherwise parent↔explorer deadlock until timeout.
            with paused_model_gate():
                outcome = _wait_for_explorer(
                    done, timeout, agent.get('session_id') or '')

            if outcome == 'stopped':
                # Propagate the caller's /stop into the explorer's OWN session:
                # sets its stop event and kills its running tool subprocess, so it
                # halts instead of exploring on for a caller that is already gone.
                from backend.agent_runtime import agent_runtime
                agent_runtime.request_stop(session_id)
                _logger.info(
                    "Explore stopped by user — propagated stop to explorer %s (session=%s)",
                    explorer_id, session_id,
                )
                return {
                    'explorer_id': explorer_id,
                    'path': path,
                    'stopped': True,
                    'error': (
                        "Exploration stopped by user request. The explorer was "
                        "stopped as well and returned no findings."
                    ),
                    'session_id': session_id,
                }

            if outcome == 'timeout':
                return {
                    'explorer_id': explorer_id,
                    'path': path,
                    'error': (
                        f"Explorer '{explorer_id}' timed out after {timeout}s. "
                        f"It will continue exploring and report back via agent messaging."
                    ),
                    'session_id': session_id,
                }

            return {
                'explorer_id': explorer_id,
                'path': path,
                'findings': answer_data.get('answer', ''),
                'tool_trace': answer_data.get('tool_trace', []),
                'session_id': session_id,
            }
        finally:
            event_stream.off('final_answer', _on_explorer_done)

    # --- Async mode (default): return immediately ---
    return {
        'explorer_id': explorer_id,
        'path': path,
        'message': (
            f"Explorer '{explorer_id}' spawned to investigate '{path}'. "
            f"It will explore independently and report its findings back to you "
            f"via agent messaging."
        ),
        'session_id': session_id,
    }
