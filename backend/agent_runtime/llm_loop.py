"""
llm_loop.py — the LLM tool-call execution loop (orchestrator).

Handles: LLM calls, response parsing (per-model tool-specific), tool dispatch,
skill injection/removal, loop detection, stop signals, timeout retries.

Split from the original monolith (Layout C / Pipeline):
- llm_call.py             — tool classification & parallel execution primitives
- llm_response_parser.py  — error humanisation, nudge patterns, context compaction
- llm_tool_executor.py    — injection cap constants
"""

import collections
import difflib
import json
import logging
import queue
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Any, List, Optional

_logger = logging.getLogger(__name__)

# ── Import from split modules ───────────────────────────────────────────────

from backend.agent_runtime.llm_call import (
    _READ_ONLY_TOOLS, _ALWAYS_SERIAL_TOOLS, _MAX_PARALLEL_TOOL_WORKERS,
    _execute_tool_core,
)
from backend.agent_runtime.llm_response_parser import (
    _humanize_llm_error, _emergency_compact_messages,
    _CONTINUATION_PATTERNS, CONTINUATION_RE,
    _PLANNING_PATTERNS, PLANNING_RE,
    CONTINUATION_NUDGE, MAX_CONTINUATION_NUDGES,
)
from backend.agent_runtime.llm_tool_executor import MAX_INJECTIONS_PER_LOOP

from models.db import db
from backend.llm_client import llm_client, strip_thinking_tags, LLMClient, _split_trailing_think_close
def _persist_agent_state_split(ms, agent_id, session_id, db_agent_id=None):
    """Persist agent state, splitting per-session vs global fields.

    - focus/focus_reason are GLOBAL  -> upsert_agent_state(__agent__)
    - mode/tasks/plan_file/states/auto_trivial are PER-SESSION -> upsert_session_state(session_id)
    """
    raw = ms.serialize()
    data = json.loads(raw)

    # Global: focus/focus_reason
    global_data = {
        'focus': data.get('focus', False),
        'focus_reason': data.get('focus_reason'),
    }
    db.upsert_agent_state(json.dumps(global_data), agent_id=agent_id)

    # Per-session: everything except focus/focus_reason
    session_data = {
        'mode': data.get('mode', 'plan'),
        'tasks': data.get('tasks', []),
        'next_task_id': data.get('next_task_id', 1),
        'plan_file': data.get('plan_file'),
        'states': data.get('states', {}),
        'auto_trivial': data.get('auto_trivial', False),
    }
    db.upsert_session_state(session_id, json.dumps(session_data), agent_id=agent_id)
from backend.tools import tool_registry
from config import (AGENT_MAX_TOOL_ITERATIONS as MAX_TOOL_ITERATIONS,
                    AGENT_MAX_TOOL_RESULT_CHARS as MAX_TOOL_RESULT_CHARS,
                    AGENT_TIMEOUT_RETRIES as MAX_TIMEOUT_RETRIES)


