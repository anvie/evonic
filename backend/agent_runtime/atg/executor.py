"""
ATG executor — dependency-aware scheduling of a compiled TaskDAG.

Executes the graph wave by wave (topological levels): read-only nodes in a
wave run in parallel via ThreadPoolExecutor, mutating nodes run serially in
node-id order. Node arguments are bound either directly from the compiled
args_template (placeholders resolved from upstream outputs) or, when that is
not possible, by a localized LLM call that sees ONLY the node's goal, tool
schema and declared upstream excerpts — never the conversation history.

The executor front-loads tool work for run_tool_loop: results are recorded
in the DAG (persisted via agent_state), mirrored to tool_trace / timeline /
event_stream exactly like the loop's Phase 3, and summarized into one system
message that the untouched loop consumes to compose the final answer.

Failure policy (M3): a node that fails after binding attempts marks the run
as 'fallback' — remaining nodes are skipped and the plain loop continues
with the partial results. M4 inserts minimal-subgraph repair before that.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from backend.agent_runtime.atg import prompts
from backend.agent_runtime.atg.graph import (
    MAX_REPAIR_ATTEMPTS,
    PLACEHOLDER_RE,
    RefinementHistory,
    TaskDAG,
    parse_placeholder,
)
from backend.agent_runtime.atg.interfaces import get_interface_catalog, is_read_only
from backend.agent_runtime.llm_call import _MAX_PARALLEL_TOOL_WORKERS, _execute_tool_core
from backend.event_stream import event_stream

_logger = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 300  # seconds, same as llm_loop's approval flow
_BIND_ATTEMPTS = 2
_SUMMARY_EXCERPT_CHARS = 300
_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)


class AtgOutcome:
    def __init__(self, status: str, summary_for_llm: str = '',
                 stopped: bool = False, stats: dict = None):
        self.status = status          # done | fallback | failed
        self.summary_for_llm = summary_for_llm
        self.stopped = stopped
        self.stats = stats or {}


class _NodeError(Exception):
    """A node-level failure (binding, guard, rejection, tool error)."""


def _is_error_result(result) -> bool:
    """Error detection covering both conventions: an 'error' key / error
    status, and tools that return bare 'Error: ...' strings (e.g. read_file),
    which _run_tool wraps as {'result': 'Error: ...'}. ATG must catch these —
    they gate repair and keep garbage out of downstream placeholders."""
    if not isinstance(result, dict):
        return True
    if 'error' in result or result.get('status') == 'error':
        return True
    value = result.get('result')
    return isinstance(value, str) and value.startswith('Error:')


# ── Argument binding ─────────────────────────────────────────────────────────

def _lookup_output(outputs: dict, node_id: str, key: str):
    value = outputs.get(node_id)
    if isinstance(value, dict) and key in value:
        return value[key]
    return None


def _resolve_template(args_template: dict, outputs: dict):
    """Resolve '${node.key}' placeholders from upstream outputs.

    Returns (resolved_args, fully_resolved). String values that are exactly
    one placeholder keep the raw upstream value (any type); embedded
    placeholders are substituted as strings.
    """
    unresolved = []

    def resolve(value):
        if isinstance(value, str):
            m = PLACEHOLDER_RE.fullmatch(value.strip())
            if m:
                parsed = parse_placeholder(m.group(1))
                if parsed:
                    v = _lookup_output(outputs, *parsed)
                    if v is None:
                        unresolved.append(m.group(1))
                        return value
                    return v

            def sub(match):
                parsed_ = parse_placeholder(match.group(1))
                v = _lookup_output(outputs, *parsed_) if parsed_ else None
                if v is None:
                    unresolved.append(match.group(1))
                    return match.group(0)
                return v if isinstance(v, str) else json.dumps(v, default=str)
            return PLACEHOLDER_RE.sub(sub, value)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    resolved = resolve(args_template)
    return resolved, not unresolved


def _upstream_digest(node, dag: TaskDAG, outputs: dict) -> str:
    """Localized context: excerpts of the node's declared upstream outputs."""
    lines = []
    for dep in node.deps:
        producer = dag.get(dep)
        value = outputs.get(dep)
        if value is None and producer is not None:
            value = producer.record.get('output_excerpt')
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if text and len(text) > 1500:
            text = text[:1500] + '…[truncated]'
        lines.append(f"- {dep} ({producer.goal if producer else '?'}): {text or '(no output)'}")
    return '\n'.join(lines) or '(none)'


