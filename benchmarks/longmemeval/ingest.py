"""Inject a LongMemEval question's haystack into a fresh Evonic session so each
arm builds its own memory, WITHOUT the agent regenerating replies.

- Windowed / Full history: just write the transcript to the chatlog; the server's
  context assembly reads it at question time.
- CMP: additionally drive cmp.on_turn_boundary so the path graph + cards build.
- Summary+tail: additionally run the summarizer so a rolling summary exists.

The question itself is asked separately (run.py) via the chat API on the same
session, so each arm answers through the real Evonic context pipeline.
"""

import hashlib
import json
import os
import sqlite3
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
import sys
sys.path.insert(0, REPO)

# Load SECRET_KEY from .env into the environment BEFORE importing config/backend,
# so config.py does not think it is missing and generate+append a NEW key to
# .env (which would desync this process from the running server -> 401).
if 'SECRET_KEY' not in os.environ:
    _envp = os.path.join(REPO, '.env')
    if os.path.exists(_envp):
        for _l in open(_envp):
            if _l.strip().startswith('SECRET_KEY='):
                os.environ['SECRET_KEY'] = _l.strip().split('=', 1)[1].strip().strip('"').strip("'")

from models.db import db                                        # noqa: E402
from models.chatlog import chatlog_manager                      # noqa: E402
from backend.agent_state import AgentState                      # noqa: E402


def slug(agent_id, user_id):
    items = sorted([user_id or '', agent_id or ''])
    h = hashlib.sha1(json.dumps(items).encode()).hexdigest()[:8]
    return f"{agent_id}-{h}"


def clean(agent_id, session_id):
    p = os.path.join(REPO, 'agents', agent_id, 'sessions', f'{session_id}.jsonl')
    if os.path.exists(p):
        os.remove(p)
    dbp = os.path.join(REPO, 'agents', agent_id, 'chat.db')
    if os.path.exists(dbp):
        con = sqlite3.connect(dbp, timeout=30)
        try:
            for t in ('session_state', 'chat_messages', 'chat_summaries', 'chat_sessions'):
                try:
                    con.execute(f"DELETE FROM {t} WHERE session_id=?", (session_id,))
                except sqlite3.OperationalError:
                    pass
            con.commit()
        finally:
            con.close()


def _append(chatlog, sid, role, content, ts):
    typ = 'user' if role == 'user' else 'final'
    e = {'type': typ, 'session_id': sid, 'content': content, 'ts': ts}
    if typ == 'user':
        e['sender_id'] = 'lme'
        e['metadata'] = {}
    chatlog.append(e)


def _session_title(sess, date, i):
    """A topic label for the session's CMP path, from its first user turn."""
    first = next((t.get('content', '') for t in sess if t.get('role') == 'user'), '')
    first = ' '.join(first.split())[:60]
    return first or (date or f'Session {i+1}')


_CARD_CLIENT = None


def _card_client():
    global _CARD_CLIENT
    if _CARD_CLIENT is None:
        from backend.llm_client import LLMClient
        _CARD_CLIENT = LLMClient(db.get_model_by_id('llama.cpp/Gemma4-12B'))
    return _CARD_CLIENT


def _summarize_card(sess, date):
    """Cheap per-session waypoint card: extract the concrete facts a user might
    later ask about (one LLM call over THIS session's turns only, so no O(n^2)
    chatlog re-read). Returns a list of short fact strings."""
    txt = '\n'.join(f"{t.get('role')}: {t.get('content','')}" for t in sess)[:4500]
    prompt = ("From this conversation, extract the concrete facts the user might "
              "later ask about: names, numbers, dates, amounts, preferences, "
              "decisions, events, statuses. Return ONLY a JSON array of short "
              f"fact strings (max 10).\n\n[Date: {date}]\n{txt}")
    try:
        # Assistant prefill skips Gemma's CoT (which otherwise burns the whole
        # token budget before any JSON appears — same fix as the CMP detector).
        resp = _card_client().chat_completion(
            [{'role': 'user', 'content': prompt},
             {'role': 'assistant', 'content': '["'}],
            enable_thinking=False, max_tokens=400)
        content = (resp.get('response', {}).get('choices') or [{}])[0].get(
            'message', {}).get('content') or ''
        content = content.translate(str.maketrans({'“': '"', '”': '"',
                                                   '‘': "'", '’': "'"}))
        if not content.lstrip().startswith('['):
            content = '["' + content        # server did not echo the prefill
        import re
        m = re.search(r'\[.*\]', content, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(0))
            except ValueError:
                arr = re.findall(r'"([^"]{3,200})"', m.group(0))
            if isinstance(arr, list):
                return [str(x)[:200] for x in arr if str(x).strip()][:10]
        # no closing bracket (max_tokens cut) — salvage the quoted strings
        arr = re.findall(r'"([^"]{3,200})"', content)
        if arr:
            return [x for x in arr][:10]
    except Exception:
        pass
    return []