def run_tool_loop(agent: Dict[str, Any],
                  agent_context: dict,
                  messages: List[dict],
                  tools: List[dict],
                  session_id: str,
                  llm_lock: threading.Lock,
                  stop_event: threading.Event,
                  session_skill_mds: dict,
                  session_skill_tools: dict,
                  llm_log_path: str,
                  inject_queue=None,
                  session_db_agent_id: str = None) -> tuple:
    """Call LLM in a loop, executing tool calls until a final text response.

    Returns (response_text, tool_trace, timeline) where:
    - tool_trace: list of {"tool": name, "args": {...}, "result": {...}} for animated bubbles
    - timeline: chronological list of events for the thinking panel:
        {"type": "thinking", "content": "..."}
        {"type": "tool_call", "tool": "...", "args": {...}}
        {"type": "tool_result", "tool": "...", "result": {...}, "error": bool}
        {"type": "response", "content": "..."}  (intermediate text before tool calls)

    session_skill_mds / session_skill_tools are the runtime's instance dicts (mutated in-place).
    """
    from backend.event_stream import event_stream
    from models.chatlog import chatlog_manager

    agent_id = agent['id']
    db_agent_id = session_db_agent_id or agent_id  # which per-agent DB owns this session
    external_user_id = agent_context.get('user_id')
    channel_id = agent_context.get('channel_id')

    chatlog = chatlog_manager.get(db_agent_id, session_id)
    _loop_ts = int(time.time() * 1000)
    chatlog.append({'type': 'turn_begin', 'session_id': session_id, 'ts': _loop_ts})
    event_stream.emit('turn_begin', {'session_id': session_id, 'ts': _loop_ts})

    builtin_exec = tool_registry.get_builtin_executor(agent_context)
    real_exec = tool_registry.get_real_executor(agent_context)

    # Chain-of-responsibility: collect executors in order, iterate until one returns non-None
    _builtin_chain = [builtin_exec]
    if agent_context.get('is_super'):
        from backend.tools.super_agent_tools import get_super_agent_executor
        _builtin_chain.append(get_super_agent_executor(agent_context))
    if agent_context.get('is_super') or agent_context.get('agent_messaging_enabled'):
        from backend.tools.agent_messaging import get_agent_messaging_executor
        _builtin_chain.append(get_agent_messaging_executor(agent_context))

    def builtin_exec(fn_name, args):
        for _exec in _builtin_chain:
            result = _exec(fn_name, args)
            if result is not None:
                return result
        return None

    tool_trace = []
    timeline = []
    _loop_start_time = time.time()
    _last_intermediate_text = None   # dedup tracker for intermediate channel sends
    _intermediate_dup_count = 0      # consecutive duplicate counter
    _force_stop_injected = False     # True after first force-stop injection
    # Sliding-window tool+args loop detector (window=10, threshold=5)
    _tool_call_window: collections.deque = collections.deque(maxlen=10)
    _tool_args_force_stop_injected: bool = False
    # Post-force-stop hard-termination counter
    _any_force_stop_injected: bool = False
    _post_force_stop_tool_count: int = 0
    # Continuation-nudge tracker
    _continuation_nudge_count: int = 0

    # Restore persisted skill state for this session (survives across turns until unload or clear)
    _skill_system_mds: dict = dict(session_skill_mds.get(session_id, {}))
    _loaded_lazy_skills: dict = {
        sk_id: [td.get('function', {}).get('name', '') for td in tds]
        for sk_id, tds in session_skill_tools.get(session_id, {}).items()
    }
    # Re-inject persisted skill tools into this turn's tool list
    _existing_fns = {t.get('function', {}).get('name', '') for t in tools}
    for _sk_tds in session_skill_tools.get(session_id, {}).values():
        for td in _sk_tds:
            fn = td.get('function', {}).get('name', '')
            if fn and fn not in _existing_fns:
                tools.append(td)
                _existing_fns.add(fn)

    # Add restored skill tool IDs to assigned_tool_ids for authorization guard
    _assigned = agent_context.get('assigned_tool_ids')
    if _assigned is not None:
        for sk_id, fns in _loaded_lazy_skills.items():
            for fn in fns:
                if fn:
                    _tid = f'skill:{sk_id}:{fn}'
                    if _tid not in _assigned:
                        _assigned.append(_tid)

    # Resolve agent's default model for LLM calls
    agent_model_config = None
    try:
        model = db.get_agent_default_model(agent_id)
        if model:
            agent_model_config = {
                'base_url': model.get('base_url'),
                'api_key': model.get('api_key'),
                'model_name': model.get('model_name'),
                'timeout': model.get('timeout', 60),
                'thinking': bool(model.get('thinking', False)),
                'thinking_budget': model.get('thinking_budget', 0),
                'max_tokens': model.get('max_tokens', 32768),
                'temperature': model.get('temperature'),
                'vision_supported': bool(model.get('vision_supported', False)),
            }
            _logger.info("%s using model: %s (%s)", agent_id, model.get('name'), model.get('model_name'))
        else:
            _logger.info("No model configured for agent %s, using config.py defaults", agent_id)
    except Exception as e:
        _logger.warning("Failed to resolve model for agent %s: %s", agent_id, e)

    # Fallback: agent.model is a raw model-name string override (from agent General Settings).
    # Use it with the global endpoint when no llm_models entry is configured.
    if not agent_model_config and agent.get('model'):
        import config as _config
        try:
            from models.db import db as _db
            _dm = _db.get_default_model()
            _base_url = _dm.get('base_url') if _dm else None
            _api_key = _dm.get('api_key') if _dm else None
            _timeout = _dm.get('timeout') if _dm else None
        except Exception:
            _base_url = None
            _api_key = None
            _timeout = None
        agent_model_config = {
            'base_url': _base_url,
            'api_key': _api_key,
            'model_name': agent['model'],
            'timeout': _timeout,
            'thinking': False,
            'thinking_budget': 0,
        }
        _logger.info("Using agent model string override: %s", agent['model'])

    # Create LLMClient with resolved model config
    llm = LLMClient(model_config=agent_model_config) if agent_model_config else llm_client

    # If the model doesn't support vision, replace image content with a text instruction
    # so the LLM can inform the user in their own language.
    _vision_supported = bool((agent_model_config or {}).get('vision_supported', False))
    if not _vision_supported:
        _patched = []
        for _msg in messages:
            _content = _msg.get('content')
            if _msg.get('role') == 'user' and isinstance(_content, list):
                _has_img = any(isinstance(p, dict) and p.get('type') == 'image_url' for p in _content)
                if _has_img:
                    _text_parts = [p['text'] for p in _content if isinstance(p, dict) and p.get('type') == 'text']
                    _user_text = _text_parts[0] if _text_parts else ''
                    _note = (
                        f"[System note: The user sent an image{(' with the message: ' + _user_text) if _user_text else ''}, "
                        "but this model does not support image processing. "
                        "Please inform the user politely that you cannot process images with the current model, "
                        "and respond in the same language the user is using.]"
                    )
                    _msg = {**_msg, 'content': _note}
            _patched.append(_msg)
        messages = _patched

    timeout_retries = 0
    max_timeout_retries = int(db.get_setting('agent_timeout_retries', str(MAX_TIMEOUT_RETRIES)))
    max_tool_iterations = int(db.get_setting('max_tool_iterations', str(MAX_TOOL_ITERATIONS)))
    _compaction_attempted = False

    # Build param view-type lookup: {fn_name: {param_name: view_type}}
    _param_type_map = {}
    for t in tools:
        fn = t.get('function', {})
        fn_name = fn.get('name', '')
        props = fn.get('parameters', {}).get('properties', {})
        types = {pname: pdef['view'] for pname, pdef in props.items() if 'view' in pdef}
        if types:
            _param_type_map[fn_name] = types

    # Pre-turn: run interceptors once before the first LLM call so plugins (e.g. kanban)
    # can classify the incoming user message and pre-set state (e.g. _approval_granted)
    # before the LLM attempts any tool calls.
    from backend.plugin_manager import run_message_interceptors as _pre_run_interceptors
    for _pre_inj in _pre_run_interceptors(agent_id, '', messages):
        messages.append(_pre_inj)

    _iteration = 0
    _injection_count = 0  # total injections in this loop run (capped to prevent infinite loops)
    # Track whether we've already done a thinking-enabled LLM call with tool calls.
    # Some APIs (e.g. DeepSeek-R1) require that thinking is ONLY enabled on the
    # first call; subsequent calls (after tool results) must NOT re-enable thinking,
    # otherwise the API rejects with "reasoning_content must be passed back".
    _had_tool_call_iteration = False
    while _iteration < max_tool_iterations:
        _iteration += 1
        # Drain injected user messages from mid-loop injection queue.
        # Multiple queued messages are merged into one to avoid consecutive user turns.
        if inject_queue is not None:
            injected_parts = []
            while True:
                try:
                    injected_parts.append(inject_queue.get_nowait()['content'])
                except queue.Empty:
                    break
            if injected_parts:
                merged = "\n\n".join(injected_parts)
                messages.append({"role": "user", "content": merged})
                # Reset iteration counter so injected tasks (e.g. next kanban task in
                # autopilot mode) each get a fresh budget instead of sharing the counter
                # with the previous task. Capped at MAX_INJECTIONS_PER_LOOP to prevent
                # infinite loops when injections keep arriving continuously.
                _injection_count += 1
                if _injection_count <= MAX_INJECTIONS_PER_LOOP:
                    _iteration = 0
                    # Do NOT reset _had_tool_call_iteration here. If prior iterations already
                    # used thinking + tool calls, the message list contains reasoning_content.
                    # Re-enabling thinking at this point causes DeepSeek-R1 to reject with
                    # "reasoning_content must be passed back".
                else:
                    _logger.warning("Injection cap reached (%d), iteration counter will no longer reset — loop will terminate at max_tool_iterations (%d).", MAX_INJECTIONS_PER_LOOP, max_tool_iterations)
                _logger.debug("Injected %d user message(s) into loop for session %s (injection #%d)",
                              len(injected_parts), session_id, _injection_count)
                event_stream.emit('message_injection_applied', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'content': merged, 'count': len(injected_parts),
                })
                event_stream.emit('turn_split', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                })

        # Inject / update mental state system message before each LLM call
        ms = agent_context.get('agent_state')
        if ms is not None:
            state_msg = {"role": "system", "content": ms.render()}
            state_idx = next(
                (i for i, m in enumerate(messages)
                 if m.get('role') == 'system' and '## Agent State' in m.get('content', '')),
                None
            )
            if state_idx is not None:
                messages[state_idx] = state_msg
            else:
                messages.insert(1, state_msg)

        # Inject / update persistent skill SYSTEM.md as system messages (re-injected each iteration
        # so skill instructions survive summarization in long conversations)
        for sk_id, sk_content in _skill_system_mds.items():
            marker = f'## Skill Context: {sk_id}'
            sk_msg = {"role": "system", "content": f"{marker}\n\n{sk_content}"}
            sk_idx = next(
                (i for i, m in enumerate(messages)
                 if m.get('role') == 'system' and marker in m.get('content', '')),
                None
            )
            if sk_idx is not None:
                messages[sk_idx] = sk_msg
            else:
                insert_at = 2 if agent_context.get('agent_state') is not None else 1
                messages.insert(insert_at, sk_msg)

        # LOCK ORDERING: Main path — llm_lock only. No other locks held here.
        # Disable thinking after the first tool-call iteration so the API doesn't
        # see "thinking enabled" while tool_calls + reasoning_content are already
        # in the history (causes DeepSeek-R1 "reasoning_content must be passed back").
        _enable_thinking_this_call = not _had_tool_call_iteration
        _logger.info("[LOCK] _llm_lock - WAITING (session=%s, main LLM call)", session_id)
        with llm_lock:
            _logger.info("[LOCK] _llm_lock - ACQUIRED (session=%s, main LLM call)", session_id)
            result = llm.chat_completion(
                messages=messages,
                tools=tools if tools else None,
                temperature=None,
                enable_thinking=_enable_thinking_this_call,
                max_tokens=None,
                log_file=llm_log_path
            )

        # Check A: stop signal check after LLM call (earliest safe point)
        if stop_event.is_set():
            stop_event.clear()
            _logger.info("Stop signal received for session %s — aborting loop", session_id)
            stop_msg = "Agent stopped by user request."
            _stop_dur = round(time.time() - _loop_start_time, 1)
            db.add_chat_message(session_id, 'assistant', stop_msg, agent_id=db_agent_id,
                                metadata={"timeline": timeline, "stopped": True, "thinking_duration": _stop_dur})
            chatlog.append({'type': 'final', 'session_id': session_id, 'content': stop_msg,
                            'metadata': {'stopped': True, 'thinking_duration': _stop_dur}})
            _stop_inj = ("[SYSTEM] Your previous reasoning and response were forcefully "
                         "interrupted by the user via /stop before completion. "
                         "Await the user's next instruction.")
            db.add_chat_message(session_id, 'user', _stop_inj,
                                agent_id=db_agent_id, metadata={"stop_injection": True})
            chatlog.append({'type': 'system', 'session_id': session_id, 'content': _stop_inj,
                            'metadata': {'stop_injection': True}})
            chatlog.append({'type': 'turn_end', 'session_id': session_id,
                            'thinking_duration': _stop_dur})
            event_stream.emit('final_answer', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'answer': stop_msg, 'tool_trace': tool_trace, 'timeline': timeline,
            })
            return stop_msg, tool_trace, timeline

        if not result.get('success'):
            error_type = result.get('error_type', '')

            # llama.cpp failed to parse tool call arguments as JSON. Two common causes:
            # 1. Content was truncated mid-generation (max_tokens hit) — string never closed.
            # 2. Unescaped special characters inside a string value.
            # Retrying regenerates the same broken call — inject a correction message so
            # the model reformulates: break large content into smaller chunks and/or
            # ensure all special characters are properly escaped.
            if error_type == 'tool_call_json_error' and timeout_retries < max_timeout_retries:
                timeout_retries += 1
                _logger.warning("tool_call_json_error — injecting correction message (%d/%d)", timeout_retries, max_timeout_retries)
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your previous tool call failed — the server could not parse "
                        "the tool call arguments as valid JSON. This is usually caused by one of:\n"
                        "1. The content was too large and got cut off mid-string. "
                        "If you were writing a large file, split it into smaller parts and write "
                        "them in separate calls (e.g. write the first half, then append or overwrite "
                        "with the second half).\n"
                        "2. Unescaped special characters (e.g. double quotes inside a string value "
                        "must be written as \\\" in JSON).\n"
                        "Please retry with the above in mind."
                    )
                })
                event_stream.emit('llm_retry', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'retry_count': timeout_retries, 'max_retries': max_timeout_retries,
                    'error_type': error_type,
                })
                continue

            # Auto-retry on transient provider/connection errors (no partial output to preserve)
            if error_type in ('provider_error', 'connection_error') and timeout_retries < max_timeout_retries:
                timeout_retries += 1
                wait = min(2 ** timeout_retries, 30)
                _logger.warning("%s — auto-retry %d/%d in %ds", error_type, timeout_retries, max_timeout_retries, wait)
                user_msg = f"Model is busy, retrying... ({timeout_retries}/{max_timeout_retries})"
                event_stream.emit('llm_retry', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'retry_count': timeout_retries, 'max_retries': max_timeout_retries,
                    'error_type': error_type,
                    'user_message': user_msg,
                })
                time.sleep(wait)
                continue

            # Auto-retry on timeout: LLM was likely still reasoning
            if error_type in ('request_timeout', 'generation_timeout') and timeout_retries < max_timeout_retries:
                timeout_retries += 1
                _logger.warning("%s detected — auto-retry %d/%d with continue prompt", error_type, timeout_retries, max_timeout_retries)

                # For generation_timeout, preserve partial reasoning in timeline
                partial = result.get('response', {})
                if isinstance(partial, dict):
                    choices = partial.get('choices', [{}])
                    if choices:
                        partial_msg = choices[0].get('message', {})
                        partial_reasoning = partial_msg.get('reasoning_content') or partial_msg.get('reasoning') or ''
                        partial_content = partial_msg.get('content', '')
                        if partial_reasoning:
                            timeline.append({"type": "thinking", "content": partial_reasoning})
                        if partial_content:
                            _partial_msg: Dict[str, Any] = {"role": "assistant", "content": partial_content}
                            if partial_reasoning:
                                _partial_msg["reasoning_content"] = partial_reasoning
                            messages.append(_partial_msg)

                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "user", "content": "[SYSTEM] Your previous response timed out. Please continue where you left off and provide your answer."})

                event_stream.emit('llm_retry', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'retry_count': timeout_retries, 'max_retries': max_timeout_retries,
                    'error_type': error_type,
                })
                continue

            _resp_val = result.get('response', 'Unknown error')
            if isinstance(_resp_val, dict):
                _resp_val = _resp_val.get('error') or str(_resp_val)
            error_detail = result.get('error_detail') or str(_resp_val)
            _logger.error("LLM error [%s]: %s", result.get('error_type', 'unknown'), error_detail)

            # Auto-compact on context size exceeded (one attempt only)
            _err_lower = error_detail.lower()
            _is_context_exceeded = (
                'context length' in _err_lower or 'context size' in _err_lower
                or 'exceed_context' in _err_lower or 'exceeds the available context' in _err_lower
                or 'too long' in _err_lower
            )
            if _is_context_exceeded and not _compaction_attempted:
                _compaction_attempted = True
                _logger.warning("Context size exceeded for session %s — attempting emergency compaction", session_id)
                event_stream.emit('llm_retry', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'retry_count': 0, 'max_retries': 1,
                    'error_type': 'context_compaction',
                    'user_message': 'Conversation is too long, automatically compacting...',
                })
                _compacted = _emergency_compact_messages(
                    messages=messages,
                    llm=llm,
                    llm_lock=llm_lock,
                    session_id=session_id,
                    agent_id=agent_id,
                )
                if _compacted is not None:
                    messages[:] = _compacted
                    event_stream.emit('llm_retry', {
                        'agent_id': agent_id, 'session_id': session_id,
                        'external_user_id': external_user_id, 'channel_id': channel_id,
                        'retry_count': 1, 'max_retries': 1,
                        'error_type': 'context_compaction',
                        'user_message': 'Summary complete, resuming...',
                    })
                    continue
                # Compaction failed — fall through to error

            # ── Per-agent model fallback ──────────────────────────────────
            # After all retries to the primary model fail, attempt the
            # agent's configured fallback model (if any) before giving up.
            _fallback_succeeded = False
            _fallback_model = db.get_agent_fallback_model(agent_id)
            if _fallback_model:
                _logger.warning(
                    "Primary model failed [%s] for agent %s — attempting fallback model %s (%s)",
                    error_type, agent_id, _fallback_model.get('name'), _fallback_model.get('model_name'))
                event_stream.emit('llm_fallback', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'primary_error': error_type,
                    'fallback_model': _fallback_model.get('name'),
                })
                try:
                    _fallback_config = {
                        'base_url': _fallback_model.get('base_url'),
                        'api_key': _fallback_model.get('api_key'),
                        'model_name': _fallback_model.get('model_name'),
                        'timeout': _fallback_model.get('timeout', 60),
                        'thinking': bool(_fallback_model.get('thinking', False)),
                        'thinking_budget': _fallback_model.get('thinking_budget', 0),
                        'max_tokens': _fallback_model.get('max_tokens', 32768),
                        'temperature': _fallback_model.get('temperature'),
                    }
                    _fallback_llm = LLMClient(model_config=_fallback_config)
                    with llm_lock:
                        _fallback_result = _fallback_llm.chat_completion(
                            messages=messages,
                            tools=tools if tools else None,
                            temperature=None,
                            enable_thinking=_enable_thinking_this_call,
                            max_tokens=None,
                            log_file=llm_log_path
                        )
                    if _fallback_result.get('success'):
                        _logger.info(
                            "Fallback model %s succeeded for agent %s — using for remaining iterations",
                            _fallback_model.get('model_name'), agent_id)
                        event_stream.emit('llm_fallback_succeeded', {
                            'agent_id': agent_id, 'session_id': session_id,
                            'external_user_id': external_user_id, 'channel_id': channel_id,
                            'fallback_model': _fallback_model.get('name'),
                        })
                        llm = _fallback_llm
                        result = _fallback_result
                        _fallback_succeeded = True
                    else:
                        _fb_err = _fallback_result.get('error_type', 'unknown')
                        _logger.error(
                            "Fallback model %s also failed for agent %s [%s]: %s",
                            _fallback_model.get('model_name'), agent_id, _fb_err,
                            _fallback_result.get('error_detail', ''))
                        event_stream.emit('llm_fallback_failed', {
                            'agent_id': agent_id, 'session_id': session_id,
                            'external_user_id': external_user_id, 'channel_id': channel_id,
                            'fallback_model': _fallback_model.get('name'),
                            'fallback_error': _fb_err,
                        })
                except Exception as _fe:
                    _logger.error(
                        "Fallback model exception for agent %s: %s", agent_id, _fe)

            if not _fallback_succeeded:
                error_msg = _humanize_llm_error(error_detail)
                _err_dur = round(time.time() - _loop_start_time, 1)
                db.add_chat_message(session_id, 'assistant', error_msg, agent_id=db_agent_id,
                                    metadata={"error": True, "timeline": timeline, "thinking_duration": _err_dur})
                chatlog.append({'type': 'error', 'session_id': session_id, 'content': error_msg,
                                'metadata': {'error': True, 'thinking_duration': _err_dur}})
                chatlog.append({'type': 'turn_end', 'session_id': session_id, 'thinking_duration': _err_dur})
                return {"text": error_msg, "error": True}, tool_trace, timeline

        choice = result['response'].get('choices', [{}])[0]
        msg = choice.get('message', {})
        raw_content = msg.get('content', '')
        reasoning_content = msg.get('reasoning_content') or msg.get('reasoning')
        tool_calls = msg.get('tool_calls')

        # Fallback: parse Qwen's native <tool_call> XML format when the model
        # doesn't return structured tool_calls in the OpenAI response field.
        if not tool_calls and raw_content and '<tool_call>' in raw_content:
            from evaluator.qwen_parser import extract_qwen_tool_calls, qwen_tool_calls_to_openai_format, strip_qwen_tool_calls
            qwen_calls = extract_qwen_tool_calls(raw_content)
            if qwen_calls:
                tool_calls = qwen_tool_calls_to_openai_format(qwen_calls)
                raw_content = strip_qwen_tool_calls(raw_content)

        # DEBUG: log raw thinking fields for diagnosis
        _logger.debug("reasoning_content type=%s repr=%s raw_content[:200]=%s",
                      type(reasoning_content).__name__,
                      repr(reasoning_content)[:200] if reasoning_content else 'None',
                      repr(raw_content)[:200])

        # Extract thinking from reasoning_content field or content tags
        thinking = None
        reasoning_text = (reasoning_content or '').strip()
        embedded_final_in_reasoning = None
        if reasoning_text and '</think>' in reasoning_text:
            # Some backends accidentally include </think> + final response inside
            # reasoning_content. Strip the tag and recover the trailing text.
            reasoning_text, embedded_final_in_reasoning = _split_trailing_think_close(reasoning_text)
        if reasoning_text:
            timeline.append({"type": "thinking", "content": reasoning_text})
            chatlog.append({'type': 'thinking', 'session_id': session_id, 'content': reasoning_text})
            # Still strip any thinking tags from content (some models put it in both)
            content, _ = strip_thinking_tags(raw_content) if raw_content else ('', None)
            # Recover final response embedded after </think> in reasoning_content
            if not content and embedded_final_in_reasoning:
                content = embedded_final_in_reasoning
            event_stream.emit('llm_thinking', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'thinking': reasoning_text,
            })
        elif raw_content:
            content, thinking = strip_thinking_tags(raw_content)
            if thinking:
                timeline.append({"type": "thinking", "content": thinking})
                chatlog.append({'type': 'thinking', 'session_id': session_id, 'content': thinking})
                event_stream.emit('llm_thinking', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'thinking': thinking,
                })
        else:
            content = ''

        # Fallback: recover tool calls from thinking/CoT content.
        # Covers the case where the model emits <tool_call> XML inside <think> tags
        # or in the separate reasoning_content field (llama.cpp --reasoning mode)
        # instead of in the main response body.
        if not tool_calls:
            cot_text = reasoning_text or thinking
            if cot_text and '<tool_call>' in cot_text:
                from evaluator.qwen_parser import extract_qwen_tool_calls, qwen_tool_calls_to_openai_format
                cot_calls = extract_qwen_tool_calls(cot_text)
                if cot_calls:
                    tool_calls = qwen_tool_calls_to_openai_format(cot_calls)
                    _logger.debug("Recovered %d tool call(s) from thinking/CoT content", len(tool_calls))

        if content:
            is_final = not bool(tool_calls)
            event_stream.emit('llm_response_chunk', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'content': content, 'is_final': is_final,
                # Signal frontend to also render a standalone bubble for intermediate
                # responses when send_intermediate_responses is enabled on the agent.
                'send_as_message': is_final or bool(agent.get('send_intermediate_responses')),
            })

        if not tool_calls:
            # Treat trivial single-character/punctuation-only responses (e.g. ">", "<")
            # as empty — these are artefacts from confused models, not real output.
            _TRIVIAL_RE = re.compile(r'^[\s>|#\-\.\\/<>!]+$')
            if content and _TRIVIAL_RE.match(content.strip()):
                _logger.debug("Trivial response %r — treating as empty", content.strip())
                content = ''

            # If content is empty, inject a follow-up to get a proper response.
            # This handles models (e.g. Qwen3) that sometimes swallow the response
            # inside <think> tags, leaving content blank.
            # Allow up to 2 injections to recover from a bad first follow-up reply.
            _FOLLOWUP_SENTINEL = '[SYSTEM] Please continue and give your response.'
            if not content:
                inject_count = sum(
                    1 for m in messages
                    if m.get('role') == 'user' and m.get('content') == _FOLLOWUP_SENTINEL
                )
                _logger.warning("Empty response detected (reasoning=%s, tool_calls=none, inject_count=%d)",
                               'present' if reasoning_content else 'none', inject_count)
                if inject_count < 2:
                    _logger.warning("Response recovery rule (%d/2) — injecting follow-up sentinel", inject_count + 1)
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content": _FOLLOWUP_SENTINEL})
                    continue
                _logger.warning("Max recovery attempts reached — returning empty response")

            # Detect continuation phrases: LLM said it will continue but produced no tool calls.
            # Nudge it to keep going; nudge is NOT saved to DB/history.
            elif content and CONTINUATION_RE.search(content) and _continuation_nudge_count < MAX_CONTINUATION_NUDGES:
                if PLANNING_RE.search(content):
                    _logger.debug("Nudge negated by PLANNING_RE — treating as final")
                    break
                _continuation_nudge_count += 1
                _logger.debug("Continuation phrase detected — nudging LLM (%d/%d)",
                              _continuation_nudge_count, MAX_CONTINUATION_NUDGES)
                db.add_chat_message(session_id, 'assistant', content, agent_id=db_agent_id)
                chatlog.append({'type': 'intermediate', 'session_id': session_id, 'content': content})
                _asst_nudge_msg: Dict[str, Any] = {"role": "assistant", "content": content}
                if reasoning_text:
                    _asst_nudge_msg["reasoning_content"] = reasoning_text
                messages.append(_asst_nudge_msg)
                # Nudge injected internally only — not persisted to DB
                messages.append({"role": "user", "content": CONTINUATION_NUDGE})
                continue

            # If LLM responded with only [DONE], suppress it and treat as finished.
            elif content and content.strip() == "[DONE]":
                content = ""

            # Normalize "[No response needed]" variants to empty to suppress sending
            if content and content.strip().lower().startswith("[no response"):
                content = ""

            # Run interceptors before committing the final answer.
            # Plugins (e.g. kanban) can inspect the content and inject a
            # follow-up instruction that forces the LLM back into the loop.
            from backend.plugin_manager import run_message_interceptors
            pre_final_injections = run_message_interceptors(agent_id, content, messages)
            if pre_final_injections:
                # Save this response as an intermediate assistant message so the
                # LLM sees it as context, then append the injected instructions.
                db.add_chat_message(session_id, 'assistant', content, agent_id=db_agent_id)
                chatlog.append({'type': 'intermediate', 'session_id': session_id, 'content': content})
                _asst_inj_msg: Dict[str, Any] = {"role": "assistant", "content": content}
                if reasoning_text:
                    _asst_inj_msg["reasoning_content"] = reasoning_text
                messages.append(_asst_inj_msg)
                for inj in pre_final_injections:
                    messages.append(inj)
                continue  # re-enter loop so LLM can act on the injected reminder

            # Final response — save with timeline metadata
            meta = {"timeline": timeline} if timeline else None
            if meta:
                meta['thinking_duration'] = round(time.time() - _loop_start_time, 1)
            if meta and agent.get('send_intermediate_responses'):
                meta['send_intermediate_responses'] = True
            _final_dur = round(time.time() - _loop_start_time, 1)
            if content:
                db.add_chat_message(session_id, 'assistant', content, agent_id=db_agent_id, metadata=meta)
                _cl_meta = {'thinking_duration': _final_dur}
                if agent.get('send_intermediate_responses'):
                    _cl_meta['send_intermediate_responses'] = True
                chatlog.append({'type': 'final', 'session_id': session_id, 'content': content,
                                'metadata': _cl_meta})
            chatlog.append({'type': 'turn_end', 'session_id': session_id, 'thinking_duration': _final_dur})
            # Persist mental state for next turn
            ms = agent_context.get('agent_state')
            if ms is not None:
                _persist_agent_state_split(ms, agent_id, session_id, db_agent_id)
            final = content or "(No response)"
            event_stream.emit('final_answer', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'answer': final, 'tool_trace': tool_trace, 'timeline': timeline,
            })
            return final, tool_trace, timeline

        # Record intermediate response text (before tool calls)
        if content:
            timeline.append({"type": "response", "content": content})

            # Loop safety: always track intermediate text duplicates (ungated).
            # Uses fuzzy match so slight wording variations still count as the same response.
            def _normalize(s):
                return re.sub(r'[^\w\s]', '', s.lower()).strip()
            _is_dup_text = (
                _last_intermediate_text is not None and
                difflib.SequenceMatcher(None, _normalize(content), _normalize(_last_intermediate_text)).ratio() >= 0.7
            )
            if _is_dup_text:
                _intermediate_dup_count += 1
                if _any_force_stop_injected:
                    # Already injected force-stop but LLM is still looping — hard stop
                    _logger.error("LLM still looping after force-stop injection — terminating loop")
                    _dup_dur = round(time.time() - _loop_start_time, 1)
                    meta = {"timeline": timeline, "thinking_duration": _dup_dur}
                    db.add_chat_message(session_id, 'assistant', content, agent_id=db_agent_id, metadata=meta)
                    chatlog.append({'type': 'error', 'session_id': session_id,
                                    'content': content or '(No response)',
                                    'metadata': {'thinking_duration': _dup_dur, 'loop_terminated': True}})
                    chatlog.append({'type': 'turn_end', 'session_id': session_id, 'thinking_duration': _dup_dur})
                    # Emit final_answer so auto-forward (e.g. sub-agent → parent) still fires
                    # on this hard-stop exit path. Without this, sub-agent replies are silently lost.
                    _final_loop_term = content or "(No response)"
                    event_stream.emit('final_answer', {
                        'agent_id': agent_id, 'session_id': session_id,
                        'external_user_id': external_user_id, 'channel_id': channel_id,
                        'answer': _final_loop_term, 'tool_trace': tool_trace, 'timeline': timeline,
                        'loop_terminated': True,
                    })
                    return _final_loop_term, tool_trace, timeline
            else:
                _last_intermediate_text = content
                _intermediate_dup_count = 0

            # Optionally forward to channel (e.g. Telegram) if agent setting is on
            if agent.get('send_intermediate_responses') and channel_id and not _is_dup_text:
                from backend.channels.registry import channel_manager
                _inst = channel_manager._active.get(channel_id)
                if _inst and _inst.is_running:
                    try:
                        _inst.send_message_buffered(external_user_id, content)
                    except Exception as _e:
                        _logger.warning("Intermediate send error: %s", _e)

        # Sanitize tool_calls before storing in conversation history.
        # If any arguments string is too large or invalid JSON, replace it with a
        # stub — otherwise llama.cpp will choke on it when we send the history back.
        _MAX_ARGS_CHARS = MAX_TOOL_RESULT_CHARS  # reuse same ceiling as tool results
        sanitized_tool_calls = []
        for _tc in tool_calls:
            _raw_args = _tc.get('function', {}).get('arguments', '')
            _tc_copy = json.loads(json.dumps(_tc))  # deep copy via JSON round-trip
            if len(_raw_args) > _MAX_ARGS_CHARS:
                _tc_copy['function']['arguments'] = json.dumps(
                    {'__truncated__': True, 'original_length': len(_raw_args),
                     'note': 'Arguments were too large and have been omitted from history.'}
                )
            sanitized_tool_calls.append(_tc_copy)

        # Save the assistant message with tool calls
        db.add_chat_message(session_id, 'assistant', content, tool_calls=tool_calls, agent_id=db_agent_id)
        # Write intermediate content + individual tool_call entries to chatlog
        if content:
            chatlog.append({'type': 'intermediate', 'session_id': session_id, 'content': content})
        for _tc in tool_calls:
            _fn = _tc.get('function', {})
            try:
                _tc_args = json.loads(_fn.get('arguments', '{}'))
            except (json.JSONDecodeError, TypeError):
                _tc_args = {}
            chatlog.append({'type': 'tool_call', 'session_id': session_id,
                            'function': _fn.get('name', ''), 'params': _tc_args,
                            'id': _tc.get('id', '')})
        _asst_tc_msg: Dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": sanitized_tool_calls}
        if reasoning_text:
            _asst_tc_msg["reasoning_content"] = reasoning_text
        messages.append(_asst_tc_msg)
        # Mark that we've done a tool-call iteration so subsequent LLM calls
        # don't re-enable thinking (some APIs reject thinking + existing tool history).
        _had_tool_call_iteration = True

        # ── Hybrid tool execution: parallel for read-only, serial for writes ──
        # Phase 1: Parse all tool call arguments and emit 'tool_call_started'.
        _parse_failed = {}
        _tool_records = []       # [(tc, fn_name, args, pt)]
        _parallel_indices = set()

        for tc_idx, tc in enumerate(tool_calls):
            fn_name = tc['function']['name']
            raw_args_str = tc['function'].get('arguments', '')
            try:
                args = json.loads(raw_args_str)
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "Failed to parse tool call arguments for '%s' (len=%d) "
                    "— arguments may have been truncated by max_tokens",
                    fn_name, len(raw_args_str))
                _parse_failed[tc_idx] = json.dumps({
                    'error': (
                        f"Tool call arguments for '{fn_name}' could not be parsed — "
                        "the generated JSON was likely truncated because the content "
                        "was too large. Please retry using smaller chunks (e.g. use "
                        "str_replace for targeted edits instead of rewriting the "
                        "entire file with write_file)."
                    )
                })
                _tool_records.append((tc, fn_name, None, {}))
                continue

            pt = _param_type_map.get(fn_name, {})
            timeline.append({"type": "tool_call", "tool": fn_name, "args": args, "param_types": pt})
            event_stream.emit('tool_call_started', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'tool_name': fn_name, 'tool_args': args, 'param_types': pt,
            })
            _tool_records.append((tc, fn_name, args, pt))

            if fn_name in _READ_ONLY_TOOLS and fn_name not in _ALWAYS_SERIAL_TOOLS:
                _parallel_indices.add(tc_idx)

        # Phase 2: Submit parallel batch for all read-only tools (if enabled).
        _parallel_futures = {}  # tc_idx -> Future or guard-rejection dict
        _pool = None
        if _parallel_indices and not agent_context.get('disable_parallel_tool_execution', 0):
            from backend.plugin_manager import check_tool_guards as _guard_p2
            _pool = ThreadPoolExecutor(
                max_workers=min(len(_parallel_indices), _MAX_PARALLEL_TOOL_WORKERS),
                thread_name_prefix='tool-parallel')
            for p_idx in _parallel_indices:
                _tc_p, _fn_p, _args_p, _pt_p = _tool_records[p_idx]
                _gr = _guard_p2(agent_id, _fn_p, _args_p)
                if _gr:
                    _parallel_futures[p_idx] = {
                        'error': _gr.get('error', 'Blocked by plugin guard'),
                        'blocked_by': 'tool_guard'}
                else:
                    _parallel_futures[p_idx] = _pool.submit(
                        _execute_tool_core, _fn_p, _args_p,
                        builtin_exec, real_exec)

        # Phase 3: Process each tool in original order.
        for i, (_tc, fn_name, args, _pt) in enumerate(_tool_records):
            # --- Parse-failure fast path ---
            if i in _parse_failed:
                result_str = _parse_failed[i]
                db.add_chat_message(session_id, 'tool', result_str,
                                    tool_call_id=_tc['id'], agent_id=db_agent_id)
                chatlog.append({'type': 'tool_output', 'session_id': session_id,
                                'content': result_str,
                                'tool_call_id': _tc['id'], 'error': True,
                                'function': fn_name})
                messages.append({"role": "tool", "tool_call_id": _tc['id'],
                                 "content": result_str})
                timeline.append({"type": "tool_result", "tool": fn_name,
                                 "error": True})
                event_stream.emit('tool_executed', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id,
                    'channel_id': channel_id,
                    'tool_name': fn_name, 'tool_args': {},
                    'tool_result': {'error': True}, 'has_error': True,
                })
                # Contribute to loop-detection window
                _tool_call_key = f"{fn_name}|"
                _tool_call_window.append(_tool_call_key)
                continue

            # --- Obtain tool_result ---
            if i in _parallel_futures:
                _pr = _parallel_futures[i]
                if isinstance(_pr, Future):
                    tool_result = _pr.result()
                else:
                    tool_result = _pr  # guard-rejection dict
            else:
                from backend.plugin_manager import check_tool_guards
                guard_result = check_tool_guards(agent_id, fn_name, args)
                if guard_result:
                    tool_result = {
                        'error': guard_result.get('error',
                                                  'Blocked by plugin guard'),
                        'blocked_by': 'tool_guard'}
                else:
                    tool_result = _execute_tool_core(fn_name, args,
                                                     builtin_exec, real_exec)

            # Sub-agents are fire-and-forget workers spawned by a parent agent.
            # There's no human watching their session, so auto-approve safety checks.
            # The parent's decision to spawn the sub-agent IS the approval.
            if isinstance(tool_result, dict) and tool_result.get('level') == 'requires_approval' \
                    and (agent.get('is_subagent') or agent_context.get('is_subagent')):
                _logger.info("Sub-agent %s: auto-approving safety check for %s", agent_id, fn_name)
                agent_context['_skip_safety'] = True
                try:
                    tool_result = builtin_exec(fn_name, args)
                    if tool_result is None:
                        tool_result = real_exec(fn_name, args)
                finally:
                    agent_context.pop('_skip_safety', None)

            # Human-in-the-loop approval for requires_approval safety results
            elif isinstance(tool_result, dict) and tool_result.get('level') == 'requires_approval':
                from backend.agent_runtime.approval import approval_registry
                APPROVAL_TIMEOUT = 300  # 5 minutes

                pending = approval_registry.create(
                    session_id=session_id,
                    agent_id=agent_id,
                    tool_call_id=_tc['id'],
                    tool_name=fn_name,
                    tool_args=args,
                    safety_result=tool_result,
                )

                event_stream.emit('approval_required', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'approval_id': pending.approval_id,
                    'tool_name': fn_name,
                    'tool_args': args,
                    'approval_info': tool_result.get('approval_info', {}),
                    'reasons': tool_result.get('reasons', []),
                    'score': tool_result.get('score'),
                    'source_agent_name': agent.get('name', agent_id),
                })

                # Escalation: ensure a human can see the approval.
                # For inter-agent sessions, the current session has no human viewer —
                # always escalate to the agent's own human session or the super agent.
                # For direct sessions, escalate only if no web/channel listener.
                # List of (session_id, external_user_id, channel_id) that received
                # approval_required — used to fan-out approval_resolved to all of them.
                _escalation_targets: list = []
                _is_inter_agent = bool(external_user_id and external_user_id.startswith('__agent__'))
                try:
                    from backend.channels.registry import channel_manager
                    from backend.agent_runtime.notifier import _resolve_agent_target
                    if _is_inter_agent:
                        _needs_escalation = True
                    else:
                        _agent_has_channel = any(
                            ch['id'] in channel_manager._active
                            for ch in db.get_channels(agent_id)
                        )
                        _web_is_watching = event_stream.has_web_listener(session_id)
                        _needs_escalation = not _agent_has_channel and not _web_is_watching

                    if _needs_escalation:
                        _approval_event_payload = {
                            'agent_id': agent_id,
                            'approval_id': pending.approval_id,
                            'tool_name': fn_name,
                            'tool_args': args,
                            'approval_info': tool_result.get('approval_info', {}),
                            'reasons': tool_result.get('reasons', []),
                            'score': tool_result.get('score'),
                            'source_agent_id': agent_id,
                            'source_agent_name': agent.get('name', agent_id),
                        }

                        # Web UI: emit to the agent's most-recent human session so the
                        # approval modal appears in the browser (sessions.html handles this).
                        _human_session = db.get_latest_human_session(agent_id)
                        _human_session_id = _human_session.get('id') if _human_session else None
                        if _human_session_id:
                            _web_uid = _human_session.get('external_user_id', '')
                            _web_cid = _human_session.get('channel_id')
                            event_stream.emit('approval_required', {
                                **_approval_event_payload,
                                'session_id': _human_session_id,
                                'external_user_id': _web_uid,
                                'channel_id': _web_cid,
                            })
                            _escalation_targets.append((_human_session_id, _web_uid, _web_cid))

                        # Channel (Telegram / any active channel): always notify for
                        # inter-agent sessions. For direct sessions, only if no web listener.
                        _has_human_listener = bool(
                            _human_session_id and event_stream.has_web_listener(_human_session_id)
                        )
                        _try_channel_escalation = _is_inter_agent or not _has_human_listener
                        if _try_channel_escalation:
                            _super = db.get_super_agent()
                            if _super and _super['id'] != agent_id:
                                _su_uid, _su_cid = _resolve_agent_target(_super['id'])
                                if _su_uid and _su_cid:
                                    event_stream.emit('approval_required', {
                                        **_approval_event_payload,
                                        'session_id': session_id,
                                        'external_user_id': _su_uid,
                                        'channel_id': _su_cid,
                                    })
                                    _escalation_targets.append((session_id, _su_uid, _su_cid))
                except Exception:
                    pass  # Never block approval flow due to escalation failure

                # Poll every second to also respect the stop signal and timeout
                deadline = time.time() + APPROVAL_TIMEOUT
                while not pending.decision_event.wait(timeout=1.0):
                    if stop_event.is_set():
                        approval_registry.resolve(pending.approval_id, 'reject')
                        break
                    if time.time() >= deadline:
                        break

                timed_out = pending.decision is None
                decision = pending.decision or 'reject'

                if decision == 'approve':
                    # Re-execute bypassing safety check
                    agent_context['_skip_safety'] = True
                    try:
                        tool_result = builtin_exec(fn_name, args)
                        if tool_result is None:
                            tool_result = real_exec(fn_name, args)
                    finally:
                        agent_context.pop('_skip_safety', None)
                else:
                    reason = 'timed out' if timed_out else 'rejected by user'
                    tool_result = {
                        'error': f'Tool execution {reason}. The user declined to approve this action.',
                        'level': 'rejected',
                        'original_reasons': pending.safety_result.get('reasons', []),
                    }

                # Always resolve on the original session (inter-agent or direct)
                _resolved_sessions = {(session_id, channel_id)}
                event_stream.emit('approval_resolved', {
                    'agent_id': agent_id, 'session_id': session_id,
                    'external_user_id': external_user_id, 'channel_id': channel_id,
                    'approval_id': pending.approval_id,
                    'decision': decision,
                    'timed_out': timed_out,
                })
                # Fan-out to all escalation targets (web + Telegram), deduplicating by (session, channel)
                for _esc_sid, _esc_uid, _esc_cid in _escalation_targets:
                    if (_esc_sid, _esc_cid) in _resolved_sessions:
                        continue
                    _resolved_sessions.add((_esc_sid, _esc_cid))
                    event_stream.emit('approval_resolved', {
                        'agent_id': agent_id, 'session_id': _esc_sid,
                        'external_user_id': _esc_uid, 'channel_id': _esc_cid,
                        'approval_id': pending.approval_id,
                        'decision': decision,
                        'timed_out': timed_out,
                    })
                approval_registry.remove(pending.approval_id)

            # Lazy tool injection: use_skill returned tool defs to inject mid-turn
            if fn_name == 'use_skill' and isinstance(tool_result, dict) and 'inject_tools' in tool_result:
                injected = tool_result.pop('inject_tools')
                loaded_sid = tool_result.get('id', '')
                injected_fns = []
                for td in injected:
                    fn = td.get('function', {}).get('name', '')
                    if fn and not any(t.get('function', {}).get('name') == fn for t in tools):
                        tools.append({"type": "function", "function": td['function']})
                        injected_fns.append(fn)
                if loaded_sid and injected_fns:
                    _loaded_lazy_skills[loaded_sid] = injected_fns
                    session_skill_tools.setdefault(session_id, {})[loaded_sid] = [
                        t for t in tools if t.get('function', {}).get('name', '') in set(injected_fns)
                    ]
                # Add injected tool IDs to assigned_tool_ids for authorization guard
                _assigned = agent_context.get('assigned_tool_ids')
                if _assigned is not None and loaded_sid:
                    for fn in injected_fns:
                        _tid = f'skill:{loaded_sid}:{fn}'
                        if _tid not in _assigned:
                            _assigned.append(_tid)

            # Persistent skill context: capture system_md for re-injection each iteration
            if fn_name == 'use_skill' and isinstance(tool_result, dict) and tool_result.get('system_md'):
                loaded_sid = tool_result.get('id', '')
                if loaded_sid:
                    _skill_system_mds[loaded_sid] = tool_result['system_md']
                    session_skill_mds.setdefault(session_id, {})[loaded_sid] = tool_result['system_md']

            # Lazy tool removal: unload_skill removes injected tools from context
            if fn_name == 'unload_skill' and isinstance(tool_result, dict) and tool_result.get('remove_tools'):
                unload_sid = tool_result.get('id', '')
                if unload_sid in _loaded_lazy_skills:
                    fns_to_remove = set(_loaded_lazy_skills.pop(unload_sid))
                    tools[:] = [t for t in tools if t.get('function', {}).get('name', '') not in fns_to_remove]
                    session_skill_tools.get(session_id, {}).pop(unload_sid, None)
                # Remove unloaded tool IDs from assigned_tool_ids
                _assigned = agent_context.get('assigned_tool_ids')
                if _assigned is not None and unload_sid:
                    for fn in fns_to_remove:
                        _tid = f'skill:{unload_sid}:{fn}'
                        if _tid in _assigned:
                            _assigned.remove(_tid)

            # Persistent skill context: clear system_md when skill is unloaded
            if fn_name == 'unload_skill' and isinstance(tool_result, dict):
                unload_sid = tool_result.get('id', '')
                _skill_system_mds.pop(unload_sid, None)
                session_skill_mds.get(session_id, {}).pop(unload_sid, None)

            # Serialize tool result for LLM (always valid JSON when possible)
            try:
                result_str = json.dumps(tool_result)
            except (TypeError, ValueError):
                result_str = str(tool_result)

            # Truncate for LLM context; UI also uses this so display matches what LLM sees
            if len(result_str) > MAX_TOOL_RESULT_CHARS:
                remaining = len(result_str) - MAX_TOOL_RESULT_CHARS
                llm_result_str = (result_str[:MAX_TOOL_RESULT_CHARS] +
                                  f"\n...[truncated — {remaining} chars omitted]")
            else:
                llm_result_str = result_str

            # Structured result for timeline/UI — mirrors what LLM receives
            if llm_result_str == result_str:
                # No truncation: use structured dict for richer display
                if isinstance(tool_result, dict):
                    result_dict = tool_result
                elif isinstance(tool_result, list):
                    result_dict = {"data": tool_result}
                elif isinstance(tool_result, str):
                    result_dict = {"data": tool_result}
                else:
                    result_dict = {"data": result_str}
            else:
                # Truncated: plain string so UI shows exactly what LLM gets
                result_dict = {"data": llm_result_str}
                # Preserve small metadata fields so tool-specific UI can still render
                if isinstance(tool_result, dict):
                    for _key in ('exit_code', 'execution_time'):
                        if _key in tool_result:
                            result_dict[_key] = tool_result[_key]

            has_error = isinstance(tool_result, dict) and ('error' in tool_result or tool_result.get('status') == 'error')

            timeline.append({"type": "tool_result", "tool": fn_name, "result": result_dict, "error": has_error})

            event_stream.emit('tool_executed', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'tool_name': fn_name, 'tool_args': args,
                'tool_result': result_dict, 'has_error': has_error,
            })

            # Persist agent state immediately for state-changing built-in tools
            if fn_name in ('save_plan', 'set_mode', 'update_tasks', 'state'):
                _ms = agent_context.get('agent_state')
                if _ms is not None:
                    _persist_agent_state_split(_ms, agent_id, session_id, db_agent_id)

            # Record in trace (for animated bubbles)
            tool_trace.append({"tool": fn_name, "args": args, "result": result_dict})

            # Save tool result message (same as LLM receives, for UI consistency)
            db.add_chat_message(session_id, 'tool', llm_result_str, tool_call_id=_tc['id'], agent_id=db_agent_id)
            chatlog.append({'type': 'tool_output', 'session_id': session_id,
                            'content': llm_result_str, 'tool_call_id': _tc['id'], 'error': has_error,
                            'function': fn_name})
            messages.append({
                "role": "tool",
                "tool_call_id": _tc['id'],
                "content": llm_result_str
            })

            # Sliding-window tool+args loop detection (window=10, threshold=5).
            # Catches loops even when other tools are interleaved between repeats.
            _tool_call_key = f"{fn_name}|{json.dumps(args, sort_keys=True, default=str)}"
            _tool_call_window.append(_tool_call_key)
            if _post_force_stop_tool_count > 0:
                _post_force_stop_tool_count += 1
                if _post_force_stop_tool_count > 3:
                    _logger.error("LLM still calling tools after force-stop — hard terminating")
                    error_msg = "LLM Error: Agent continued calling tools after loop-detection force-stop. Terminated."
                    _pfs_dur = round(time.time() - _loop_start_time, 1)
                    db.add_chat_message(session_id, 'assistant', error_msg, agent_id=db_agent_id,
                                        metadata={"error": True, "timeline": timeline,
                                                  "thinking_duration": _pfs_dur})
                    chatlog.append({'type': 'error', 'session_id': session_id, 'content': error_msg,
                                    'metadata': {'error': True, 'thinking_duration': _pfs_dur}})
                    chatlog.append({'type': 'turn_end', 'session_id': session_id, 'thinking_duration': _pfs_dur})
                    return {"text": error_msg, "error": True}, tool_trace, timeline

            if _tool_call_window.count(_tool_call_key) >= 5 and not _tool_args_force_stop_injected:
                _logger.warning("Loop detected (%d/10 calls in window: %s) — injecting force-stop",
                               _tool_call_window.count(_tool_call_key), fn_name)
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] URGENT: You have called the tool '{fn_name}' with the same arguments {_tool_call_window.count(_tool_call_key)} times in the last {len(_tool_call_window)} tool calls. STOP and revert to the state where you started. Review your previous results and provide your FINAL answer."
                })
                _tool_args_force_stop_injected = True
                _any_force_stop_injected = True
                _post_force_stop_tool_count = 1

        # Shut down parallel execution pool (no-op if no parallel tools were used)
        if _pool is not None:
            _pool.shutdown(wait=False)

        # Tool calls executed successfully — reset continuation nudge counter
        _continuation_nudge_count = 0

        # Check B: stop signal check after tool execution, before next LLM call
        if stop_event.is_set():
            stop_event.clear()
            _logger.info("Stop signal received for session %s — aborting after tools", session_id)
            stop_msg = "Agent stopped by user request."
            _stopb_dur = round(time.time() - _loop_start_time, 1)
            db.add_chat_message(session_id, 'assistant', stop_msg, agent_id=db_agent_id,
                                metadata={"timeline": timeline, "stopped": True, "thinking_duration": _stopb_dur})
            chatlog.append({'type': 'final', 'session_id': session_id, 'content': stop_msg,
                            'metadata': {'stopped': True, 'thinking_duration': _stopb_dur}})
            _stopb_inj = ("[SYSTEM] Your previous reasoning and response were forcefully "
                          "interrupted by the user via /stop before completion. "
                          "Await the user's next instruction.")
            db.add_chat_message(session_id, 'user', _stopb_inj,
                                agent_id=db_agent_id, metadata={"stop_injection": True})
            chatlog.append({'type': 'system', 'session_id': session_id, 'content': _stopb_inj,
                            'metadata': {'stop_injection': True}})
            chatlog.append({'type': 'turn_end', 'session_id': session_id, 'thinking_duration': _stopb_dur})
            event_stream.emit('final_answer', {
                'agent_id': agent_id, 'session_id': session_id,
                'external_user_id': external_user_id, 'channel_id': channel_id,
                'answer': stop_msg, 'tool_trace': tool_trace, 'timeline': timeline,
            })
            return stop_msg, tool_trace, timeline

        # If the LLM has been repeating the same intermediate response 3+ times,
        # inject an urgent instruction to break the loop.
        if _intermediate_dup_count >= 3:
            _logger.warning("Loop detected (%d duplicates) — injecting force-stop", _intermediate_dup_count)
            messages.append({
                "role": "user",
                "content": "[SYSTEM] URGENT: You are stuck repeating the same response in a loop. "
                           "STOP calling tools immediately. Summarise what you have found so far and "
                           "give your FINAL answer NOW."
            })
            _intermediate_dup_count = 0
            _force_stop_injected = True
            _any_force_stop_injected = True

        # Run message interceptors — plugins can inject system messages after intermediate responses
        from backend.plugin_manager import run_message_interceptors
        for inj_msg in run_message_interceptors(agent_id, content, messages):
            messages.append(inj_msg)

    _logger.error("Maximum tool iterations reached (%d)", max_tool_iterations)
    error_msg = (
        f"LLM Error: Maximum tool iterations reached ({max_tool_iterations}). "
        f"The model could not produce a final answer within this limit. "
        f"You can increase this limit in System Settings → General → Max Tool Iterations."
    )
    _max_dur = round(time.time() - _loop_start_time, 1)
    db.add_chat_message(session_id, 'assistant', error_msg, agent_id=db_agent_id,
                        metadata={"error": True, "timeline": timeline, "thinking_duration": _max_dur})
    chatlog.append({'type': 'error', 'session_id': session_id, 'content': error_msg,
                    'metadata': {'error': True, 'thinking_duration': _max_dur}})
    chatlog.append({'type': 'turn_end', 'session_id': session_id, 'thinking_duration': _max_dur})
    return {"text": error_msg, "error": True}, tool_trace, timeline
