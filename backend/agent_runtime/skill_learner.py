"""
skill_learner.py — turns a finished turn into a learned skill.

Subscribed to ``turn_complete``. When an agent with learned skills enabled has
just worked through a multi-step task, the agent is asked once, in a separate
one-shot session, to write (or update) a skill under ``/_self/skills/`` so the
next run does not have to rediscover the steps. See backend/learned_skills.py
for the skill format.

Every stage is reported in the session the work happened in, as a session note.
"""

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from backend import learned_skills

log = logging.getLogger(__name__)

MIN_TOOL_CALLS = 5
COOLDOWN_SECONDS = 600
ASK_TIMEOUT = 600
DECISION_TIMEOUT_MS = 15_000
MAX_TRACE_STEPS = 40
_FILE_TOOLS = {'write_file', 'patch', 'str_replace'}
_SELF_SKILL_RE = re.compile(r'^/_self/skills/([a-z0-9][a-z0-9_-]*)/')

_guard = threading.Lock()
_running: set = set()               # agent ids with a learner in flight
_last_run: Dict[str, float] = {}    # agent id -> time the last learner finished

_AUTHORING_GUIDE = """\
A learned skill is a folder `/_self/skills/<skill_id>/` (id: lowercase letters, digits, `-`, `_`):
- `skill.json`: {"id": "<skill_id>", "name": "...", "description": "...", "brief": "<one line: when to load this skill>", "version": "1.0.0"}
- `SYSTEM.md`: the instructions: when to use it, prerequisites, the exact steps, pitfalls you hit and how to avoid them.
- Optional tools: add "tools_file": "tools.json" to skill.json, write `tools.json` as a JSON array of
  {"type": "function", "function": {"name": "<fn>", "description": "...", "parameters": {<JSON schema>}}},
  and one script per tool at `scripts/<fn>.sh` (bash) or `scripts/<fn>.py` (python).
  A script receives each argument as the environment variable `ARG_<NAME>` (upper-cased) and all of them as JSON in `SKILL_ARGS`.

Rules:
- Only keep something that will recur. If this was a one-off, or an existing skill already covers it, reply NO_SKILL.
- Prefer updating an existing learned skill over creating a near-duplicate.
- Instructions alone are enough for most skills. Add a tool only when a fixed script is more reliable than redoing the steps by hand.
- Keep it portable, so the skill can be exported to another agent: no machine-specific absolute paths, hostnames or user names. Take them as tool parameters or describe how to find them. List required programs under prerequisites.
- Never put secrets, tokens or personal data in a skill.
- Generalise: parameters instead of the specific file names and values of this one task.
- If you add a script and have a shell, run it once to check it works before you finish.
- Write files with your file tools using `/_self/skills/...` paths."""


def _is_error(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get('error')) or result.get('exit_code', 0) not in (0, None)


def worth_reviewing(tool_trace: List[dict]) -> bool:
    """Cheap filter: a long turn, or one that failed and then got it right."""
    if len(tool_trace) >= MIN_TOOL_CALLS:
        return True
    errors = [_is_error(step.get('result')) for step in tool_trace]
    return any(errors) and not errors[-1]


def saved_skill_ids(tool_trace: List[dict]) -> List[str]:
    """Ids of learned skills the agent successfully wrote to during a turn."""
    ids = []
    for step in tool_trace:
        if step.get('tool') not in _FILE_TOOLS or _is_error(step.get('result')):
            continue
        for value in (step.get('args') or {}).values():
            m = _SELF_SKILL_RE.match(value) if isinstance(value, str) else None
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + ' …[truncated]'


def build_prompt(user_request: str, tool_trace: List[dict], response: str,
                 existing: List[dict]) -> str:
    steps = []
    for i, step in enumerate(tool_trace[-MAX_TRACE_STEPS:], 1):
        status = 'ERROR' if _is_error(step.get('result')) else 'ok'
        steps.append(f"{i}. {step.get('tool')}({_clip(step.get('args') or {}, 600)}) "
                     f"-> {status}: {_clip(step.get('result'), 400)}")
    skills = '\n'.join(f"- {s['id']}: {s['brief'] or s['description']}" for s in existing) or '(none)'
    return (
        "You just finished the task below. Decide whether it should become a learned skill, "
        "so that next time you can load the skill instead of working it out again.\n\n"
        f"USER REQUEST:\n{_clip(user_request, 2000)}\n\n"
        "WHAT YOU DID (tool calls in order):\n" + '\n'.join(steps) + "\n\n"
        f"YOUR FINAL ANSWER:\n{_clip(response, 2000)}\n\n"
        f"YOUR EXISTING LEARNED SKILLS:\n{skills}\n\n"
        f"{_AUTHORING_GUIDE}\n\n"
        "When you are done, reply with exactly `DONE` or `NO_SKILL`."
    )


def _snapshot(agent: dict) -> Dict[str, float]:
    return {s['id']: s['updated_at'] for s in learned_skills.list_skills(agent, include_invalid=True)}


def _note(agent_id: str, session_id: str, external_user_id: str, text: str, **meta) -> None:
    """Tell the user (and the agent) what happened, in the session it happened in."""
    try:
        from backend.agent_runtime import agent_runtime
        agent_runtime.add_session_note(
            agent_id, session_id, f"[SYSTEM] {text}",
            {'learned_skill': meta}, external_user_id=external_user_id or '')
    except Exception:
        log.warning("[skill_learner] could not post note to session %s", session_id, exc_info=True)