def ingest(agent_id, user_id, q, build_cmp=False, build_summary=False,
           build_cards=True):
    """Inject q's haystack into a fresh session. Returns (session_id, n_paths).

    CMP: build ONE path per haystack session DIRECTLY from the known session
    boundaries (store.new_cmp / create_path), setting each path's segment to that
    session's chatlog ts range. No detector runs during ingestion (LongMemEval
    already gives the boundaries); the detector is used only to route the
    question at answer time. read_transcript/recall then reach each session's
    transcript, and old sessions archive under the normal caps."""
    session_id = slug(agent_id, user_id)
    clean(agent_id, session_id)
    agent = db.get_agent(agent_id)
    chatlog = chatlog_manager.get(agent_id, session_id)
    ms = AgentState() if build_cmp else None

    sessions = q['haystack_sessions']
    dates = q.get('haystack_dates') or [''] * len(sessions)
    ts = int(time.time() * 1000) - len(sessions) * 10_000_000
    store = None
    if build_cmp:
        from backend.agent_runtime.cmp import store

    for i, (sess, date) in enumerate(zip(sessions, dates)):
        s_i = ts
        # open this session's CMP path at s_i (segment starts at s_i-1); the
        # NEXT create_path closes it, so the path spans exactly this session.
        if build_cmp:
            title = _session_title(sess, date, i)
            if i == 0:
                ms.cmp = store.new_cmp(ms, title=title, goal='', now_ts=s_i)
            else:
                store.create_path(ms.cmp, ms, title=title, now_ts=s_i)
            if build_cards:
                path = store.active_path(ms.cmp)
                facts = _summarize_card(sess, date)
                path['key_facts'] = facts
                path['outcome'] = ('; '.join(facts[:3]))[:300]
                path['card_stale'] = False
                try:
                    path['tags'] = store.compute_tags(path)
                except Exception:
                    pass
        # inject a date marker + the session's turns
        if date:
            _append(chatlog, session_id, 'user', f"[Conversation on {date}]", ts)
            ts += 1000
        for turn in sess:
            _append(chatlog, session_id, turn.get('role'),
                    turn.get('content') or '', ts)
            ts += 1000

    if build_cmp:
        from backend.agent_runtime.llm_loop import _persist_agent_state_split
        _persist_agent_state_split(ms, agent_id, session_id)

    if build_summary:
        _run_summary(agent, session_id)

    return session_id, (len((ms.cmp or {}).get('paths', {})) if ms else 0)


def _run_summary(agent, session_id):
    """Force the rolling summary to be built over the injected transcript."""
    try:
        from backend.agent_runtime import summarizer as _sum
        import threading
        _sum._do_summarize(agent, session_id, threading.Lock())
    except Exception as e:
        print(f"  [summary build failed: {str(e)[:80]}]")


if __name__ == '__main__':
    # smoke: ingest one oracle question for CMP and report paths built
    data = json.load(open(os.path.join(os.path.dirname(__file__), 'data',
                                       'longmemeval_oracle.json')))
    q = data[0]
    print(f"Q ({q['question_type']}): {q['question']}")
    print(f"answer: {q['answer']}  | sessions: {len(q['haystack_sessions'])}")
    t0 = time.time()
    sid, npaths = ingest('aisyah', 'lme-smoke-1', q, build_cmp=True,
                         cmp_granularity='turn')
    print(f"ingested CMP -> session {sid}, {npaths} paths ({time.time()-t0:.0f}s)")
