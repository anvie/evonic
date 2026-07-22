"""
CMP (Context Memory Path) — path store.

Pure functions over the `ms.cmp` dict (persisted inside session_state).
No I/O, no LLM calls. Each path record is a "path card" plus transcript
segments (chatlog ts ranges) plus a snapshot of the per-task AgentState
fields (mode/plan_file/auto_trivial/atg) — restoring these on switch is
what fixes the single-slot `ms.atg` limitation.
"""
from __future__ import annotations

import re
import time

CMP_VERSION = 1
MAX_PATHS = 100

# Count-based preserved cap + wall-clock archived decay (evaluated at turn
# boundaries): each state strictly reduces a path's context cost — active
# (full card), preserved (snippet, max MAX_PRESERVED), archived (map-node
# title only), pruned (record removed).
MAX_PRESERVED = 100                        # per-cap, oldest preserved archived
ARCHIVED_TTL_MS = 3 * 24 * 3600 * 1000    # archived  > 3 days → pruned
RESTORE_MAX_HOPS = 3                       # lineage auto-restore depth

# Card field caps (interface-preserving but bounded)
TITLE_MAX = 60
GOAL_MAX = 300
OUTCOME_MAX = 300
KEY_FACTS_MAX = 6
KEY_FACT_CHARS = 200
ARTIFACTS_MAX = 8

VALID_STATUSES = {"active", "preserved", "archived"}

# AgentState fields snapshotted per path (the single-slot per-task state)
SNAPSHOT_FIELDS = ("mode", "plan_file", "auto_trivial", "atg")


def _now_ms() -> int:
    return int(time.time() * 1000)


ACTION_MAX = 32

_PATH_ID_RE = re.compile(r'^([A-Z]+)(\d+)$')


def clamp_card_fields(card: dict) -> dict:
    """Enforce field caps in place; returns the card for chaining."""
    card["title"] = str(card.get("title") or "")[:TITLE_MAX]
    card["goal"] = str(card.get("goal") or "")[:GOAL_MAX]
    card["outcome"] = str(card.get("outcome") or "")[:OUTCOME_MAX]
    card["action"] = str(card.get("action") or "")[:ACTION_MAX]
    card["key_facts"] = [str(f)[:KEY_FACT_CHARS]
                         for f in (card.get("key_facts") or [])[:KEY_FACTS_MAX]]
    card["artifacts"] = [str(a)[:KEY_FACT_CHARS]
                         for a in (card.get("artifacts") or [])[:ARTIFACTS_MAX]]
    return card


# Card fields the delta op may touch. Everything else on a path record —
# id, title, action, depends_on, segments, status, snapshots — is immutable
# to the LLM (title/action/depends_on are set once at creation; the map's
# node labels and edges never change afterwards).
_DELTA_APPEND_FIELDS = (
    ("new_facts", "key_facts", KEY_FACTS_MAX),
    ("new_artifacts", "artifacts", ARTIFACTS_MAX),
)


def apply_card_delta(path: dict, delta: dict) -> None:
    """Deterministic, append-only card update from a single-pass turn op
    (evomem-writer style: the LLM emits a delta, this applies it under the
    graph's immutability invariants). `outcome` replaces; facts/artifacts
    append with exact dedupe, capped keep-newest. Never touches node
    identity (id/title/action/depends_on) or lifecycle fields."""
    if not isinstance(path, dict) or not isinstance(delta, dict):
        return
    outcome = str(delta.get("outcome") or "").strip()
    if outcome:
        path["outcome"] = outcome[:OUTCOME_MAX]
    for delta_key, field, cap in _DELTA_APPEND_FIELDS:
        new_items = delta.get(delta_key)
        if not isinstance(new_items, list):
            continue
        items = [str(v)[:KEY_FACT_CHARS] for v in (path.get(field) or [])]
        for item in new_items:
            text = str(item).strip()[:KEY_FACT_CHARS]
            if text and text not in items:
                items.append(text)
        path[field] = items[-cap:]
    # refresh searchable tags so `recall` can find this path by keyword
    path["tags"] = compute_tags(path)


