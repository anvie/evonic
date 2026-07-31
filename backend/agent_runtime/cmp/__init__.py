"""
CMP (Context Memory Path) — cross-task context management.

Session-level sibling of ATG (which structures context WITHIN one task):
maintains a graph of task paths rendered as a navigable map, with
interface-preserving path cards, 4-class boundary detection and soft
offload. See .claude/tasks/cmp-implementation.md and the CMP paper.

Public surface:
    is_cmp_enabled(agent)              — flag gate
    render_cmp_section(cmp)            — map + cards block for AgentState.render
    on_turn_boundary(...)              — detector + lifecycle orchestrator (M3)
"""


# How many turns a detector-referenced (pinned) path keeps its full card
# rendered after it was last named — the follow-up window over which a
# just-referenced offloaded path stays fully in context.
_PIN_TTL = 3


def is_cmp_enabled(agent: dict) -> bool:
    """CMP applies only with agent-state enabled (paths live on AgentState)
    and never for sub-agents (delegated single-task workers)."""
    if not agent or not agent.get('enable_cmp'):
        return False
    if not agent.get('enable_agent_state'):
        return False
    return not agent.get('is_subagent')


def render_cmp_section(cmp: dict, agent_name: str = "Agent") -> str:
    """Lazy re-export so AgentState.render never pays the import unless used."""
    from backend.agent_runtime.cmp.render import render_cmp_section as _render
    return _render(cmp, agent_name)


def on_turn_boundary(agent: dict, ms, chatlog, user_text: str,
                     session_id: str = None, agent_id: str = None):
    """Per-turn CMP orchestration: first-path init, boundary detection,
    path ops (switch/branch), lifecycle decay. Called from the runtime at
    the re-arm slot; subsumes maybe_rearm_atg for cmp agents (a new branch
    IS the re-arm — fresh plan cycle on its own path).

    Returns the decision dict, or None when CMP does not apply.
    Mutates ms (and ms.cmp) in place; persistence rides the normal
    agent-state persist paths.
    """
    if not is_cmp_enabled(agent) or ms is None:
        return None

    from backend.agent_runtime.cmp import detector, store
    from backend.agent_runtime.cmp.compactor import finalize_active_card

    text = (user_text or '').strip()
    user_ts = _last_user_ts(chatlog)

    # First-path init: adopt the ongoing work (or this first message) as A1.
    if not ms.cmp or not ms.cmp.get('paths'):
        title = _current_work_title(ms)
        ms.cmp = store.new_cmp(ms, title=title or text[:60] or 'Session start',
                               goal=text[:300], now_ts=user_ts)
        if not title:  # raw message as title reads badly on the map — name it
            named = detector.detect(
                ms.cmp, ms, text, initializing=True,
                session_id=session_id, agent_id=agent_id).get('new_path')
            _apply_naming(ms.cmp['paths'][ms.cmp['active_id']], named)
        _emit(agent, 'cmp_path_created',
              {'path_id': ms.cmp['active_id'],
               'title': ms.cmp['paths'][ms.cmp['active_id']]['title'],
               'initiator': 'auto-init'})
        return {'decision': 'init', 'target': ms.cmp['active_id'], 'layer': 'init'}

    decision = detector.detect(ms.cmp, ms, text,
                               recent_tail=_last_final_excerpt(chatlog),
                               recent_dialogue=_recent_dialogue(chatlog),
                               session_id=session_id, agent_id=agent_id)
    d, target = decision['decision'], decision.get('target')
    # The delta describes the just-completed turn on the (still) active path;
    # apply it before any switch/branch suspends that path.
    if decision.get('card_delta'):
        store.apply_card_delta(store.active_path(ms.cmp),
                               decision['card_delta'])
    try:
        if d == 'return':
            finalize_active_card(chatlog, ms.cmp, ms)
            store.switch_to(ms.cmp, ms, target, now_ts=user_ts)
            _emit(agent, 'cmp_path_switched',
                  {'to': target, 'initiator': 'detector'})
        elif d in ('dep_branch', 'indep_branch'):
            from backend.task_classifier import classify_task
            finalize_active_card(chatlog, ms.cmp, ms)
            record = store.create_path(
                ms.cmp, ms, title=text[:60], goal=text[:300],
                depends_on=[target] if (d == 'dep_branch' and target) else [],
                now_ts=user_ts, trivial=classify_task(text) == 'trivial')
            _apply_naming(record, decision.get('new_path'))
            decision['target'] = record['id']
            _emit(agent, 'cmp_path_created',
                  {'path_id': record['id'], 'depends_on': record['depends_on'],
                   'initiator': 'detector'})
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "CMP path operation failed — continuing on the active path")
        decision = {'decision': 'continue', 'target': None, 'layer': 'error'}

    # Waypoint pinning (paper's load_waypoint, made automatic + STICKY): the
    # detector flags non-active paths whose stored facts this turn references
    # (recaps, cross-path summaries). Those paths are (a) pinned for a short
    # window so their FULL cards stay rendered across follow-up turns — not
    # only the turn they were named — and (b) promoted archived->preserved so
    # the card survives lifecycle decay. The count-cap (tick_lifecycle) then
    # enforces MAX_PRESERVED by archiving the OLDEST preserved, so the 10-cap
    # holds and the least-recently-referenced card decays out. Active path is
    # never pinned (its full card is already in context).
    referenced = [pid for pid in (decision.get('pin') or [])
                  if pid in ms.cmp['paths'] and pid != ms.cmp['active_id']]
    # Query-relevant waypoint auto-pinning: lexically match the user's message
    # against the stored cards (title/tags/key_facts/goal) and pin the top
    # hits too. The detector names paths it can see on the map, but archived
    # nodes render as title+tags only — on large graphs the fact the user is
    # asking about often lives in a card the detector cannot read. The lexical
    # index sees every card, costs no LLM call, and a wrong pin only costs a
    # briefly rendered extra card (TTL decays it).
    excerpts = {}
    try:
        # Cards are lossy, so raw transcript matches always participate rather
        # than only when card search found fewer than two hits. Merge both
        # retrieval layers under one small pin cap; transcript hits win ties
        # because they carry the actual user-authored evidence into context.
        candidates = {}
        for hit in store.search_cmp_paths(ms.cmp, text, limit=3):
            if hit.get('score', 0) >= 2:
                candidates[hit['id']] = (hit['score'], 0, [])
        for hit in store.search_cmp_transcripts(ms.cmp, chatlog, text, limit=3):
            if hit.get('score', 0) >= 2:
                old = candidates.get(hit['id'], (0, 0, []))
                candidates[hit['id']] = (max(hit['score'], old[0]), 1,
                                          hit.get('excerpts') or [])
        ranked = sorted(candidates.items(),
                        key=lambda item: (-item[1][0], -item[1][1], item[0]))
        for pid, (_, _, hit_excerpts) in ranked:
            if pid == ms.cmp['active_id'] or pid in referenced:
                continue
            referenced.append(pid)
            if hit_excerpts:
                excerpts[pid] = hit_excerpts
            if len(referenced) >= 3:
                break
    except Exception:
        pass
    ttl = ms.cmp.setdefault('pin_ttl', {})
    for pid in list(ttl):                      # decay existing pins
        ttl[pid] -= 1
        if ttl[pid] <= 0 or pid not in ms.cmp['paths'] or pid == ms.cmp['active_id']:
            ttl.pop(pid, None)
    for pid in referenced:                     # (re)arm referenced pins
        ttl[pid] = _PIN_TTL
    ms.cmp['pinned_ids'] = list(ttl)
    # transcript-recall excerpts ride the pin lifecycle: refreshed for newly
    # matched paths, dropped when their pin decays.
    old_ex = ms.cmp.get('pin_excerpts') or {}
    ms.cmp['pin_excerpts'] = {pid: (excerpts.get(pid) or old_ex.get(pid))
                              for pid in ttl
                              if excerpts.get(pid) or old_ex.get(pid)}
    promoted = store.promote_pinned(ms.cmp, list(ttl), user_ts)
    if referenced:
        _emit(agent, 'cmp_waypoints_pinned',
              {'path_ids': referenced, 'promoted': promoted})

    lifecycle = store.tick_lifecycle(ms.cmp, now_ts=user_ts)
    for archived_id in lifecycle['archived']:
        _emit(agent, 'cmp_path_archived', {'path_id': archived_id})
    for pruned_id in lifecycle['pruned']:
        _emit(agent, 'cmp_path_pruned', {'path_id': pruned_id})
    _emit(agent, 'cmp_boundary_decision', decision)
    return decision


