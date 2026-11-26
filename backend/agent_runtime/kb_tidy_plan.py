"""Deterministic planning for the KB Janitor (sefton tidy pass).

The janitor is an LLM agent with a limited tool budget, so left alone it keeps
auditing whichever docs it happens to Read first. This module decides *what* it
should look at, in code:

* ``plan_batch``  — a per-doc audit ledger turns the vault into a priority queue
  (never audited > changed since audit > longest unaudited), capped per run, plus
  the docs linked to new/changed ones (not counted against the cap).
* ``find_issues`` — heuristic pre-analysis (dangling links, plain-text mentions
  that should be links, duplicate names, empty docs, parenthetical titles) so the
  LLM verifies and fixes instead of hunting.

Pure functions over a scanned vault; no LLM, no DB. The caller owns the ledger.
"""

import difflib
import functools
import os
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

BATCH_CAP = 15            # docs per run drawn from the priority queue
NEIGHBOR_CAP = 30         # safety bound on linked docs added on top of the cap
MAX_ISSUES = 80           # issue lines handed to the LLM
MAX_DANGLING_PER_DOC = 8  # dangling links listed per doc (rest summarised)
_CLOSEST_MIN_RATIO = 0.72  # similarity for a 'closest existing doc' suggestion
_MIN_NAME_LEN = 3         # shorter names are too ambiguous for plain-text matching
_MTIME_EPS = 0.001

_LINK_RE = re.compile(r'\[\[([^\]\n]+)\]\]')
# Fenced blocks (closed, or open to the end of the doc) and inline code spans.
_CODE_RE = re.compile(r'^(```|~~~).*?(?:^\1|\Z)|`[^`\n]+`', re.DOTALL | re.MULTILINE)
_PAREN_TITLE_RE = re.compile(r'\([^)]+\)\s*$')
_SKIP_NAMES = frozenset({"user", "assistant", "note", "notes", "summary"})


class Doc(NamedTuple):
    slug: str
    mtime: float
    title: str
    aliases: Tuple[str, ...]
    body: str
    links: Tuple[str, ...]      # raw [[link]] targets (display/heading stripped)


def _split_frontmatter(text: str):
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm, key = {}, None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item and key is not None:          # block-list entry under the last key
            fm.setdefault(key, [])
            if isinstance(fm[key], list):
                fm[key].append(item.group(1).strip().strip('"').strip("'"))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [p.strip().strip('"').strip("'") for p in inner.split(",")] if inner else []
        elif val == "":
            fm[key] = []                      # may be filled by block-list lines
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, m.group(2)


def _link_target(raw: str) -> str:
    return raw.split('|', 1)[0].split('#', 1)[0].strip()


def scan_vault(kb_dir: str) -> Dict[str, Doc]:
    """Read every ``.md`` doc under *kb_dir* (skipping dot dirs and ``inbox``)."""
    docs: Dict[str, Doc] = {}
    if not kb_dir or not os.path.isdir(kb_dir):
        return docs
    for root, dirs, fnames in os.walk(kb_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'inbox']
        for fn in sorted(fnames):
            if not fn.endswith('.md') or fn.startswith('.'):
                continue
            full = os.path.join(root, fn)
            slug = os.path.relpath(full, kb_dir).replace(os.sep, '/')[:-3]
            try:
                mtime = os.path.getmtime(full)
                with open(full, encoding='utf-8') as f:
                    fm, body = _split_frontmatter(f.read())
            except OSError:
                continue
            title = str(fm.get('title') or slug.rsplit('/', 1)[-1]).strip()
            aliases = fm.get('aliases') or []
            if isinstance(aliases, str):
                aliases = [aliases]
            aliases = tuple(a for a in aliases if isinstance(a, str) and a.strip())
            links = tuple(t for t in (_link_target(r) for r in _LINK_RE.findall(body)) if t)
            docs[slug] = Doc(slug, mtime, title, aliases, body, links)
    return docs


def _names(doc: Doc) -> List[str]:
    return [n for n in (doc.title, *doc.aliases) if n]