# ── keyword tags + in-session search (recall over the path graph) ───────────
_STOPWORDS = frozenset((
    "the a an and or but for to of in on at by with from into as is are was were "
    "be been being this that these those it its they them their we you your i me my "
    "do does did done make made create created add added want wants need needs "
    "please help user agent task new back again then now here there what which who "
    "how when where why can could would should will just also only about only".split()
))
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{1,}")


def _tokenize(text: str) -> set:
    """Lowercased content tokens ≥3 chars, stopwords dropped; identifier-like
    tokens (filenames, snake_case, CamelCase) are preserved as-is too so
    'tasks.json' / 'convert_heading' / 'llm_usage' remain matchable."""
    out = set()
    for tok in _TOKEN_RE.findall(text or ""):
        low = tok.lower()
        if len(low) >= 3 and low not in _STOPWORDS:
            out.add(low)
    return out


def compute_tags(path: dict, cap: int = 12) -> list:
    """Salient keyword tags for a path, derived from its title/goal/facts/
    artifacts. Deterministic — no LLM. Title/fact/artifact tokens rank first."""
    strong = _tokenize(path.get("title") or "")
    for f in path.get("key_facts") or []:
        strong |= _tokenize(str(f))
    for a in path.get("artifacts") or []:
        strong |= _tokenize(str(a))
    weak = _tokenize(path.get("goal") or "") | _tokenize(path.get("outcome") or "")
    ordered = sorted(strong) + sorted(weak - strong)
    return ordered[:cap]


def _path_search_tokens(path: dict) -> tuple:
    """(strong_tokens, all_tokens) for scoring a query against a path."""
    strong = set(path.get("tags") or []) or set(compute_tags(path))
    strong |= _tokenize(path.get("title") or "")
    allt = set(strong)
    for f in path.get("key_facts") or []:
        allt |= _tokenize(str(f))
    allt |= _tokenize(path.get("goal") or "") | _tokenize(path.get("outcome") or "")
    for a in path.get("artifacts") or []:
        allt |= _tokenize(str(a))
    return strong, allt


def _stem(tok: str) -> str:
    """Suffix-strip stemmer for recall matching ('graduated'~'graduate',
    'degrees'~'degree'). Deterministic, English-lite; only used for transcript
    search where exact-token overlap is too brittle."""
    for suf in ('ing', 'ed', 'es', 's'):
        if len(tok) > 4 and tok.endswith(suf):
            tok = tok[:-len(suf)]
            break
    return tok[:-1] if len(tok) > 4 and tok.endswith('e') else tok


def search_cmp_transcripts(cmp: dict, chatlog, query: str, limit: int = 3,
                           exclude_active: bool = True) -> list:
    """Lexical recall over each path's RAW transcript segments — the fallback
    layer beneath card search, for facts the compactor never lifted into
    key_facts (cards are lossy by design). Stemmed token overlap, no LLM.
    Returns [{id, score, excerpts}] where excerpts are the best-matching
    transcript lines (the actual text holding the fact)."""
    q = {_stem(t) for t in _tokenize(query)}
    if not q or not cmp or not cmp.get('paths'):
        return []
    from backend.agent_runtime.cmp.compactor import collect_path_entries
    scored = []
    for pid, path in cmp['paths'].items():
        if exclude_active and pid == cmp.get('active_id'):
            continue
        lines, user_lines = [], []
        for e in collect_path_entries(chatlog, path):
            if e.get('type') not in ('user', 'final', 'intermediate'):
                continue
            content = e.get('content') or ''
            s = len(q & {_stem(t) for t in _tokenize(content)})
            if s > 0:
                item = (s, content.strip()[:300])
                lines.append(item)
                if e.get('type') == 'user':
                    user_lines.append(item)
        if lines:
            # User-authored matches are the strongest evidence for personal
            # memory queries. Assistant boilerplate often repeats query words
            # ("store", "returns") across unrelated sessions and otherwise
            # crowds the actual user facts out of a small recall limit.
            evidence = user_lines or lines
            evidence.sort(key=lambda t: -t[0])
            score = max(s for s, _ in evidence)
            total = sum(s for s, _ in evidence)
            scored.append((bool(user_lines), score, total, pid,
                           [ln for _, ln in evidence[:3]]))
    scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
    return [{'id': pid, 'score': score, 'excerpts': ex}
            for _, score, _, pid, ex in scored[:limit]]