def _apply_naming(record: dict, named) -> None:
    """Set a new path's title/action ONCE, from the single-pass envelope's
    in-context naming. Both fields are immutable afterwards (map node labels
    and edges never change). Falls back to the mechanical raw-message
    title/action already on the record when the envelope carried no name."""
    if not isinstance(named, dict):
        return
    from backend.agent_runtime.cmp import store
    title = str(named.get('title') or '').strip()
    action = str(named.get('action') or '').strip()
    if title:
        record['title'] = title[:store.TITLE_MAX]
    if action:
        record['action'] = action[:store.ACTION_MAX]


def _current_work_title(ms) -> str:
    if isinstance(ms.atg, dict):
        goal = ((ms.atg.get('dag') or {}).get('root_goal')
                or ms.atg.get('root_goal'))
        if goal:
            return str(goal)[:60]
    if ms.plan_file:
        return str(ms.plan_file)[:60]
    return ''


def _last_final_excerpt(chatlog, max_chars: int = 1000) -> str:
    """Excerpt of the agent's latest final reply — the just-delivered
    deliverable, fed to the single-pass turn call both as routing context
    and as the substance for the active path's card delta."""
    try:
        entry = chatlog.get_last_entry(types=frozenset({'final'}))
        content = (entry or {}).get('content') or ''
        return content[:max_chars]
    except Exception:
        return ''


def _recent_dialogue(chatlog, max_msgs: int = 5, per_msg: int = 400,
                     max_chars: int = 1600) -> str:
    """The last few user↔agent turns before the current message — the
    immediate conversational context the boundary detector needs to ground
    terse messages ('coba lagi', 'yang itu aja', 'lanjut'). Excludes the
    current user message (the one being classified, already the newest 'user'
    entry) so the detector doesn't just echo it back."""
    try:
        entries = [e for e in chatlog.tail(limit=24)
                   if e.get('type') in ('user', 'final', 'intermediate')
                   and (e.get('content') or '').strip()
                   and not (e.get('metadata') or {}).get('slash_command')]
        if entries and entries[-1].get('type') == 'user':
            entries = entries[:-1]  # drop the message being classified now
        lines = []
        for e in entries[-max_msgs:]:
            role = 'User' if e.get('type') == 'user' else 'Agent'
            lines.append(f"{role}: {(e.get('content') or '').strip()[:per_msg]}")
        return '\n'.join(lines)[-max_chars:]
    except Exception:
        return ''


def _last_user_ts(chatlog):
    try:
        entry = chatlog.get_last_entry(types={'user'})
        if entry and entry.get('ts'):
            return entry['ts']
    except Exception:
        pass
    import time
    return int(time.time() * 1000)


def _emit(agent: dict, event: str, payload: dict) -> None:
    try:
        from backend.event_stream import event_stream
        event_stream.emit(event, {'agent_id': agent.get('id', ''), **payload})
    except Exception:
        pass