def _bind_node_via_llm(node, dag, outputs, runtime, available_tools,
                       feedback: str = None) -> tuple:
    """One localized LLM call to produce (tool, args) for this node."""
    catalog = get_interface_catalog(available_tools)
    tool_names = {(t.get('function') or {}).get('name') for t in available_tools}
    tool_constraint = (f"You MUST use the tool: {node.tool}" if node.tool
                       else "Choose the most suitable tool from the catalog.")
    user = prompts.NODE_BIND_USER.format(
        goal=node.goal,
        tool_constraint=tool_constraint,
        args_template=json.dumps(node.args_template, default=str),
        upstream=_upstream_digest(node, dag, outputs),
    )
    if feedback:
        user += f"\n\nReviewer feedback on the previous attempt: {feedback}"
    system = prompts.NODE_BIND_SYSTEM.format(catalog=catalog)

    last_error = None
    for _ in range(_BIND_ATTEMPTS):
        prompt = user if last_error is None else (
            user + prompts.NODE_BIND_RETRY_SUFFIX.format(errors=last_error))
        with runtime['llm_lock']:
            result = runtime['llm'].chat_completion(
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt}],
                tools=None, temperature=0, enable_thinking=False,
                max_tokens=None, log_file=runtime.get('llm_log_path'),
            )
        if not result.get('success'):
            last_error = result.get('error_detail') or 'LLM call failed'
            continue
        msg = (result.get('response') or {}).get('choices', [{}])[0].get('message', {})
        content = msg.get('content') or msg.get('reasoning_content') or ''
        from backend.agent_runtime.llm_json import extract_first_json
        obj = extract_first_json(content)
        if obj is None:
            last_error = "invalid JSON: no parseable object in response"
            continue
        tool = node.tool or obj.get('tool')
        args = obj.get('args')
        if not tool or tool not in tool_names:
            last_error = f"unknown tool '{tool}'"
            continue
        if not isinstance(args, dict):
            last_error = "'args' must be an object"
            continue
        return tool, args
    raise _NodeError(f"argument binding failed: {last_error}")


# ── Single-node execution ────────────────────────────────────────────────────

class _ExecCtx:
    """Bag of per-run wiring shared by node executions."""

    def __init__(self, agent, agent_context, runtime, builtin_exec, real_exec,
                 chatlog, tool_trace, timeline, session_id, stop_event):
        self.agent = agent
        self.agent_context = agent_context
        self.runtime = runtime
        self.builtin_exec = builtin_exec
        self.real_exec = real_exec
        self.chatlog = chatlog
        self.tool_trace = tool_trace
        self.timeline = timeline
        self.session_id = session_id
        self.stop_event = stop_event
        self.agent_id = agent_context.get('id', '')
        self.db_agent_id = agent_context.get('_db_agent_id', self.agent_id)
        self.external_user_id = agent_context.get('user_id', '')
        self.channel_id = agent_context.get('channel_id')

    def emit(self, event, payload):
        event_stream.emit(event, {
            'agent_id': self.agent_id, 'session_id': self.session_id,
            'external_user_id': self.external_user_id,
            'channel_id': self.channel_id, **payload,
        })


def _run_tool(ctx: _ExecCtx, tool: str, args: dict) -> dict:
    """Guard-checked tool execution with the loop's approval semantics."""
    from backend.plugin_manager import check_tool_guards
    guard_context = {
        'agent_id': ctx.agent_id, 'session_id': ctx.session_id,
        'external_user_id': ctx.external_user_id, 'channel_id': ctx.channel_id,
    }
    guard = check_tool_guards(ctx.agent_id, tool, args, guard_context)
    if guard and guard.get('level') != 'requires_approval':
        return {'error': guard.get('error', 'Blocked by plugin guard'),
                'blocked_by': 'tool_guard'}
    if guard:  # requires_approval from a plugin guard
        result = guard
    else:
        result = _execute_tool_core(tool, args, ctx.builtin_exec, ctx.real_exec)

    if isinstance(result, dict) and result.get('level') == 'requires_approval':
        result = _await_approval(ctx, tool, args, result)
    return result if isinstance(result, dict) else {'result': result}