def search_cmp_paths(cmp: dict, query: str, limit: int = 5,
                     exclude_active: bool = True) -> list:
    """Rank the session's paths by keyword overlap with the query. Lets the
    agent's `recall` tool retrieve facts from offloaded/archived paths that the
    per-turn detector did not pin. Returns [{id,title,status,key_facts,score}]."""
    q = _tokenize(query)
    if not q or not cmp or not cmp.get("paths"):
        return []
    scored = []
    for pid, path in cmp["paths"].items():
        if exclude_active and pid == cmp.get("active_id"):
            continue
        strong, allt = _path_search_tokens(path)
        score = 2 * len(q & strong) + len(q & (allt - strong))
        if score > 0:
            scored.append((score, pid, path))
    scored.sort(key=lambda t: (-t[0], t[1]))

    def _snippet(path):
        # what the match is grounded in — key_facts first, then goal/outcome,
        # since some facts (a lunch venue, a time) live only in the raw goal.
        parts = list(path.get("key_facts") or [])
        if path.get("outcome"):
            parts.append(f"outcome: {path['outcome']}")
        if path.get("goal"):
            parts.append(f"goal: {path['goal']}")
        return " | ".join(parts)[:600]

    return [{"id": pid, "title": path.get("title"),
             "status": path_status(path), "snippet": _snippet(path),
             "key_facts": path.get("key_facts") or [], "score": score}
            for score, pid, path in scored[:limit]]


def path_status(path: dict) -> str:
    """Normalized lifecycle status. 'dormant' is the legacy (pre-lifecycle)
    name of 'preserved' — reader-side migration; writers only emit the new
    vocabulary, so persisted sessions converge without a migration pass."""
    status = (path or {}).get("status")
    return "preserved" if status == "dormant" else (status or "preserved")


def _set_status(path: dict, status: str, now_ts: int) -> None:
    """All status writes go through here so `state_since` (the lifecycle
    clock used for count-based ordering and the archive TTL) can never
    drift from the status."""
    path["status"] = status
    path["state_since"] = now_ts


def _state_since(path: dict) -> int:
    # legacy records predate state_since; last_active is the best proxy
    return path.get("state_since") or path.get("last_active") or 0


def _is_depended_on(cmp: dict, pid: str) -> bool:
    return any(pid in (p.get("depends_on") or [])
               for p in cmp["paths"].values() if p.get("id") != pid)


def _id_sort_key(pid: str):
    m = _PATH_ID_RE.match(pid or '')
    return (m.group(1), int(m.group(2))) if m else (pid, 0)


def sort_path_ids(ids) -> list:
    """Level-then-index ordering (A1, A2, A10, B1 — not lexicographic)."""
    return sorted(ids, key=_id_sort_key)


def path_level(cmp: dict, pid: str) -> int:
    """Dependency depth of a path: root=0 (letter A), child of root=1 (B)…"""
    seen = set()
    def depth(p):
        if p in seen or p not in cmp["paths"]:
            return 0
        seen.add(p)
        deps = cmp["paths"][p].get("depends_on") or []
        return 0 if not deps else 1 + max(depth(d) for d in deps)
    return depth(pid)


def _compute_path_id(cmp: dict, depends_on: list) -> str:
    """Level-based id (paper map notation): the letter encodes the dependency
    level (A=root, B=child, C=grandchild…), the number is the next free index
    within that level — siblings across different parents share the level
    counter (A1, A2 / B1, B2, B3 / C1…)."""
    if not depends_on:
        level = 0
    else:
        level = 1 + max(path_level(cmp, d) for d in depends_on)
    letter = chr(ord('A') + min(level, 25))
    highest = 0
    for pid in cmp.get("paths") or {}:
        m = _PATH_ID_RE.match(pid)
        if m and m.group(1) == letter:
            highest = max(highest, int(m.group(2)))
    return f"{letter}{highest + 1}"


