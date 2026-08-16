"""Benchmark runner: executes scripted scenarios against Evonic agents.

Each run = (schedule, seed, agent). A fresh user_id per run gives a fresh
server-side session; conversation state lives on the server, so re-invoking
with the same --run-id resumes any partially completed run (turns already
answered are skipped).

Usage:
  python runner.py --run-id r1 [--schedules s1_sequential,s2_single_return]
                   [--seeds 1,2] [--agents aisyah,aisyah_base]
                   [--max-turns N]   # smoke-testing aid
"""

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import EvonicClient, REPO_ROOT  # noqa: E402
from scenarios import SCHEDULES, build      # noqa: E402
from scoring import score_probe             # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')

DETECTOR_MARK = 'task-path map of a multi-task session'


_ARMS = {'aisyah': 'CMP', 'aisyah_full': 'Full history',
         'aisyah_base': 'Windowed (50-msg)'}


def _arm_of(agent):
    return _ARMS.get(agent, 'Without CMP')


# Strong context-ceiling error signatures a llama.cpp/OpenAI endpoint returns
# when the prompt exceeds n_ctx (as opposed to prose that merely says "context").
_CEILING_SIGS = ('exceed the available context', 'exceeds context',
                 'context length', 'context window', 'maximum context',
                 'n_ctx', 'context size', 'too many tokens', 'kv cache',
                 'requested tokens', 'out of memory', 'prompt is too long')


def _looks_like_ceiling(resp):
    """True when a reply is empty or carries a context-overflow error — the
    full-history arm's hard-limit signature at the model's context ceiling."""
    if resp is None or not resp.strip():
        return True
    low = resp.lower()
    return any(s in low for s in _CEILING_SIGS)


def _live_context(agent, session_id, turn_index):
    """Best-effort read of the main-agent prompt-token peak for a turn straight
    from the session's llm_trace file, so the progress log carries context live
    (no post-hoc metrics pass needed to replay the chart). Detector calls run
    through the classifier client and aren't in llm_traces, but we still skip
    any trace whose system prompt carries the detector marker, to be safe.
    Returns the peak prompt_tokens for turn_index (1-based) or None."""
    if not session_id:
        return None
    short = session_id.split('-')[-1]
    path = os.path.join(REPO_ROOT, 'agents', agent, 'llm_traces', f'{short}.jsonl')
    if not os.path.exists(path):
        return None
    peak = 0
    try:
        with open(path) as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('turn_index') != turn_index:
                    continue
                msgs = (rec.get('request') or {}).get('messages', [])
                sysmsg = next((m.get('content') or '' for m in msgs
                               if m.get('role') == 'system'), '')
                if DETECTOR_MARK in sysmsg:
                    continue
                pt = (rec.get('usage') or {}).get('prompt_tokens') or 0
                peak = max(peak, pt)
    except OSError:
        return None
    return peak or None


def _slug_session_id(agent_id, user_id):
    """The deterministic session id the server derives for (agent, user).
    Mirrors models.chatlog.session_slug."""
    import hashlib
    items = sorted([user_id or '', agent_id or ''])
    h = hashlib.sha1(json.dumps(items).encode()).hexdigest()[:8]
    return f"{agent_id}-{h}"


def _hard_reset_session(agent_id, user_id):
    """Fully wipe any prior server-side state for this (agent, user) so a run
    starts truly clean. The platform's delete only archives the row and leaves
    session_state (the CMP path graph) behind, and get_or_create_session
    reuses archived sessions — so the CMP graph would otherwise persist across
    runs sharing the deterministic session slug. We clear it directly in the
    per-agent chat.db (WAL, so a brief writer alongside the live server is
    safe when no turn for this session is in flight)."""
    sid = _slug_session_id(agent_id, user_id)
    db = os.path.join(REPO_ROOT, 'agents', agent_id, 'chat.db')
    if not os.path.exists(db):
        return sid
    con = sqlite3.connect(db, timeout=30)
    try:
        con.execute('PRAGMA busy_timeout=30000')
        for tbl in ('chat_messages', 'chat_summaries', 'session_state', 'agent_state'):
            try:
                con.execute(f"DELETE FROM {tbl} WHERE session_id=?", (sid,))
            except sqlite3.OperationalError:
                pass  # table may lack session_id column (agent_state global rows)
        con.execute("DELETE FROM chat_sessions WHERE id=?", (sid,))
        con.commit()
    finally:
        con.close()
    return sid


def _wipe_agent_memory(agent_id):
    """Delete the agent's long-term memory (kb markdown + evomem index) so a
    fresh run cannot answer probes from a prior run's saved facts. evomem is
    invoked per-call as a subprocess (no persistent handle), so removing
    .evomem.db between runs is safe — the next call re-creates it."""
    import glob
    kb = os.path.join(REPO_ROOT, 'agents', agent_id, 'kb')
    if not os.path.isdir(kb):
        return
    for path in glob.glob(os.path.join(kb, '*.md')):
        try:
            os.remove(path)
        except OSError:
            pass
    for path in glob.glob(os.path.join(kb, '.evomem.db*')):
        try:
            os.remove(path)
        except OSError:
            pass


