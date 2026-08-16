"""Densify the recall-vs-age curve by CONTINUING an existing session.

Rather than re-run from turn 0, this reconnects to the CMP session that r11
already built (same agent+user_id, NO reset) and fires one lookup per unique
landmark that was actually planted (scenario turn <= turns actually executed).
Each probe is scored live; age = current session turn - planted turn. Probing
recent-first spreads ages from a few turns to the full session depth, giving
a dense accuracy-vs-distance curve on the mature graph.

Appends to results/<run>/accuracy_continue.jsonl (one line per probe).

Usage: python probe_continue.py [run_id=r11] [agent=aisyah] [limit=all]
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import EvonicClient          # noqa: E402
from scenarios import build              # noqa: E402
from scoring import score_probe          # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else 'r11'
    agent = sys.argv[2] if len(sys.argv) > 2 else 'aisyah'
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None

    raw_path = os.path.join(RESULTS_DIR, run_id, 'raw',
                            f's10_hybrid_seed1_{agent}.json')
    raw = json.load(open(raw_path))
    user_id = raw['user_id']
    base = len(raw['turns'])            # turns actually executed in the session

    sc = build('s10_hybrid', 1, salt=run_id)
    # only landmarks that were actually planted (planted turn within executed run)
    lms = [l for l in sc['facts']['landmarks'] if l[0] < base]
    lms.sort(key=lambda l: -l[0])       # recent-first → ages span young..deep
    if limit:
        lms = lms[:limit]

    out_path = os.path.join(RESULTS_DIR, run_id, f'accuracy_continue_{agent}.jsonl')
    out = open(out_path, 'a')
    client = EvonicClient()
    print(f"continuing session for {agent} (user={user_id}, base turn={base}); "
          f"{len(lms)} landmark probes")

    for k, (pturn, cn, thing, value) in enumerate(lms):
        session_turn = base + 1 + k
        age = session_turn - pturn
        msg = f"Reminder lookup — what was the {thing} for the {cn} project?"
        try:
            reply = client.chat_and_wait(agent, msg, user_id)
        except Exception as e:
            print(f"  probe {k+1}/{len(lms)} age={age} {cn}/{thing} ERROR {str(e)[:60]}")
            out.write(json.dumps({'seq': k, 'session_turn': session_turn,
                                  'planted_turn': pturn, 'age': age, 'codename': cn,
                                  'thing': thing, 'want': value, 'score': None,
                                  'error': str(e)[:120]}) + '\n')
            out.flush()
            continue
        s = score_probe(reply['response'], [{'name': f'{cn}_{thing}', 'any_of': [value]}])
        out.write(json.dumps({'seq': k, 'session_turn': session_turn,
                              'planted_turn': pturn, 'age': age, 'codename': cn,
                              'thing': thing, 'want': value, 'score': s['score'],
                              'resp': reply['response'][:200],
                              'wall_s': round(reply['wall_s'], 1)}) + '\n')
        out.flush()
        print(f"  probe {k+1}/{len(lms)} age={age:>3} {cn}/{thing} "
              f"score={s['score']} ({reply['wall_s']:.0f}s)")
        time.sleep(1.0)

    out.close()
    print(f"done — wrote {out_path}")


if __name__ == '__main__':
    main()