def _await_approval(ctx: _ExecCtx, tool: str, args: dict, safety_result: dict) -> dict:
    """Human-in-the-loop approval for a DAG node (same registry/timeout as the loop)."""
    from backend.agent_runtime.approval import approval_registry
    from models.db import db

    if ctx.external_user_id and ctx.external_user_id.startswith('api:'):
        return {'error': 'Tool execution rejected: API consumers cannot approve tool calls.',
                'level': 'rejected',
                'original_reasons': safety_result.get('reasons', [])}

    pending = approval_registry.create(
        session_id=ctx.session_id, agent_id=ctx.agent_id,
        tool_call_id=f'atg-{tool}-{int(time.time() * 1000)}',
        tool_name=tool, tool_args=args, safety_result=safety_result)
    try:
        db.store_pending_tool_approval(
            approval_id=pending.approval_id, session_id=ctx.session_id,
            agent_id=ctx.agent_id, tool_name=tool, tool_args=args,
            approval_info=safety_result.get('approval_info', {}),
            reasons=safety_result.get('reasons', []),
            score=safety_result.get('score'),
            source_agent_id=ctx.agent_id,
            source_agent_name=ctx.agent.get('name', ctx.agent_id))
    except Exception:
        pass
    ctx.emit('approval_required', {
        'approval_id': pending.approval_id, 'tool_name': tool, 'tool_args': args,
        'approval_info': safety_result.get('approval_info', {}),
        'reasons': safety_result.get('reasons', []),
        'score': safety_result.get('score'),
        'source_agent_name': ctx.agent.get('name', ctx.agent_id)})

    deadline = time.time() + _APPROVAL_TIMEOUT
    while time.time() < deadline and pending.decision is None:
        if ctx.stop_event.is_set():
            break
        pending.decision_event.wait(timeout=1.0)

    timed_out = pending.decision is None
    decision = pending.decision or 'reject'
    ctx.emit('approval_resolved', {
        'approval_id': pending.approval_id, 'decision': decision,
        'timed_out': timed_out})
    approval_registry.remove(pending.approval_id)
    try:
        db.delete_pending_tool_approval(pending.approval_id)
    except Exception:
        pass

    if decision == 'approve':
        ctx.agent_context['_skip_safety'] = True
        try:
            result = ctx.builtin_exec(tool, args)
            if result is None:
                result = ctx.real_exec(tool, args)
            return result if isinstance(result, dict) else {'result': result}
        finally:
            ctx.agent_context.pop('_skip_safety', None)
    reason = 'timed out' if timed_out else 'rejected by user'
    return {'error': f'Tool execution {reason}. The user declined to approve this action.',
            'level': 'rejected',
            'original_reasons': safety_result.get('reasons', [])}


def _bind_node(ctx: _ExecCtx, dag: TaskDAG, node, outputs: dict) -> tuple:
    """Resolve (tool, args) for a node — direct template resolution when
    possible, localized LLM bind otherwise. Raises _NodeError on failure."""
    resolved, fully = _resolve_template(node.args_template, outputs)
    if node.tool and fully and node.args_template:
        return node.tool, resolved
    return _bind_node_via_llm(node, dag, outputs, ctx.runtime,
                              ctx.runtime.get('tools') or [])


def _record_bind_failure(ctx: _ExecCtx, node, error: str, ts_start: float):
    node.status = 'failed'
    node.record_result(error=error, ts_start=ts_start, ts_end=time.time())
    ctx.timeline.append({"type": "tool_result", "tool": node.tool or '?',
                         "error": True})


def _execute_node(ctx: _ExecCtx, dag: TaskDAG, node, outputs: dict) -> dict:
    """Bind and execute one node; records state on the node. Returns the result."""
    ts_start = time.time()
    node.status = 'running'
    node.attempts += 1
    try:
        tool, args = _bind_node(ctx, dag, node, outputs)
    except _NodeError as e:
        _record_bind_failure(ctx, node, str(e), ts_start)
        return {'error': str(e)}
    return _execute_bound(ctx, node, tool, args, outputs, ts_start)


