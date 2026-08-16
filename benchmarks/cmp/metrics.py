"""Metrics extraction for CMP benchmark runs.

Reads, for each raw run file produced by runner.py:
  - agents/<agent>/llm_traces/<session-suffix>.jsonl  (every LLM call: full
    request, usage, duration) -> per-turn context/token/latency metrics,
    split into main-agent calls vs CMP detector calls;
  - agents/<agent>/chat.db session_state -> CMP path graph + stats;
  - the recorded responses -> probe scores (deterministic, scenarios+scoring).

Outputs results/<run_id>/metrics/<run_key>.json plus turns.csv and
summary.json across all runs.
"""

import csv
import glob
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import REPO_ROOT          # noqa: E402
from scenarios import build           # noqa: E402
from scoring import score_probe       # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')

TOKEN_MONITOR_DB = os.path.join(REPO_ROOT, 'shared', 'data', 'db', 'plugins',
                                'token_monitor.db')
DETECTOR_MARK = 'task-path map of a multi-task session'
# Proxy pricing (USD per 1M tokens) for economic-cost estimates: hosted
# Qwen-9B-class open-model rate. Config, not measurement.
PRICE_IN_PER_M = 0.10
PRICE_OUT_PER_M = 0.30


def _trace_path(agent, session_id):
    suffix = session_id.split('-', 1)[1] if session_id and '-' in session_id else session_id
    return os.path.join(REPO_ROOT, 'agents', agent, 'llm_traces', f'{suffix}.jsonl')


def _load_traces(agent, session_id):
    path = _trace_path(agent, session_id)
    recs = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
    return recs


def _classify_call(rec):
    msgs = rec.get('request', {}).get('messages', [])
    sysmsg = next((m.get('content') or '' for m in msgs if m.get('role') == 'system'), '')
    if DETECTOR_MARK in sysmsg:
        return 'detector'
    return 'main'


_SMART = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"})


def _is_error_result(content):
    """A tool result that represents an INVALID/failed action (not file content
    that merely contains the word 'error'). Genuine errors are structured:
    {"error": "..."} with no result/stdout, or an error-prefixed string."""
    c = (content or '').strip().translate(_SMART)
    if c.startswith('{'):
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and obj.get('error') and not (
                    obj.get('result') or obj.get('stdout') or obj.get('content')):
                return True
        except ValueError:
            pass
    return c[:30].lower().startswith(('error', 'traceback', 'exception'))


def _turn_action_stats(records):
    """(tool_calls_emitted, tool_errors) for a turn — the model's actions and
    how many were invalid/failed. Attribution is by tool_call_id so results are
    matched to this turn's calls only."""
    emitted, errored = set(), set()
    for r in records:
        msg = ((r.get('response') or {}).get('choices') or [{}])[0].get('message', {})
        for tc in (msg.get('tool_calls') or []):
            if tc.get('id'):
                emitted.add(tc['id'])
    for r in records:
        for m in r.get('request', {}).get('messages', []):
            if (m.get('role') == 'tool' and m.get('tool_call_id') in emitted
                    and _is_error_result(str(m.get('content') or ''))):
                errored.add(m['tool_call_id'])
    return len(emitted), len(errored)


_MONITOR_CACHE = None


def _monitor_rows():
    """All token_monitor rows (any source) with parsed unix timestamps."""
    global _MONITOR_CACHE
    if _MONITOR_CACHE is not None:
        return _MONITOR_CACHE
    from datetime import datetime
    out = []
    if os.path.exists(TOKEN_MONITOR_DB):
        con = sqlite3.connect(f'file:{TOKEN_MONITOR_DB}?mode=ro', uri=True)
        try:
            rows = con.execute(
                "SELECT created_at, source, agent_id, model, prompt_tokens, "
                "completion_tokens, duration_ms FROM token_usage").fetchall()
        finally:
            con.close()
        for created_at, source, agent_id, model, pt, ct, dur in rows:
            try:
                ts = datetime.fromisoformat(created_at).timestamp()
            except (ValueError, TypeError):
                continue
            out.append({'ts': ts, 'source': source, 'agent_id': agent_id,
                        'model': model, 'prompt_tokens': pt or 0,
                        'completion_tokens': ct or 0, 'duration_ms': dur or 0})
    out.sort(key=lambda r: r['ts'])
    _MONITOR_CACHE = out
    return out