def _default_action(title: str) -> str:
    """Mechanical edge label until the compactor refines it."""
    return ' '.join((title or '').split()[:4])[:ACTION_MAX]


def new_cmp(ms, title: str, goal: str = "", now_ts: int = None) -> dict:
    """Initialize the cmp dict with the first path, adopting the current
    AgentState per-task fields as that path's live state."""
    now_ts = now_ts if now_ts is not None else _now_ms()
    cmp = {
        "version": CMP_VERSION,
        "active_id": "A1",
        "paths": {
            "A1": _new_path_record("A1", title, goal, now_ts),
        },
        "stats": {"switches": 0, "branches": 0, "detector_llm_calls": 0},
    }
    return cmp


def _new_path_record(pid: str, title: str, goal: str, now_ts: int,
                     depends_on: list = None) -> dict:
    # Segments use exclusive-start semantics (after_ts < ts <= up_to_ts, the
    # chatlog range convention), cut at user_entry_ts - 1 so the triggering
    # user entry always belongs to the newly opened segment.
    return clamp_card_fields({
        "id": pid,
        "title": title,
        "status": "active",
        "goal": goal,
        "outcome": "",
        "action": _default_action(title),
        "key_facts": [],
        "artifacts": [],
        "depends_on": list(depends_on or []),
        "segments": [[now_ts - 1, None]],
        "last_active": now_ts,
        "state_since": now_ts,
        "card_stale": True,
        # per-task AgentState snapshot; None while the path is active
        # (the live values are on ms) — filled on switch-away.
        "mode": None,
        "plan_file": None,
        "auto_trivial": False,
        "atg": None,
    })


def active_path(cmp: dict) -> dict:
    return cmp["paths"][cmp["active_id"]]


def valid_targets(cmp: dict) -> list:
    """[(id, title)] of switchable (non-active) paths, newest first."""
    out = [(p["id"], p["title"]) for p in cmp["paths"].values()
           if p["id"] != cmp["active_id"]]
    return sorted(out, key=lambda t: _id_sort_key(t[0]), reverse=True)


def create_path(cmp: dict, ms, title: str, goal: str = "",
                depends_on: list = None, now_ts: int = None,
                trivial: bool = False) -> dict:
    """Create and switch to a new path with task-appropriate mode.

    Trivial paths start executable immediately; complex paths start a fresh
    plan cycle. Returns the record and raises ValueError on invalid depends_on.
    """
    depends_on = list(depends_on or [])
    unknown = [d for d in depends_on if d not in cmp["paths"]]
    if unknown:
        raise ValueError(f"Unknown dependency path(s): {unknown}. "
                         f"Valid: {sorted(cmp['paths'])}")
    now_ts = now_ts if now_ts is not None else _now_ms()

    _suspend_active(cmp, ms, now_ts)

    pid = _compute_path_id(cmp, depends_on)
    record = _new_path_record(pid, title, goal, now_ts, depends_on=depends_on)
    cmp["paths"][pid] = record
    cmp["active_id"] = pid

    # Start trivial tasks executable; complex tasks enter a fresh plan cycle.
    ms.mode = "execute" if trivial else "plan"
    ms.plan_file = None
    ms.auto_trivial = trivial
    ms.atg = None

    cmp["stats"]["branches"] = cmp["stats"].get("branches", 0) + 1
    restore_lineage(cmp, pid, now_ts)
    enforce_caps(cmp)
    return record


