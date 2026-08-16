"""Run a LongMemEval subset across the four arms.

For each (question, arm): inject the haystack into a fresh per-arm session
(ingest.py), ask the question via the chat API, and save the answer as the arm's
hypothesis. Full history on _s is expected to hit the latency wall; those turns
are recorded as failures (empty hypothesis) rather than waited on forever.

Usage:
  python run.py --run-id lme1 --variant s --limit 40 [--arms CMP,Windowed,...]
Outputs results/<run-id>/hypotheses_<arm>.jsonl : {question_id, question_type,
hypothesis, wall_s, n_paths}
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'cmp'))
import ingest                                    # noqa: E402  (also preloads SECRET_KEY)
from client import EvonicClient                  # noqa: E402

RESULTS = os.path.join(HERE, 'results')

ARMS = {
    'CMP':          ('aisyah',      dict(build_cmp=True)),
    'Windowed':     ('aisyah_base', dict()),
    'Summary+tail': ('aisyah_sum',  dict(build_summary=True)),
    'Full history': ('aisyah_full', dict()),
}


def pick_subset(data, limit):
    """Balanced subset across question types, deterministic (dataset order)."""
    from collections import defaultdict, OrderedDict
    by = defaultdict(list)
    for q in data:
        by[q['question_type']].append(q)
    types = list(by)
    out, i = OrderedDict(), 0
    while len(out) < limit and any(by[t] for t in types):
        t = types[i % len(types)]
        if by[t]:
            q = by[t].pop(0)
            out[q['question_id']] = q
        i += 1
    return list(out.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--variant', default='s', choices=['s', 'oracle'])
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--arms', default=','.join(ARMS))
    ap.add_argument('--fh-timeout', type=int, default=600,
                    help='full-history per-question timeout (latency wall)')
    ap.add_argument('--timeout', type=int, default=400)
    args = ap.parse_args()

    fname = 'longmemeval_s.json' if args.variant == 's' else 'longmemeval_oracle.json'
    data = json.load(open(os.path.join(HERE, 'data', fname)))
    subset = pick_subset(data, args.limit)
    arms = [a.strip() for a in args.arms.split(',') if a.strip()]
    out_dir = os.path.join(RESULTS, args.run_id)
    os.makedirs(out_dir, exist_ok=True)
    client = EvonicClient()
    print(f"run {args.run_id}: {len(subset)} questions x {len(arms)} arms "
          f"(variant={args.variant})")

    for arm in arms:
        agent, kw = ARMS[arm]
        out_path = os.path.join(out_dir, f"hypotheses_{agent}.jsonl")
        done = set()
        if os.path.exists(out_path):
            done = {json.loads(l)['question_id'] for l in open(out_path) if l.strip()}
        fh = open(out_path, 'a')
        tmo = args.fh_timeout if arm == 'Full history' else args.timeout
        print(f"\n=== arm {arm} ({agent}) — {len(done)} done, timeout {tmo}s ===")
        for k, q in enumerate(subset):
            if q['question_id'] in done:
                continue
            uid = f"lme-{args.run_id}-{q['question_id']}-{agent}"
            t0 = time.time()
            try:
                _, npaths = ingest.ingest(agent, uid, q,
                                          build_cmp=kw.get('build_cmp', False),
                                          build_summary=kw.get('build_summary', False))
            except Exception as e:
                print(f"  [{k+1}] {q['question_id']} INGEST ERROR {str(e)[:80]}")
                continue
            try:
                reply = client.chat_and_wait(agent, q['question'], uid, timeout=tmo)
                hyp = reply['response']
                wall = round(reply['wall_s'], 1)
            except Exception as e:
                hyp, wall = '', -1.0
                print(f"  [{k+1}] {q['question_id']} ANSWER FAIL ({str(e)[:50]})")
            fh.write(json.dumps({'question_id': q['question_id'],
                                 'question_type': q['question_type'],
                                 'hypothesis': hyp, 'wall_s': wall,
                                 'n_paths': npaths}) + '\n')
            fh.flush()
            print(f"  [{k+1}/{len(subset)}] {q['question_type'][:14]:<14} "
                  f"{wall}s paths={npaths} : {hyp[:70].strip()}")
        fh.close()
    print("\nall arms done")


if __name__ == '__main__':
    main()