def _execute_bound(ctx: _ExecCtx, node, tool: str, args: dict,
                   outputs: dict, ts_start: float) -> dict:
    ctx.timeline.append({"type": "tool_call", "tool": tool, "args": args,
                         "param_types": {}, "atg_node": node.id})
    ctx.emit('tool_call_started', {'tool_name': tool, 'tool_args': args,
                                   'param_types': {}, 'atg_node': node.id})

    result = _run_tool(ctx, tool, args)
    has_error = _is_error_result(result)
    ts_end = time.time()

    node.tool = tool
    node.status = 'failed' if has_error else 'done'
    node.record_result(resolved_args=args,
                       output=None if has_error else result,
                       error=(result.get('error') or str(result.get('result')))
                             if has_error else None,
                       ts_start=ts_start, ts_end=ts_end)
    if not has_error:
        outputs[node.id] = result

    ctx.tool_trace.append({"tool": tool, "args": args, "result": result})
    ctx.timeline.append({"type": "tool_result", "tool": tool,
                         "error": has_error, "atg_node": node.id})
    ctx.chatlog.append({'type': 'atg_node_result', 'session_id': ctx.session_id,
                        'node_id': node.id, 'tool': tool,
                        'content': json.dumps(result, default=str)[:4000],
                        'error': has_error})
    ctx.emit('tool_executed', {'tool_name': tool, 'tool_args': args,
                               'tool_result': result, 'has_error': has_error,
                               'atg_node': node.id})
    return result


# ── Thought experiment (pre-execution simulation) ────────────────────────────

def _thought_experiment(ctx: _ExecCtx, dag: TaskDAG, planned: list,
                        outputs: dict) -> dict:
    """One cheap LLM call reviewing a wave's mutating calls before they run.

    `planned` is [(node, tool, args)]. Returns {node_id: {verdict, reason}};
    any parse/LLM failure returns {} — never block execution on a broken
    simulator.
    """
    nodes_block = '\n'.join(
        f"- {n.id}: {t}({json.dumps(a, default=str)[:300]}) — goal: {n.goal}"
        for n, t, a in planned)
    dep_ids = sorted({d for n, _, _ in planned for d in n.deps})
    upstream_lines = []
    for dep in dep_ids:
        value = outputs.get(dep)
        producer = dag.get(dep)
        if value is None and producer is not None:
            value = producer.record.get('output_excerpt')
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if text and len(text) > 200:
            text = text[:200] + '…'
        upstream_lines.append(f"- {dep}: {text or '(no output)'}")

    try:
        with ctx.runtime['llm_lock']:
            result = ctx.runtime['llm'].chat_completion(
                messages=[{"role": "system", "content": prompts.THOUGHT_SYSTEM},
                          {"role": "user", "content": prompts.THOUGHT_USER.format(
                              root_goal=dag.root_goal,
                              nodes_block=nodes_block,
                              upstream='\n'.join(upstream_lines) or '(none)')}],
                tools=None, temperature=0, enable_thinking=False,
                max_tokens=400, log_file=ctx.runtime.get('llm_log_path'))
        if not result.get('success'):
            return {}
        msg = (result.get('response') or {}).get('choices', [{}])[0].get('message', {})
        content = msg.get('content') or msg.get('reasoning_content') or ''
        from backend.agent_runtime.llm_json import extract_first_json
        verdicts = extract_first_json(content)
        return verdicts if isinstance(verdicts, dict) else {}
    except Exception:
        _logger.warning("ATG thought experiment failed — proceeding unchecked",
                        exc_info=True)
        return {}


# ── Run loop ─────────────────────────────────────────────────────────────────

def _seed_outputs(dag: TaskDAG) -> dict:
    """Rebuild in-memory outputs from persisted records (resume after restart)."""
    outputs = {}
    for nid, node in dag.nodes.items():
        if node.status in ('done', 'frozen') and node.record.get('output_excerpt'):
            excerpt = node.record['output_excerpt']
            try:
                outputs[nid] = json.loads(excerpt)
            except ValueError:
                outputs[nid] = {'result': excerpt}
    return outputs