def _session_id_for(agent_id, user_id):
    db = os.path.join(REPO_ROOT, 'shared', 'db', 'evonic.db')
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        row = con.execute(
            'SELECT session_id FROM session_index WHERE agent_id=? AND external_user_id=?',
            (agent_id, user_id)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def run_one(client, run_id, schedule, seed, agent, max_turns=None, pause=1.0,
            endurance=False, stop_after_zeros=3, latency_ceiling=None):
    scenario = build(schedule, seed, salt=run_id)
    run_key = f"{schedule}_seed{seed}_{agent}"
    user_id = f"cmpb-{run_id}-{schedule}-{seed}-{agent}"
    out_dir = os.path.join(RESULTS_DIR, run_id, 'raw')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{run_key}.json")

    if os.path.exists(out_path):
        with open(out_path) as f:
            record = json.load(f)
    else:
        # Fresh run for this cell: the session slug is deterministic, so any
        # prior run's session (and its leaked CMP path graph in session_state)
        # would be reused. Hard-reset it so path ids start at A1.
        stale = _session_id_for(agent, user_id)
        if stale:
            client._request('DELETE', f'/api/sessions/{stale}')
        wiped = _hard_reset_session(agent, user_id)
        _wipe_agent_memory(agent)
        print(f"[{run_key}] hard-reset session {wiped} + wiped kb/evomem")
        log_path = os.path.join(REPO_ROOT, 'logs', 'evonic.log')
        record = {'run_id': run_id, 'run_key': run_key, 'schedule': schedule,
                  'seed': seed, 'salt': run_id, 'agent': agent, 'user_id': user_id,
                  'session_id': None, 'n_turns_planned': scenario['n_turns'],
                  'log_offset': os.path.getsize(log_path) if os.path.exists(log_path) else 0,
                  'turns': []}

    done = len(record['turns'])
    turns = scenario['turns']
    if max_turns is not None:
        turns = turns[:max_turns]
    if done >= len(turns):
        print(f"[{run_key}] already complete ({done} turns)")
        return record

    # Per-turn progress log (append-only jsonl) so the chart's build-up can be
    # replayed frame-by-frame later. One line per completed turn with the
    # metrics available live: context (from traces), live probe score, running
    # accuracy, and any hard failure.
    prog_dir = os.path.join(RESULTS_DIR, run_id, 'progress')
    os.makedirs(prog_dir, exist_ok=True)
    prog_path = os.path.join(prog_dir, f"{run_key}.jsonl")
    if not done and os.path.exists(prog_path):
        os.remove(prog_path)   # fresh cell → fresh progress log
    prog = open(prog_path, 'a')
    arm = _arm_of(agent)
    probe_hist = [r.get('probe_score') for r in record['turns']
                  if r.get('probe_score') is not None]
    consec_zero = 0

    print(f"[{run_key}] {'resuming at' if done else 'starting'} turn {done + 1}/{len(turns)}"
          f"{' [endurance]' if endurance else ''}")
    hard_fail = None
    for t in turns[done:]:
        t0 = time.time()
        try:
            reply = client.chat_and_wait(agent, t['message'], user_id)
        except TimeoutError as e:
            # No reply within the timeout — in an endurance run this is the
            # context-ceiling signature (the turn can no longer be served).
            hard_fail = str(e)[:200]
            print(f"[{run_key}] HARD FAILURE at turn {t['idx'] + 1}: {hard_fail}")
            if endurance:
                break
            raise
        if record['session_id'] is None:
            record['session_id'] = _session_id_for(agent, user_id)

        # Context ceiling: the turn returned but with an empty/overflow-error
        # reply — full history can no longer be served. Record it and stop.
        if endurance and _looks_like_ceiling(reply['response']):
            hard_fail = f"context ceiling at turn {t['idx'] + 1}: {reply['response'][:120]!r}"
            print(f"[{run_key}] HARD FAILURE — {hard_fail}")
            break

        # Latency ceiling: prompt-processing cost grows with context, so full
        # history's per-turn time explodes as it accumulates. Once a turn blows
        # past the ceiling the arm is operationally intractable — declare the
        # wall here instead of grinding through 30-45 min turns to a timeout.
        if endurance and latency_ceiling and reply['wall_s'] > latency_ceiling:
            hard_fail = (f"latency ceiling at turn {t['idx'] + 1}: "
                         f"{reply['wall_s']:.0f}s > {latency_ceiling}s "
                         f"(ctx≈{_live_context(agent, record['session_id'], t['idx'] + 1) or '?'})")
            print(f"[{run_key}] HARD FAILURE — {hard_fail}")
            break

        # live probe score. Every probe (incl. s8's dependency/sibling/NIAH
        # kinds in the hybrid) is scored and logged, but only the sparse
        # 'lookup' reminders drive the accuracy-wall stop — those are the
        # fixed-recency needles that isolate context-size degradation.
        probe_score = probe_age = probe_kind = None
        if t['probe'] and t['probe'].get('groups'):
            sc = score_probe(reply['response'], t['probe']['groups'])
            probe_score = sc['score']
            probe_age = t['probe'].get('age')
            probe_kind = t['probe'].get('kind')
            if probe_kind == 'lookup':
                probe_hist.append(probe_score)
                consec_zero = consec_zero + 1 if probe_score == 0 else 0

        turn_rec = {
            'idx': t['idx'],
            'task': t['task'],
            'gt_route': t['gt_route'],
            'gt_target': t['gt_target'],
            'has_probe': bool(t['probe']),
            'probe': t['probe'],
            'probe_score': probe_score,
            'message': t['message'],
            'response': reply['response'],
            'wall_s': round(reply['wall_s'], 2),
            'ts': time.time(),
        }
        record['turns'].append(turn_rec)
        with open(out_path, 'w') as f:
            json.dump(record, f, indent=1)

        ctx = _live_context(agent, record['session_id'], t['idx'] + 1)
        cum = round(sum(probe_hist) / len(probe_hist), 4) if probe_hist else None
        prog.write(json.dumps({
            'turn': t['idx'] + 1, 'agent': agent, 'arm': arm,
            'task': t['task'],
            'kind': (t['probe'] or {}).get('kind') if t['probe'] else None,
            'ctx': ctx,
            'probe_score': probe_score, 'probe_age': probe_age,
            'cum_acc': cum, 'consec_zero': consec_zero,
            'wall_s': round(reply['wall_s'], 2),
            'hard_fail': False, 'ts': turn_rec['ts'],
        }) + '\n')
        prog.flush()

        pline = (f" probe={probe_score:.2f}(age {probe_age},z{consec_zero})"
                 if probe_score is not None else "")
        print(f"[{run_key}] turn {t['idx'] + 1}/{len(turns)} "
              f"({reply['wall_s']:.0f}s, ctx={ctx or '?'}{pline})")

        if endurance and consec_zero >= stop_after_zeros:
            print(f"[{run_key}] STOP: {consec_zero} consecutive zero-accuracy "
                  f"lookups — accuracy wall reached at turn {t['idx'] + 1}")
            break
        time.sleep(pause)

    if hard_fail:
        prog.write(json.dumps({'turn': None, 'agent': agent, 'arm': arm,
                               'hard_fail': True, 'reason': hard_fail,
                               'ts': time.time()}) + '\n')
    prog.flush()
    prog.close()
    print(f"[{run_key}] complete — session {record['session_id']} "
          f"({len(record['turns'])} turns"
          f"{', HARD FAIL' if hard_fail else ''})")
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--schedules', default=','.join(SCHEDULES))
    ap.add_argument('--seeds', default='1,2')
    ap.add_argument('--agents', default='aisyah,aisyah_base')
    ap.add_argument('--max-turns', type=int, default=None)
    ap.add_argument('--endurance', action='store_true',
                    help='run-to-failure: live-score lookups, stop a cell on '
                         '--stop-after-zeros consecutive zero-accuracy lookups '
                         'or a hard timeout (context ceiling)')
    ap.add_argument('--stop-after-zeros', type=int, default=3)
    ap.add_argument('--latency-ceiling', type=float, default=None,
                    help='endurance: hard-fail an arm when a turn exceeds this '
                         'many seconds (full-history latency wall)')
    args = ap.parse_args()

    schedules = [s.strip() for s in args.schedules.split(',') if s.strip()]
    seeds = [int(s) for s in args.seeds.split(',')]
    agents = [a.strip() for a in args.agents.split(',') if a.strip()]

    client = EvonicClient()
    t0 = time.time()
    plan = [(sch, seed, agent) for sch in schedules for seed in seeds for agent in agents]
    print(f"run {args.run_id}: {len(plan)} sessions")
    for i, (sch, seed, agent) in enumerate(plan, 1):
        print(f"=== [{i}/{len(plan)}] {sch} seed={seed} agent={agent} "
              f"(elapsed {int(time.time() - t0)}s) ===")
        run_one(client, args.run_id, sch, seed, agent, max_turns=args.max_turns,
                endurance=args.endurance, stop_after_zeros=args.stop_after_zeros,
                latency_ceiling=args.latency_ceiling)
    print(f"all done in {int(time.time() - t0)}s")


if __name__ == '__main__':
    main()
