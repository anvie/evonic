"""End-to-end recall spike: inject a fact-bearing multi-task history, build the
CMP graph with the deepseek detector (reliable JSON -> card facts captured), then
ask a question whose answer lives in an EARLIER (non-active) task and verify:
  1. the fact-bearing path's card key_facts captured the planted fact
  2. render_cmp_section (the memory the agent sees) contains the fact
  3. the Gemma agent, given only that CMP memory, answers with the fact

Detector = deepseek-v4-flash (cmp_model_id). Answer model = Gemma4-12B.
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
from backend.llm_client import LLMClient                        # noqa: E402

AGENT = 'aisyah'
SID = 'aisyah-lmerecall01'
FACT = 'AKIA-7729'

TURNS = [
    ("For the production deploy, note my AWS access key is AKIA-7729. Keep it for later.",
     "Noted: your production AWS access key is AKIA-7729. I'll keep it in mind for the deploy."),
    ("Good. Also remind me it's tied to the us-east-1 region.",
     "Got it, AKIA-7729 is tied to us-east-1 for the production deploy."),
    ("Different topic: help me plan a 5 day trip to Bali next month.",
     "For 5 days in Bali: 2 days Ubud, 2 days Uluwatu, 1 day Nusa Penida."),
    ("What hotels do you suggest in Ubud?",
     "In Ubud, consider Bisma Eight or Komaneka at Bisma, both central."),
    ("Now help me write a Python function to parse a CSV into dicts.",
     "Here's parse_csv(path) using csv.DictReader, returning a list of row dicts."),
    ("Add pytest unit tests for it.",
     "Added pytest tests for a normal file, an empty file, and a missing file."),
]
QUESTION = "Remind me: what was my production AWS access key?"


def _clean():
    p = os.path.join(REPO, 'agents', AGENT, 'sessions', f'{SID}.jsonl')
    if os.path.exists(p):
        os.remove(p)
    dbp = os.path.join(REPO, 'agents', AGENT, 'chat.db')
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


def _ingest(agent, chatlog, ms):
    now = int(time.time() * 1000)
    for i, (u, a) in enumerate(TURNS):
        chatlog.append({'type': 'user', 'session_id': SID, 'content': u,
                        'sender_id': 'test', 'metadata': {}, 'ts': now + i * 2000})
        t0 = time.time()
        dec = cmp_pkg.on_turn_boundary(agent, ms, chatlog, u)
        chatlog.append({'type': 'final', 'session_id': SID, 'content': a,
                        'ts': now + i * 2000 + 1000})
        print(f"  ingest turn {i+1}: {(dec or {}).get('decision')} -> "
              f"{(dec or {}).get('target')} ({time.time()-t0:.1f}s)")


def main():
    print(f"detector model (cmp_model_id): {db.get_setting('cmp_model_id')}")
    _clean()
    agent = db.get_agent(AGENT)
    chatlog = chatlog_manager.get(AGENT, SID)
    ms = AgentState()

    print("\n=== ingesting fact-bearing history ===")
    _ingest(agent, chatlog, ms)

    # the question turn: let CMP route/pin the fact-bearing path
    print("\n=== asking question (CMP boundary) ===")
    now = int(time.time() * 1000)
    chatlog.append({'type': 'user', 'session_id': SID, 'content': QUESTION,
                    'sender_id': 'test', 'metadata': {}, 'ts': now})
    dec = cmp_pkg.on_turn_boundary(agent, ms, chatlog, QUESTION)
    print(f"  question routed: {(dec or {}).get('decision')} -> {(dec or {}).get('target')}")
    _persist_agent_state_split(ms, AGENT, SID)

    # 1. card key_facts
    print("\n=== 1. card key_facts per path ===")
    fact_in_card = False
    for pid, p in (ms.cmp or {}).get('paths', {}).items():
        kf = (p.get('card') or {}).get('key_facts') or []
        print(f"  {pid}: {p.get('title')!r} key_facts={kf}")
        if any(FACT in str(x) for x in kf):
            fact_in_card = True

    # 2. rendered memory the agent sees
    section = cmp_pkg.render_cmp_section(ms.cmp, 'Aisyah')
    fact_in_render = FACT in section
    print(f"\n=== 2. fact '{FACT}' in rendered CMP memory: {fact_in_render} ===")

    # 3. Gemma answers using ONLY the CMP memory
    print("\n=== 3. Gemma answer given CMP memory ===")
    gclient = LLMClient(db.get_model_by_id('llama.cpp/Gemma4-12B'))
    sysmsg = ("You are Aisyah. Answer the user's question using the task memory "
              "below. Be concise.\n\n" + section)
    resp = gclient.chat_completion(
        [{'role': 'system', 'content': sysmsg},
         {'role': 'user', 'content': QUESTION}],
        enable_thinking=False, max_tokens=200)
    try:
        answer = resp['response']['choices'][0]['message']['content'] or ''
    except Exception:
        answer = str(resp)[:300]
    print(f"  Q: {QUESTION}")
    print(f"  A: {answer.strip()[:300]}")
    answered = FACT in answer

    print("\n=== VERDICT ===")
    print(f"  fact captured in card:   {fact_in_card}")
    print(f"  fact in rendered memory: {fact_in_render}")
    print(f"  agent recalled the fact: {answered}")
    print(f"\n  SPIKE {'PASS' if answered else 'PARTIAL/FAIL'}: CMP "
          f"{'recalls the injected fact end-to-end' if answered else 'did not surface the fact in the answer'}")


if __name__ == '__main__':
    main()