def _decide(user_request: str, tool_trace: List[dict], response: str,
            existing: List[dict]) -> Optional[str]:
    """Ask the skill-worthiness decision profile. None when it is not enforcing."""
    from backend.agent_decisions import (SKILL_WORTHINESS, BuiltinDecisionRunner,
                                         skill_worthiness_input)
    sequence = [f"{step.get('tool')}:{'error' if _is_error(step.get('result')) else 'ok'}"
                for step in tool_trace]
    # Runs in the background, so the whole default chain gets time to answer.
    values = BuiltinDecisionRunner(timeout_ms=DECISION_TIMEOUT_MS).decide(SKILL_WORTHINESS, skill_worthiness_input(
        user_request, sequence, response,
        [f"{s['id']}: {s['brief'] or s['description']}" for s in existing]))
    return (values or {}).get('action')


def _learn(agent: dict, session_id: str, external_user_id: str, user_request: str,
           tool_trace: List[dict], response: str) -> None:
    from backend import agent_oneshot

    agent_id = agent['id']
    existing = learned_skills.list_skills(agent)
    if _decide(user_request, tool_trace, response, existing) == 'skip':
        return

    before = _snapshot(agent)
    _note(agent_id, session_id, external_user_id,
          "Reviewing this task to see whether it is worth keeping as a learned skill.",
          stage='reviewing')
    key = f"{int(time.time())}"
    reply = agent_oneshot.ask(
        agent_id, build_prompt(user_request, tool_trace, response, existing),
        tag='SYSTEM/skill-learner', sender='skilllearner', key=key, timeout=ASK_TIMEOUT)
    after = _snapshot(agent)
    changed = [sid for sid, mtime in after.items() if before.get(sid) != mtime]
    broken = {sid: err for sid in changed if (err := learned_skills.validate(agent, sid))}
    if broken and reply.get('success'):
        # One chance to repair, in the same one-shot session.
        problems = '\n'.join(f"- {sid}: {err}" for sid, err in broken.items())
        agent_oneshot.ask(
            agent_id, "The learned skill you wrote cannot be loaded:\n" + problems +
            "\nFix the files, then reply with exactly `DONE`.",
            tag='SYSTEM/skill-learner', sender='skilllearner', key=key, timeout=ASK_TIMEOUT)
        broken = {sid: err for sid in changed if (err := learned_skills.validate(agent, sid))}

    if not changed and not reply.get('success'):
        _note(agent_id, session_id, external_user_id,
              f"The learned-skill review did not finish: {reply.get('error')}.", stage='failed')
        return

    reported = False
    for sid in changed:
        if sid in broken:
            _note(agent_id, session_id, external_user_id,
                  f"Learned skill `{sid}` was written but cannot be loaded yet: {broken[sid]}",
                  stage='invalid', id=sid)
        elif sid in before:
            _note(agent_id, session_id, external_user_id,
                  f"Learned skill `{sid}` was updated.", stage='updated', id=sid)
        else:
            _note(agent_id, session_id, external_user_id,
                  f"New learned skill `{sid}` was created. Load it with use_skill next time.",
                  stage='created', id=sid)
        reported = True
    if not reported:
        _note(agent_id, session_id, external_user_id,
              "No learned skill was needed for this task.", stage='none')


def on_turn_complete(event: dict) -> None:
    payload = event.get('payload') or event
    agent_id = payload.get('agent_id')
    session_id = payload.get('session_id')
    tool_trace = payload.get('tool_trace') or []
    if not (agent_id and session_id and tool_trace) or payload.get('is_error'):
        return

    from models.db import db
    # Sub-agents have no row of their own: only a real agent learns.
    agent = db.get_agent(agent_id)
    if not agent or not agent.get('learned_skills_enabled'):
        return
    external_user_id = payload.get('external_user_id') or ''
    try:
        from backend.agent_oneshot import is_oneshot_external_id
        from backend.ephemeral_sessions import is_memory_blocked
        # Skips the learner's own one-shot session too, so it never feeds itself.
        if is_oneshot_external_id(external_user_id) or is_memory_blocked(session_id):
            return
    except Exception:
        return

    # The agent edited a learned skill itself during the turn: report it.
    for sid in saved_skill_ids(tool_trace):
        _note(agent_id, session_id, external_user_id,
              f"Learned skill `{sid}` was saved by the agent.", stage='saved', id=sid)

    if not worth_reviewing(tool_trace):
        return
    with _guard:
        if agent_id in _running or time.time() - _last_run.get(agent_id, 0) < COOLDOWN_SECONDS:
            return
        _running.add(agent_id)

    user_request = ''
    try:
        from models.chatlog import chatlog_manager
        last_user = chatlog_manager.get(agent_id, session_id).get_last_entry(types=frozenset({'user'}))
        user_request = (last_user or {}).get('content') or ''
    except Exception:
        pass

    def _run():
        try:
            _learn(agent, session_id, external_user_id, user_request, tool_trace,
                   payload.get('response') or '')
        except Exception:
            log.warning("[skill_learner] failed for agent %s", agent_id, exc_info=True)
        finally:
            with _guard:
                _running.discard(agent_id)
                _last_run[agent_id] = time.time()

    threading.Thread(target=_run, daemon=True).start()
