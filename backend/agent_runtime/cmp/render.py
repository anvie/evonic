"""
CMP — session map + card rendering for LLM context injection.

Map notation (paper figure / user's goal spec): edges carry the path's
action label and originate from the dependency parent (or the Agent root
for independent paths); the active node is marked with a trailing `*`:

    flowchart TD
      Agent((Agent))
      Agent -->|create report| A1["Perusahaan A"]
      Agent -->|create article| A2["blog"]
      A1 -->|create invoice| B1["client X"]
      A1 -->|create invoice| B2["client Y *"]

Path ids are level-based (A=root, B=child, C=grandchild…) — see
store._compute_path_id. Tiers below the map: the active path renders its
full IPPC card; dependency ancestors of the active path render compact
cards; EVERY other path renders a one-line snippet (its detail is
offloaded — only the interface-preserving summary stays in context).
"""
from __future__ import annotations

import re

from backend.agent_runtime.cmp.store import (dependency_ancestors, path_status,
                                              sort_path_ids)

RENDER_MAX_CHARS = 6000

_LABEL_SAFE_RE = re.compile(r'[|"\[\]{}<>`]')


def _safe(text: str, cap: int) -> str:
    return _LABEL_SAFE_RE.sub('', str(text or ''))[:cap].strip()


def render_map(cmp: dict, agent_name: str = "Agent") -> str:
    """Mermaid flowchart: action-labelled edges from the dependency parent
    (agent root for independent paths), `*` marks the active node.

    The root node's mermaid ID stays `Agent` (ids must be identifier-like —
    agent names can contain spaces); only the display label is dynamic.
    """
    safe_name = _safe(agent_name, 24) or "Agent"
    lines = ["flowchart TD", f"  Agent(({safe_name}))"]
    ordered = sort_path_ids(cmp["paths"])
    for pid in ordered:
        path = cmp["paths"][pid]
        star = " *" if pid == cmp["active_id"] else ""
        node = f'{pid}["{_safe(path.get("title"), 48)}{star}"]'
        label = _safe(path.get("action") or "task", 32)
        deps = [d for d in (path.get("depends_on") or []) if d in cmp["paths"]]
        source = deps[0] if deps else "Agent"
        lines.append(f"  {source} -->|{label}| {node}")
        for extra in deps[1:]:
            lines.append(f"  {pid} -. also uses .-> {extra}")
    return "\n".join(lines)


def _full_card(path: dict) -> list:
    lines = [f"**{path['id']} — {path.get('title') or '(untitled)'}** (active)"]
    if path.get("goal"):
        lines.append(f"- goal: {path['goal']}")
    if path.get("outcome"):
        lines.append(f"- outcome so far: {path['outcome']}")
    for fact in path.get("key_facts") or []:
        lines.append(f"- {fact}")
    if path.get("artifacts"):
        lines.append(f"- artifacts: {', '.join(path['artifacts'])}")
    if path.get("depends_on"):
        lines.append(f"- depends on: {', '.join(path['depends_on'])}")
    return lines


def _compact_card(path: dict, reason: str) -> list:
    lines = [f"**{path['id']} — {path.get('title') or '(untitled)'}** ({reason})"]
    if path.get("goal"):
        lines.append(f"- goal: {path['goal']}")
    if path.get("outcome"):
        lines.append(f"- outcome: {path['outcome']}")
    for fact in (path.get("key_facts") or [])[:3]:
        lines.append(f"- {fact}")
    if path.get("artifacts"):
        lines.append(f"- artifacts: {', '.join(path['artifacts'][:4])}")
    return lines


def _snippet_card(path: dict) -> str:
    summary = path.get("outcome") or path.get("goal") or ""
    return (f"- {path['id']} {path.get('title') or ''} "
            f"[{path_status(path)}] — {summary[:100]}")


def render_cmp_section(cmp: dict, agent_name: str = "Agent") -> str:
    """Full CMP block for the agent-state system message."""
    if not cmp or not cmp.get("paths"):
        return ""
    active_id = cmp["active_id"]
    lines = ["```mermaid", render_map(cmp, agent_name), "```", ""]

    active = cmp["paths"].get(active_id)
    if active:
        lines.extend(_full_card(active))

    ancestors = dependency_ancestors(cmp, active_id)
    for dep_id in ancestors:
        lines.append("")
        lines.extend(_compact_card(cmp["paths"][dep_id], "dependency of active path"))

    # Pinned waypoints: non-active paths this turn references (set by the
    # detector's pin field or query-relevant auto-pinning). Rendered as
    # compact cards — key facts included — even when archived, so cross-path
    # recap/compare queries can be answered without switching. Transient:
    # refreshed every turn. Kept in a SEPARATE block so the budget trim below
    # can protect it: this is the most query-relevant content in the section.
    pinned_ids = [pid for pid in (cmp.get("pinned_ids") or [])
                  if pid in cmp["paths"] and pid != active_id and pid not in ancestors]
    pin_excerpts = cmp.get("pin_excerpts") or {}
    pin_lines = []
    for pid in sort_path_ids({p: cmp["paths"][p] for p in pinned_ids}):
        pin_lines.append("")
        pin_lines.extend(_compact_card(cmp["paths"][pid], "pinned for this request"))
        # transcript-recall excerpts: the matching raw lines from this path's
        # own segments — carries facts the card never lifted into key_facts.
        for ln in (pin_excerpts.get(pid) or [])[:3]:
            pin_lines.append(f"  » {ln}")

    others = [cmp["paths"][pid] for pid in sort_path_ids(cmp["paths"])
              if pid != active_id and pid not in ancestors and pid not in pinned_ids]
    # Lifecycle tiers: preserved paths keep their snippet card; archived
    # paths contribute their title only (their map node) — the next rung
    # down in context cost before pruning removes them entirely.
    preserved = [p for p in others if path_status(p) != "archived"]
    archived = [p for p in others if path_status(p) == "archived"]
    rest_lines = []
    if preserved:
        rest_lines.append("")
        rest_lines.append("Offloaded paths (details load on return):")
        rest_lines.extend(_snippet_card(p) for p in preserved)
    if archived:
        rest_lines.append("")
        rest_lines.append("Archived paths (title + tags — recall(query) searches them, "
                          "read_transcript(id) reads them):")
        for p in archived:
            tags = ", ".join((p.get("tags") or [])[:6])
            tag_note = f" · tags: {tags}" if tags else ""
            rest_lines.append(f"- {p['id']} {p.get('title') or ''} [archived]{tag_note}")

    tail = ("\nTo retrieve an earlier task's details: recall(query) searches "
            "all paths, read_transcript(path_id) reads one, switch_path(path_id) "
            "resumes it, new_path(title) starts an unrelated task.")

    # Budget-aware assembly (most query-relevant content survives first):
    # pinned recall > head (map + active + ancestors) > tiered lists. A blind
    # tail-truncate would silently drop pinned cards behind a large map —
    # exactly the content the current request needs.
    head = "\n".join(lines)
    pins = "\n".join(pin_lines)
    rest = "\n".join(rest_lines)
    def _truncate(text, limit, marker):
        if len(text) <= limit:
            return text
        return text[:max(0, limit - len(marker))] + marker if limit > 0 else ""

    budget = RENDER_MAX_CHARS - len(tail)
    pins = _truncate(pins, budget, "\n…[pinned cards truncated]")
    remaining = budget - len(pins)
    head = _truncate(head, remaining, "\n…[session map truncated]")
    remaining -= len(head)
    rest = _truncate(rest, remaining,
                     "\n…[path list truncated — recall(query) searches all paths]")
    return head + pins + rest + tail