def switch_to(cmp: dict, ms, target_id: str, now_ts: int = None) -> dict:
    """Atomic card-first switch: suspend active (close segment + snapshot ms
    fields), then activate target (restore snapshot + open segment).

    The caller is responsible for finalizing the outgoing card BEFORE
    calling this (compactor), per the paper's ordering. Raises ValueError
    on unknown/active target.
    """
    if target_id not in cmp["paths"]:
        raise ValueError(f"Unknown path '{target_id}'. Valid: "
                         + ", ".join(f"{i} ({t})" for i, t in valid_targets(cmp)))
    if target_id == cmp["active_id"]:
        raise ValueError(f"Path '{target_id}' is already active.")
    now_ts = now_ts if now_ts is not None else _now_ms()

    _suspend_active(cmp, ms, now_ts)

    target = cmp["paths"][target_id]
    # Restore the per-task AgentState fields
    ms.mode = target.get("mode") or "execute"
    ms.plan_file = target.get("plan_file")
    ms.auto_trivial = bool(target.get("auto_trivial"))
    ms.atg = target.get("atg")
    # Clear the stored snapshot while live (avoids stale double-truth)
    target["mode"] = None
    target["plan_file"] = None
    target["auto_trivial"] = False
    target["atg"] = None

    _set_status(target, "active", now_ts)  # reactivation resets the clock
    target["last_active"] = now_ts
    target["card_stale"] = True  # new content will accumulate while active
    target["segments"].append([now_ts - 1, None])
    cmp["active_id"] = target_id

    cmp["stats"]["switches"] = cmp["stats"].get("switches", 0) + 1
    restore_lineage(cmp, target_id, now_ts)
    return target


def _suspend_active(cmp: dict, ms, now_ts: int) -> dict:
    """Close the active path's open segment and snapshot ms per-task fields."""
    path = active_path(cmp)
    _close_open_segment(path, now_ts)
    path["mode"] = ms.mode
    path["plan_file"] = ms.plan_file
    path["auto_trivial"] = ms.auto_trivial
    path["atg"] = ms.atg
    _set_status(path, "preserved", now_ts)
    path["last_active"] = now_ts
    # card_stale untouched: the compactor finalizes the card right before
    # suspension and clears the flag; activation re-marks it (new content).
    return path


def _close_open_segment(path: dict, now_ts: int) -> None:
    segments = path.get("segments") or []
    if segments and segments[-1][1] is None:
        # cut just before the incoming user entry (turn boundary — the user
        # entry that triggered the switch is already in the chatlog)
        segments[-1][1] = max(now_ts - 1, segments[-1][0])


def restore_lineage(cmp: dict, active_id: str, now_ts: int) -> list:
    """Archived dependency ancestors within RESTORE_MAX_HOPS of the active
    path are promoted back to preserved: operating deep in a branch regains
    the lineage's lightweight summaries without restoring the whole graph.
    Returns the promoted path ids."""
    promoted = []
    for pid in dependency_ancestors(cmp, active_id,
                                    max_depth=RESTORE_MAX_HOPS,
                                    max_count=len(cmp["paths"])):
        path = cmp["paths"][pid]
        if path_status(path) == "archived":
            _set_status(path, "preserved", now_ts)
            promoted.append(pid)
    return promoted


def promote_pinned(cmp: dict, pinned_ids: list, now_ts: int) -> list:
    """Persistent, cap-respecting promotion for detector-referenced paths.

    When the boundary detector flags that the current turn needs information
    from a NON-active path (a recap / cross-path summary), that path is
    promoted archived -> preserved so its card stays in context across the
    follow-up turns, not only the turn it was named. Already-preserved pins are
    just touched as recently-active so the count-cap keeps them while pinned.
    tick_lifecycle then enforces MAX_PRESERVED, archiving the OLDEST preserved
    instead — so the 10-cap holds and the least-recently-referenced card decays
    out. Returns the ids that were promoted from archived."""
    promoted = []
    for pid in pinned_ids:
        path = cmp["paths"].get(pid)
        if not path or pid == cmp.get("active_id"):
            continue
        if path_status(path) == "archived":
            _set_status(path, "preserved", now_ts)
            promoted.append(pid)
        elif path_status(path) == "preserved":
            path["state_since"] = now_ts  # keep fresh so the cap won't archive it while pinned
    return promoted


