"""Measure per-session CMP ingestion cost + recall on one full _s question."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cmp'))
import ingest
from client import EvonicClient

data = json.load(open(os.path.join(os.path.dirname(__file__), 'data', 'longmemeval_s.json')))
# pick a multi-session question (needs cross-session recall)
q = next(x for x in data if x['question_type'] == 'multi-session')
print(f"Q ({q['question_type']}): {q['question']}")
print(f"gold: {q['answer']}")
print(f"sessions: {len(q['haystack_sessions'])}  "
      f"~tokens: {sum(len(t.get('content','')) for s in q['haystack_sessions'] for t in s)//4}")

t0 = time.time()
sid, npaths = ingest.ingest('aisyah', 'lme-s-2', q, build_cmp=True)
print(f"direct CMP ingest: {npaths} paths in {time.time()-t0:.1f}s")

c = EvonicClient()
r = c.chat_and_wait('aisyah', q['question'], 'lme-s-1', timeout=600)
print(f"CMP answer: {r['response'][:400].strip()}")
print(f"question wall: {r['wall_s']:.0f}s")