def _window_events(t_start, t_end, source=None):
    """token_monitor rows within a unix-time window. Detector ('cmp') rows
    carry no session_id, but benchmark runs execute strictly sequentially, so
    window attribution is exact at run level and near-exact per turn. Other
    sources (summarizer, explorer sub-agents, memory) are session costs the
    llm_traces of the main agent do not include."""
    return [r for r in _monitor_rows()
            if t_start <= r['ts'] <= t_end and (source is None or r['source'] == source)]


_CMP_DETECT_RE = re.compile(
    r'CMP detect \[([^\]]+)\]: (\w+)(?: -> (\S+))? — .*? \| active=(\S+) \| msg: (.*)$')


def _load_cmp_log_entries(offset=0):
    """'CMP detect' verdict lines from logs/evonic.log starting at a byte
    offset (recorded by the runner at run start), in file order. The log has
    no timestamps, but each run's slice + in-order preview matching recovers
    the per-turn route decisions."""
    path = os.path.join(REPO_ROOT, 'logs', 'evonic.log')
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, errors='replace') as f:
        f.seek(offset)
        for line in f:
            m = _CMP_DETECT_RE.search(line)
            if m:
                entries.append({'layer': m.group(1), 'route': m.group(2),
                                'target': m.group(3), 'active': m.group(4),
                                'preview': m.group(5).rstrip()})
    return entries


def _align_routes(log_entries, turns):
    """Match each turn's 80-char message preview against the run's log slice,
    in order; returns {idx: (route, target)}."""
    routes = {}
    pos = 0
    for t in turns:
        preview = t['message'][:80].strip()
        found = None
        for j in range(pos, len(log_entries)):
            if log_entries[j]['preview'].startswith(preview[:60]):
                found = j
                break
        if found is not None:
            routes[t['idx']] = (log_entries[found]['route'],
                                log_entries[found]['target'])
            pos = found + 1
    return routes