def _summarize(dag: TaskDAG, status: str, failed_node=None) -> str:
    lines = [f"[ATG] Task graph execution — status: {status}. Goal: {dag.root_goal}"]
    done = [n for n in dag.nodes.values() if n.status == 'done']
    lines.append(f"{len(done)}/{len(dag.nodes)} nodes completed.")
    lines.append("Results:")
    for nid in sorted(dag.nodes):
        node = dag.nodes[nid]
        if node.status == 'done':
            excerpt = (node.record.get('output_excerpt') or '')[:_SUMMARY_EXCERPT_CHARS]
            lines.append(f"- {nid} {node.tool}({json.dumps(node.record.get('resolved_args') or {}, default=str)[:120]}): ok — {excerpt}")
        elif node.status == 'failed':
            lines.append(f"- {nid} ({node.goal}): FAILED — {node.record.get('error')}")
        elif node.status == 'skipped':
            lines.append(f"- {nid} ({node.goal}): skipped")
    if status == 'done':
        lines.append(
            "All graph nodes completed. Use these results to finish the task "
            "and compose the final answer for the user.")
    else:
        lines.append(
            "Graph execution could not complete. The completed node outputs "
            "above are valid — continue the task manually with normal tool "
            "calls from this point.")
    return '\n'.join(lines)