def name_index(docs: Dict[str, Doc]) -> Dict[str, List[str]]:
    """``casefold(title|alias) -> [slugs]`` (more than one slug = a name collision)."""
    idx: Dict[str, List[str]] = {}
    for d in docs.values():
        for n in _names(d):
            slugs = idx.setdefault(n.casefold(), [])
            if d.slug not in slugs:
                slugs.append(d.slug)
    return idx


def plan_batch(docs: Dict[str, Doc], ledger: Dict[str, dict],
               cap: int = BATCH_CAP) -> Tuple[List[str], List[str], Dict[str, str]]:
    """Pick this run's docs.

    Returns ``(batch, neighbors, reasons)``: *batch* is at most *cap* slugs ordered
    new → changed → longest-unaudited; *neighbors* are docs linked to/from a new or
    changed batch doc (outside the cap, bounded by ``NEIGHBOR_CAP``); *reasons*
    maps each batch slug to ``new`` / ``changed`` / ``stale``.
    """
    new, changed, audited = [], [], []
    for slug, d in docs.items():
        entry = ledger.get(slug)
        if not entry:
            new.append(d)
        elif d.mtime > float(entry.get('m', 0)) + _MTIME_EPS:
            changed.append(d)
        else:
            audited.append((float(entry.get('t', 0)), d))
    new.sort(key=lambda d: d.mtime, reverse=True)
    changed.sort(key=lambda d: d.mtime, reverse=True)
    audited.sort(key=lambda p: p[0])

    batch, reasons = [], {}
    for reason, group in (("new", new), ("changed", changed),
                          ("stale", [d for _, d in audited])):
        for d in group:
            if len(batch) >= cap:
                break
            batch.append(d.slug)
            reasons[d.slug] = reason

    dirty = [s for s in batch if reasons[s] in ("new", "changed")]
    idx = name_index(docs)
    in_batch = set(batch)
    neighbors: List[str] = []

    def _add(slug: str) -> None:
        if slug not in in_batch and slug not in neighbors and len(neighbors) < NEIGHBOR_CAP:
            neighbors.append(slug)

    for slug in dirty:
        d = docs[slug]
        for target in d.links:                         # docs this one links to
            for s in idx.get(target.casefold(), []):
                _add(s)
        names = {n.casefold() for n in _names(d)}
        for other in docs.values():                    # docs linking to this one
            if other.slug != slug and any(t.casefold() in names for t in other.links):
                _add(other.slug)
    return batch, neighbors, reasons


def _norm(name: str) -> str:
    return re.sub(r'[\s_\-]+', ' ', name.casefold()).strip()


def closest_docs(target: str, docs: Dict[str, Doc], n: int = 2) -> List[Doc]:
    """Existing docs whose slug/title/alias resembles *target* (for dangling
    links, so the LLM needn't search the vault to find the intended doc)."""
    t = _norm(target)
    if len(t) < _MIN_NAME_LEN:
        return []
    scored = []
    for d in docs.values():
        best = 0.0
        for cand in (d.slug.rsplit('/', 1)[-1], *_names(d)):
            c = _norm(cand)
            if not c:
                continue
            if t in c or c in t:
                ratio = 0.9 if min(len(t), len(c)) >= _MIN_NAME_LEN else 0.0
            else:
                m = difflib.SequenceMatcher(None, t, c)
                if m.real_quick_ratio() < _CLOSEST_MIN_RATIO or m.quick_ratio() < _CLOSEST_MIN_RATIO:
                    continue
                ratio = m.ratio()
            best = max(best, ratio)
        if best >= _CLOSEST_MIN_RATIO:
            scored.append((best, d))
    scored.sort(key=lambda p: p[0], reverse=True)
    return [d for _, d in scored[:n]]


def blank_code(body: str) -> str:
    """Body with code spans and fenced blocks blanked (same length), so names
    inside code (identifiers, paths, model ids) are never taken for prose."""
    return _CODE_RE.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), body)


def _unlinked_body(body: str) -> str:
    """Body with existing [[links]] and code blanked so plain-text matching skips them."""
    return _LINK_RE.sub(lambda m: ' ' * len(m.group(0)), blank_code(body))