def tick_lifecycle(cmp: dict, now_ts: int = None) -> dict:
    """Lifecycle decay evaluated at turn boundaries.

    preserved count cap (MAX_PRESERVED): when more than MAX_PRESERVED paths
    are in the preserved state, the oldest ones (by state_since) are archived
    — unless the path is in the active path's restore lineage (≤ RESTORE_MAX_HOPS),
    which never decays out from under the current work.
    archived > ARCHIVED_TTL_MS → pruned (record removed) — unless a living
    path still depends_on it (pruning a referenced parent would re-root the
    child's established edge; dead subtrees decay bottom-up instead).
    Returns {'archived': [...], 'pruned': [...]}.
    """
    now_ts = now_ts if now_ts is not None else _now_ms()
    restore_lineage(cmp, cmp["active_id"], now_ts)
    protected = set(dependency_ancestors(cmp, cmp["active_id"],
                                         max_depth=RESTORE_MAX_HOPS,
                                         max_count=len(cmp["paths"])))
    archived, pruned = [], []

    # --- preserved count cap: archive oldest over MAX_PRESERVED ----------
    preserved_unprotected = [
        (pid, p) for pid, p in cmp["paths"].items()
        if path_status(p) == "preserved" and pid not in protected
    ]
    preserved_unprotected.sort(key=lambda t: _state_since(t[1]))

    overflow = len(preserved_unprotected) - MAX_PRESERVED
    # Guard: a negative overflow must archive NOTHING. A bare slice
    # preserved_unprotected[:overflow] with overflow < 0 means "all but the
    # last |overflow|" (Python negative indexing), which wrongly archives a
    # path whenever the preserved count sits just under the cap (e.g. 2 < 3
    # archives 1, collapsing the map to a single preserved node).
    for pid, path in preserved_unprotected[:max(0, overflow)]:
        _set_status(path, "archived", now_ts)
        path["atg"] = None  # heavy snapshot never survives archiving
        archived.append(pid)

    # --- archived TTL → prune --------------------------------------------
    for pid, path in cmp["paths"].items():
        if (path_status(path) == "archived"
                and now_ts - _state_since(path) > ARCHIVED_TTL_MS
                and not _is_depended_on(cmp, pid)):
            pruned.append(pid)
    for pid in pruned:
        del cmp["paths"][pid]
    return {"archived": archived, "pruned": pruned}


def enforce_caps(cmp: dict) -> None:
    """Backstop: archived paths drop their atg snapshot (facts live in the
    card), and beyond MAX_PATHS the oldest archived paths are fully removed
    (prune semantics, including the depends_on reference protection)."""
    for path in cmp["paths"].values():
        if path_status(path) == "archived" and path.get("atg") is not None:
            path["atg"] = None

    to_prune = len(cmp["paths"]) - MAX_PATHS
    if to_prune <= 0:
        return
    archived = sorted(
        (p for p in cmp["paths"].values() if path_status(p) == "archived"),
        key=lambda p: p.get("last_active", 0))
    for path in archived:
        if to_prune <= 0:
            break
        if _is_depended_on(cmp, path["id"]):
            continue
        del cmp["paths"][path["id"]]
        to_prune -= 1


def dependency_ancestors(cmp: dict, path_id: str, max_depth: int = 2,
                         max_count: int = 4) -> list:
    """Transitive depends_on ancestors of a path (BFS, bounded)."""
    seen = []
    frontier = list(cmp["paths"].get(path_id, {}).get("depends_on") or [])
    depth = 0
    while frontier and depth < max_depth and len(seen) < max_count:
        next_frontier = []
        for dep in frontier:
            if dep in cmp["paths"] and dep not in seen and dep != path_id:
                seen.append(dep)
                if len(seen) >= max_count:
                    break
                next_frontier.extend(cmp["paths"][dep].get("depends_on") or [])
        frontier = next_frontier
        depth += 1
    return seen