def run_dag_execution(agent, agent_context, ms, stop_event,
                      builtin_exec, real_exec, chatlog, tool_trace,
                      timeline, session_id, persist_cb=None) -> AtgOutcome:
    """Execute ms.atg's DAG. Mutates ms.atg in place; caller persists on return
    (persist_cb, when given, is also invoked after every wave for crash safety).
    """
    runtime = agent_context.get('_atg_runtime') or {}
    if not runtime.get('llm'):
        raise RuntimeError("ATG runtime missing from agent_context")

    dag = TaskDAG.from_dict(ms.atg['dag'])
    history = RefinementHistory.from_dict(ms.atg.get('history') or {})
    ms.atg['status'] = 'executing'
    outputs = _seed_outputs(dag)
    stats = ms.atg.setdefault('stats', {})
    stats.setdefault('waves_executed', 0)
    stats.setdefault('parallel_peak', 0)
    parallel_ok = not agent_context.get('disable_parallel_tool_execution', 0)

    ctx = _ExecCtx(agent, agent_context, runtime, builtin_exec, real_exec,
                   chatlog, tool_trace, timeline, session_id, stop_event)

    def _sync_state(status=None):
        if status:
            ms.atg['status'] = status
        ms.atg['dag'] = dag.to_dict()
        ms.atg['history'] = history.to_dict()
        if persist_cb:
            try:
                persist_cb()
            except Exception:
                _logger.exception("ATG state persist failed")

    def _mark_skipped():
        for node in dag.nodes.values():
            if node.status in ('pending', 'ready', 'running'):
                node.status = 'skipped'

    ctx.emit('atg_wave', {'phase': 'start', 'nodes_total': len(dag.nodes)})

    while True:
        if stop_event.is_set():
            _mark_skipped()
            _sync_state()
            return AtgOutcome('failed', stopped=True, stats=stats)

        waves = dag.waves()
        if not waves:
            break
        wave_ids = waves[0]
        wave_nodes = [dag.nodes[nid] for nid in wave_ids]

        failed_here = [n for n in wave_nodes if n.status == 'failed']
        if failed_here:
            failed_node = failed_here[0]
            # Minimal-subgraph repair, bounded per run; exhaustion → fallback.
            if ms.atg.get('repair_attempts', 0) < MAX_REPAIR_ATTEMPTS:
                new_dag = None
                try:
                    from backend.agent_runtime.atg.repair import attempt_repair
                    new_dag = attempt_repair(
                        dag, history, failed_node.id, runtime['llm'],
                        runtime['llm_lock'], runtime.get('tools') or [],
                        attempt=ms.atg.get('repair_attempts', 0) + 1,
                        log_file=runtime.get('llm_log_path'))
                except Exception as e:
                    _logger.warning("ATG repair of %s not possible: %s",
                                    failed_node.id, e)
                if new_dag is not None:
                    ms.atg['repair_attempts'] = ms.atg.get('repair_attempts', 0) + 1
                    stats['repairs'] = stats.get('repairs', 0) + 1
                    dag = new_dag
                    history.record(failed_node.id, dag)
                    ctx.emit('atg_repair', {
                        'failed_node': failed_node.id,
                        'attempt': ms.atg['repair_attempts'],
                        'error': failed_node.record.get('error')})
                    _sync_state()
                    continue
            _mark_skipped()
            _sync_state('fallback')
            ctx.emit('atg_fallback', {'failed_node': failed_node.id,
                                      'error': failed_node.record.get('error')})
            return AtgOutcome('fallback',
                              summary_for_llm=_summarize(dag, 'fallback', failed_node),
                              stats=stats)

        parallel_nodes = [n for n in wave_nodes
                          if n.tool and is_read_only(n.tool) and n.args_template]
        serial_nodes = [n for n in wave_nodes if n not in parallel_nodes]
        stats['waves_executed'] += 1
        ctx.emit('atg_wave', {'phase': 'execute', 'wave': wave_ids,
                              'parallel': [n.id for n in parallel_nodes]})

        if parallel_nodes and parallel_ok and len(parallel_nodes) > 1:
            stats['parallel_peak'] = max(stats['parallel_peak'], len(parallel_nodes))
            with ThreadPoolExecutor(
                    max_workers=min(len(parallel_nodes), _MAX_PARALLEL_TOOL_WORKERS),
                    thread_name_prefix='atg-parallel') as pool:
                futures = [pool.submit(_execute_node, ctx, dag, n, outputs)
                           for n in parallel_nodes]
                for f in futures:
                    f.result()
        else:
            serial_nodes = parallel_nodes + serial_nodes

        # Serial nodes: bind first so the thought experiment reviews the
        # concrete calls, then execute in id order.
        bound = []
        for node in serial_nodes:
            if stop_event.is_set():
                _mark_skipped()
                _sync_state()
                return AtgOutcome('failed', stopped=True, stats=stats)
            ts_start = time.time()
            node.status = 'running'
            node.attempts += 1
            try:
                tool, args = _bind_node(ctx, dag, node, outputs)
                bound.append([node, tool, args, ts_start])
            except _NodeError as e:
                _record_bind_failure(ctx, node, str(e), ts_start)

        mutating = [(n, t, a) for n, t, a, _ in bound if not is_read_only(t)]
        if mutating:
            stats['thought_checks'] = stats.get('thought_checks', 0) + 1
            verdicts = _thought_experiment(ctx, dag, mutating, outputs)
            for entry in bound:
                node, _tool, _args, ts_start = entry
                v = verdicts.get(node.id) or {}
                verdict = v.get('verdict')
                if verdict == 'abort':
                    _record_bind_failure(
                        ctx, node,
                        f"aborted by pre-execution check: {v.get('reason') or 'no reason given'}",
                        ts_start)
                    entry[0] = None
                elif verdict == 'revise':
                    try:
                        entry[1], entry[2] = _bind_node_via_llm(
                            node, dag, outputs, ctx.runtime,
                            ctx.runtime.get('tools') or [],
                            feedback=v.get('reason'))
                    except _NodeError as e:
                        _record_bind_failure(ctx, node, str(e), ts_start)
                        entry[0] = None

        for node, tool, args, ts_start in bound:
            if node is None:
                continue
            if stop_event.is_set():
                _mark_skipped()
                _sync_state()
                return AtgOutcome('failed', stopped=True, stats=stats)
            _execute_bound(ctx, node, tool, args, outputs, ts_start)

        _sync_state()

    failed = [n for n in dag.nodes.values() if n.status == 'failed']
    status = 'fallback' if failed else 'done'
    _sync_state(status)
    ctx.emit('atg_wave', {'phase': 'end', 'status': status})
    return AtgOutcome(status,
                      summary_for_llm=_summarize(dag, status,
                                                 failed[0] if failed else None),
                      stats=stats)
