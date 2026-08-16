"""De-risking spike: can we build a CMP path graph by INJECTING a prior
conversation (both user and assistant turns) offline, without the agent
regenerating replies? This is the core feasibility gate for LongMemEval.

Injects 5 synthetic turns spanning 3 distinct tasks (CSV parser -> Bali trip ->
back to CSV) and drives cmp.on_turn_boundary per user turn, then persists and
verifies the graph. If CMP builds >=2 paths with a switch/return detected, the
LongMemEval ingestion path is viable.
"""

import os
import sqlite3
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, REPO)

from models.db import db                                        # noqa: E402
from models.chatlog import chatlog_manager                      # noqa: E402
from backend.agent_state import AgentState                      # noqa: E402
from backend.agent_runtime import cmp as cmp_pkg                # noqa: E402
from backend.agent_runtime.llm_loop import _persist_agent_state_split  # noqa: E402

AGENT = 'aisyah'
SID = 'aisyah-lmespike01'

TURNS = [
    ("Help me write a Python function to parse a CSV file into dicts.",
     "Sure. Here's parse_csv(path) using the csv.DictReader from the standard library, returning a list of row dicts."),
    ("Add error handling for a missing file.",
     "Done. I wrapped the open() in a try/except FileNotFoundError that raises a clear message."),
    ("Different topic now: help me plan a 5 day trip to Bali next month.",
     "Great. For 5 days in Bali I'd split it: 2 days Ubud, 2 days Uluwatu, 1 day Nusa Penida."),
    ("What are good hotels in Ubud for that trip?",
     "In Ubud, consider Bisma Eight or Komaneka at Bisma; both are central and well reviewed."),
    ("Back to the CSV parser: add unit tests with pytest.",
     "Here are pytest tests covering a normal file, an empty file, and the missing-file error path."),
]


def _clean():
    p = os.path.join(REPO, 'agents', AGENT, 'sessions', f'{SID}.jsonl')
    if os.path.exists(p):
        os.remove(p)
    dbp = os.path.join(REPO, 'agents', AGENT, 'chat.db')
    if os.path.exists(dbp):
        con = sqlite3.connect(dbp, timeout=30)
        try:
            for t in ('session_state', 'chat_messages', 'chat_sessions'):
                try:
                    con.execute(f"DELETE FROM {t} WHERE session_id=?", (SID,))
                except sqlite3.OperationalError:
                    pass
            con.commit()
        finally:
            con.close()


def main():
    _clean()
    agent = db.get_agent(AGENT)
    assert agent.get('enable_cmp'), "aisyah must have enable_cmp=1"
    chatlog = chatlog_manager.get(AGENT, SID)
    ms = AgentState()
    now = int(time.time() * 1000)

    for i, (u, a) in enumerate(TURNS):
        chatlog.append({'type': 'user', 'session_id': SID, 'content': u,
                        'sender_id': 'test', 'metadata': {}, 'ts': now + i * 2000})
        t0 = time.time()
        dec = cmp_pkg.on_turn_boundary(agent, ms, chatlog, u)
        chatlog.append({'type': 'final', 'session_id': SID, 'content': a,
                        'ts': now + i * 2000 + 1000})
        d = (dec or {}).get('decision')
        tgt = (dec or {}).get('target')
        active = (ms.cmp or {}).get('active_id')
        print(f"turn {i+1}: decision={d} target={tgt} active={active} "
              f"paths={len((ms.cmp or {}).get('paths', {}))} ({time.time()-t0:.1f}s)")

    _persist_agent_state_split(ms, AGENT, SID)

    paths = (ms.cmp or {}).get('paths', {})
    print(f"\n=== CMP graph built: {len(paths)} paths ===")
    for pid, p in paths.items():
        print(f"  {pid}: title={p.get('title')!r} depends_on={p.get('depends_on')} "
              f"facts={len((p.get('card') or {}).get('key_facts') or [])}")

    import json
    ss = db.get_session_state(SID, agent_id=AGENT)
    saved = json.loads(ss) if ss else {}
    n_persisted = len((saved.get('cmp') or {}).get('paths', {}))
    print(f"\npersisted to session_state: {n_persisted} paths")
    ok = len(paths) >= 2 and n_persisted >= 2
    print(f"\nSPIKE {'PASS' if ok else 'FAIL'}: CMP graph "
          f"{'builds + persists from injected history' if ok else 'did NOT build as expected'}")


if __name__ == '__main__':
    main()