@functools.lru_cache(maxsize=2048)
def _mention_re(name: str):
    return re.compile(r'(?<![\w\[])' + re.escape(name) + r'(?![\w\]])')


def find_issues(docs: Dict[str, Doc], focus: List[str],
                limit: int = MAX_ISSUES) -> List[str]:
    """Heuristic findings for the *focus* docs; the LLM must verify each one."""
    idx = name_index(docs)
    out: List[str] = []

    def _emit(line: str) -> bool:
        if len(out) < limit:
            out.append(line)
        return len(out) < limit

    for slug in focus:
        d = docs.get(slug)
        if d is None:
            continue
        if not d.body.strip():
            _emit(f"[{slug}] EMPTY body")
        if _PAREN_TITLE_RE.search(d.title):
            _emit(f"[{slug}] WRONG TITLE: parenthetical in title {d.title!r} "
                  f"(move the nickname to aliases)")
        for n in _names(d):
            others = [s for s in idx.get(n.casefold(), []) if s != slug]
            if others:
                _emit(f"[{slug}] DUPLICATE?: name {n!r} also belongs to "
                      f"{', '.join('[' + s + ']' for s in others)}")
        dangling, seen = [], set()
        for t in d.links:
            if t.casefold() not in idx and t.casefold() not in seen:
                seen.add(t.casefold())
                dangling.append(t)
        if dangling:
            parts = []
            for t in dangling[:MAX_DANGLING_PER_DOC]:
                near = closest_docs(t, docs)
                hint = (" -> closest: " + ", ".join(f"[{c.slug}] {c.title}" for c in near)
                        if near else " -> no close doc")
                parts.append(f"[[{t}]]{hint}")
            more = len(dangling) - MAX_DANGLING_PER_DOC
            tail = f" (+{more} more dangling)" if more > 0 else ""
            _emit(f"[{slug}] DANGLING (no doc has that title/alias): "
                  + "; ".join(parts) + tail)

    focus_set = set(focus)
    plain = {s: _unlinked_body(d.body) for s, d in docs.items()}
    # Plain-text mentions, both directions: a focus doc mentioning other docs'
    # names, and any doc mentioning a focus doc's names (retro-link into old docs).
    for slug in focus:
        d = docs.get(slug)
        if d is None:
            continue
        for other in docs.values():
            if other.slug == slug:
                continue
            for n in _names(other):
                if len(n) < _MIN_NAME_LEN or n.casefold() in _SKIP_NAMES:
                    continue
                if _mention_re(n).search(plain[slug]):
                    if not _emit(f"[{slug}] UNLINKED mention of {n!r} "
                                 f"(has doc [{other.slug}])"):
                        return out
            if other.slug in focus_set:
                continue                       # already covered by its own pass
            for n in _names(d):
                if len(n) < _MIN_NAME_LEN or n.casefold() in _SKIP_NAMES:
                    continue
                if _mention_re(n).search(plain[other.slug]):
                    if not _emit(f"[{other.slug}] UNLINKED mention of {n!r} "
                                 f"(has doc [{slug}])"):
                        return out
    return out


def render_plan(docs: Dict[str, Doc], batch: List[str], neighbors: List[str],
                reasons: Dict[str, str]) -> str:
    """Task-message section describing this run's focus batch."""
    lines = [f"FOCUS BATCH ({len(batch)} of {len(docs)} docs; audit these FIRST):"]
    for s in batch:
        lines.append(f"- [{s}] {docs[s].title} ({reasons[s]})")
    if neighbors:
        lines.append("")
        lines.append("LINKED TO THE NEW/CHANGED DOCS ABOVE (also in scope):")
        for s in neighbors:
            lines.append(f"- [{s}] {docs[s].title}")
    return "\n".join(lines)


def audit_entries(docs_after: Dict[str, Doc], slugs: List[str], now: float) -> Dict[str, dict]:
    """Ledger entries for *slugs* using their CURRENT mtime (so the janitor's own
    edits don't make them look 'changed' on the next run)."""
    return {s: {"m": docs_after[s].mtime, "t": now} for s in slugs if s in docs_after}