def _session_cmp_state(agent, session_id):
    db = os.path.join(REPO_ROOT, 'agents', agent, 'chat.db')
    if not os.path.exists(db):
        return None
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        row = con.execute('SELECT content FROM session_state WHERE session_id=?',
                          (session_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def extract_run(raw_path):
    with open(raw_path) as f:
        run = json.load(f)
    agent, session_id = run['agent'], run['session_id']
    scenario = build(run['schedule'], run['seed'], salt=run.get('salt', ''))
    traces = _load_traces(agent, session_id)

    # group trace records by turn_index (1-based in traces; scenario idx is 0-based)
    by_turn = {}
    for rec in traces:
        by_turn.setdefault(rec.get('turn_index'), []).append(rec)

    # only CMP-enabled runs emit detector log lines; each run reads its own
    # log slice (byte offset recorded by the runner at run start)
    if agent == 'aisyah' and 'log_offset' in run:
        routes = _align_routes(_load_cmp_log_entries(run['log_offset']), run['turns'])
    else:
        routes = {}

    turn_rows = []
    prev_end = None
    for t in run['turns']:
        idx = t['idx']
        calls = by_turn.get(idx + 1, [])
        main = [c for c in calls if _classify_call(c) == 'main']
        # detector calls are not in llm_traces (they run through the classifier
        # client) — pull them from the token_monitor plugin DB by time window
        t_end = t['ts']
        t_start = prev_end if prev_end is not None else t_end - t['wall_s'] - 10
        prev_end = t_end
        # baseline sessions can't trigger the detector — any cmp-source event in
        # their window is boundary leakage from the preceding CMP run
        det = _window_events(t_start, t_end, 'cmp') if agent == 'aisyah' else []
        # auxiliary LLM costs not present in the main agent's llm_traces:
        # summarizer (baseline's rolling summary) and sub-agents (explorer)
        aux = [r for r in _window_events(t_start, t_end)
               if r['source'] in ('summarizer', 'explorer', 'memory')
               and (r['agent_id'] is None or str(r['agent_id']).startswith(agent))]

        def _usage(c, k):
            return (c.get('usage') or {}).get(k, 0) or 0

        row = {
            'run_key': run['run_key'], 'schedule': run['schedule'],
            'seed': run['seed'], 'agent': agent,
            'cmp': agent == 'aisyah', 'turn': idx,
            'task': t['task'], 'gt_route': t['gt_route'],
            'wall_s': t['wall_s'],
            'main_calls': len(main),
            'main_prompt_tokens_peak': max((_usage(c, 'prompt_tokens') for c in main), default=0),
            'main_prompt_tokens_sum': sum(_usage(c, 'prompt_tokens') for c in main),
            'main_completion_tokens': sum(_usage(c, 'completion_tokens') for c in main),
            'main_duration_ms': sum(c.get('duration_ms') or 0 for c in main),
            'det_calls': len(det),
            'det_prompt_tokens': sum(d['prompt_tokens'] for d in det),
            'det_completion_tokens': sum(d['completion_tokens'] for d in det),
            'det_duration_ms': sum(d['duration_ms'] for d in det),
            'det_models': ';'.join(sorted({d['model'] or '' for d in det})),
            'aux_calls': len(aux),
            'aux_tokens': sum(r['prompt_tokens'] + r['completion_tokens'] for r in aux),
            'aux_sources': ';'.join(sorted({r['source'] for r in aux})),
        }
        # invalid-action rate (model-degradation signal): tools the agent
        # emitted this turn and how many failed/were invalid.
        _tc, _terr = _turn_action_stats(main)
        row['tool_calls_emitted'] = _tc
        row['tool_errors'] = _terr
        row['invalid_action_rate'] = round(_terr / _tc, 4) if _tc else 0.0

        # probe scoring against ground truth — prefer the probe stored in the
        # raw record (self-contained, immune to scenario edits after the run);
        # fall back to rebuilding the scenario for older runs.
        probe = t.get('probe')
        if probe is None:
            probe = scenario['turns'][idx]['probe']
        if probe:
            s = score_probe(t['response'], probe['groups'])
            row.update({'probe_kind': probe['kind'],
                        'probe_topics_before': probe.get('topics_before'),
                        'probe_score': s['score'],
                        'probe_matched': s['matched'],
                        'probe_total': s['total'],
                        'probe_misses': ';'.join(s['misses'])})
        else:
            row.update({'probe_kind': '', 'probe_topics_before': None,
                        'probe_score': None, 'probe_matched': None,
                        'probe_total': None, 'probe_misses': ''})

        # detector route decision (CMP config only): from sequential log alignment
        route_info = routes.get(idx)
        row['det_route'] = route_info[0] if route_info else None
        row['det_target'] = route_info[1] if route_info else None
        turn_rows.append(row)

    # session-level rollup
    state = _session_cmp_state(agent, session_id) or {}
    cmp_state = state.get('cmp') or {}
    probe_rows = [r for r in turn_rows if r['probe_score'] is not None]
    n_probe_groups = sum(r['probe_total'] for r in probe_rows)
    n_probe_matched = sum(r['probe_matched'] for r in probe_rows)
    total_prompt = sum(r['main_prompt_tokens_sum'] + r['det_prompt_tokens'] for r in turn_rows)
    total_completion = sum(r['main_completion_tokens'] + r['det_completion_tokens'] for r in turn_rows)
    total_aux = sum(r['aux_tokens'] for r in turn_rows)

    summary = {
        'run_key': run['run_key'], 'schedule': run['schedule'], 'seed': run['seed'],
        'agent': agent, 'cmp': agent == 'aisyah',
        'session_id': session_id, 'n_turns': len(turn_rows),
        'llm_calls_total': sum(r['main_calls'] + r['det_calls'] for r in turn_rows),
        'mean_prompt_tokens': round(sum(r['main_prompt_tokens_peak'] for r in turn_rows)
                                    / max(len(turn_rows), 1)),
        'peak_prompt_tokens': max((r['main_prompt_tokens_peak'] for r in turn_rows), default=0),
        'total_prompt_tokens': total_prompt,
        'total_completion_tokens': total_completion,
        'aux_tokens_total': total_aux,
        'aux_sources': ';'.join(sorted({s for r in turn_rows
                                        for s in r['aux_sources'].split(';') if s})),
        'cost_usd': round(((total_prompt + total_aux * 0.8) * PRICE_IN_PER_M
                           + (total_completion + total_aux * 0.2) * PRICE_OUT_PER_M) / 1e6, 4),
        'mean_wall_s': round(sum(r['wall_s'] for r in turn_rows) / max(len(turn_rows), 1), 1),
        'probe_accuracy': round(n_probe_matched / n_probe_groups, 4) if n_probe_groups else None,
        'return_fidelity': _kind_acc(probe_rows, 'return_fidelity'),
        'dependency_fidelity': _kind_acc(probe_rows, 'dependency'),
        'session_end_fidelity': _kind_acc(probe_rows, 'session_end'),
        'task_completion': _kind_acc(probe_rows, 'task_completion'),
        'niah_fidelity': _kind_acc(probe_rows, 'niah'),
        'deep_ancestor_fidelity': _kind_acc(probe_rows, 'deep_ancestor'),
        'sibling_fidelity': _kind_acc(probe_rows, 'sibling'),
        'checkpoint_fidelity': _kind_acc(probe_rows, 'checkpoint'),
        'multi_project_fidelity': _kind_acc(probe_rows, 'multi_project'),
        'followup_persist_fidelity': _kind_acc(probe_rows, 'followup_persist'),
        'invalid_action_rate': (sum(r['tool_errors'] for r in turn_rows)
                                / max(sum(r['tool_calls_emitted'] for r in turn_rows), 1)),
        'det_calls_total': sum(r['det_calls'] for r in turn_rows),
        'det_tokens_total': sum(r['det_prompt_tokens'] + r['det_completion_tokens']
                                for r in turn_rows),
        'det_duration_ms_total': sum(r['det_duration_ms'] for r in turn_rows),
        'cmp_paths': len(cmp_state.get('paths', {})) or None,
        'cmp_switches': (cmp_state.get('stats') or {}).get('switches'),
        'cmp_branches': (cmp_state.get('stats') or {}).get('branches'),
        'cmp_detector_llm_calls': (cmp_state.get('stats') or {}).get('detector_llm_calls'),
        'det_models': ';'.join(sorted({m for r in turn_rows
                                       for m in r['det_models'].split(';') if m})),
    }
    return turn_rows, summary


def _kind_acc(probe_rows, kind):
    rows = [r for r in probe_rows if r['probe_kind'] == kind]
    total = sum(r['probe_total'] for r in rows)
    if not total:
        return None
    return round(sum(r['probe_matched'] for r in rows) / total, 4)


def main(run_id):
    raw_files = sorted(glob.glob(os.path.join(RESULTS_DIR, run_id, 'raw', '*.json')))
    if not raw_files:
        raise SystemExit(f"no raw runs under results/{run_id}/raw")
    out_dir = os.path.join(RESULTS_DIR, run_id, 'metrics')
    os.makedirs(out_dir, exist_ok=True)

    all_rows, summaries = [], []
    for path in raw_files:
        rows, summary = extract_run(path)
        all_rows.extend(rows)
        summaries.append(summary)
        with open(os.path.join(out_dir, f"{summary['run_key']}.json"), 'w') as f:
            json.dump({'summary': summary, 'turns': rows}, f, indent=1)
        print(f"{summary['run_key']}: probe_acc={summary['probe_accuracy']} "
              f"mean_ctx={summary['mean_prompt_tokens']} peak={summary['peak_prompt_tokens']} "
              f"det_calls={summary['det_calls_total']}")

    with open(os.path.join(out_dir, 'turns.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    with open(os.path.join(out_dir, 'summary.json'), 'w') as f:
        json.dump(summaries, f, indent=1)
    print(f"wrote {len(all_rows)} turn rows, {len(summaries)} run summaries -> {out_dir}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'r1')
