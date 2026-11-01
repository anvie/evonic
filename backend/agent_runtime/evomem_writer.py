"""
evomem_writer.py — write structured markdown docs into an agent's evomem.

Evomem treats disk as the source of truth: docs are markdown files, the database
is derived via `sync`. This module writes *rich, inline-linked* docs (the
Obsidian/wiki model) and schedules a debounced `sync` so the graph is built off
the hot path.

Model
-----
- A **doc** is one markdown file with frontmatter (`title`, `type`,
  `description`, optional `tags`/`aliases`, `created`/`updated`) and a body of
  rich prose. Relationships are expressed as **inline** `[[Doc Title]]`
  wiki-links woven into the sentences — never a separate "Relations" list.
- Docs live at the knowledge root or inside a **collection** folder, one level
  under kb/ (e.g. `kb/riset-xyz/`), created on the user's request. Each collection
  has an `index.md` of `type: session` or `type: group` describing it.
- There is no `entities/` directory: an entity like "Jakarta" is just a doc with
  `type: place`. Links resolve to a doc by title/alias anywhere in the vault, so
  callers link by display title, not by path.

All writes are atomic and best-effort; failures are swallowed so the FTS5 memory
pipeline is never affected.
"""

from __future__ import annotations

import os
import re
import logging
import threading
import unicodedata
from datetime import datetime, timezone

from backend.agent_runtime.evomem_client import (
    _get_evomem_dir, init_evomem, sync as _evomem_sync, vlog,
)

logger = logging.getLogger(__name__)

# Debounce window (seconds) for coalescing a burst of writes into one sync.
_SYNC_DEBOUNCE_SECONDS = float(os.environ.get("EVOMEM_SYNC_DEBOUNCE", "5"))

# Typed edge labels evomem can carry on a link (used to populate the `recall`
# graph-traversal edge_type filter). The engine infers these from the sentence
# around an inline link; any other label is stored as a custom edge type.
EDGE_TYPES = {
    "founded", "invested_in", "works_at", "advises", "attended",
    "located_in", "lives_in", "visited", "born_in", "part_of",
    "member_of", "owns", "uses", "knows", "related_to", "mentions",
}

# Doc types accepted by evomem's validator (mirrors Rust validate::VALID_TYPES).
DOC_TYPES = {
    "note", "session", "group", "person", "place", "venue", "event",
    "organization", "company", "product", "contact",
}

# Per-agent debounced-sync timers, guarded by a lock.
_sync_timers: dict = {}
_sync_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    """Deterministic slug from a name: lowercase ascii, dashes, capped length.

    Same input always yields the same slug, so a doc maps to a stable file
    (dedup by construction). Returns '' if nothing usable remains.
    """
    if not name:
        return ""
    norm = unicodedata.normalize("NFKD", name)
    norm = norm.encode("ascii", "ignore").decode("ascii")
    norm = norm.lower()
    norm = re.sub(r"[^a-z0-9]+", "-", norm)
    norm = norm.strip("-")
    return norm[:60].strip("-")


def _doc_path(agent_id: str, rel_slug: str) -> str:
    """Absolute path to a doc file under the agent's brain dir.

    ``rel_slug`` is a knowledge-root-relative slug that may include folder
    segments (e.g. ``riset-xyz/foo`` -> ``kb/riset-xyz/foo.md``).
    """
    rel = (rel_slug or "").strip("/").replace("..", "")
    return os.path.abspath(os.path.join(_get_evomem_dir(agent_id), f"{rel}.md"))


def _ensure_brain(agent_id: str) -> bool:
    """Make sure the brain DB exists (idempotent). Returns False if unavailable."""
    brain_dir = _get_evomem_dir(agent_id)
    if os.path.isdir(brain_dir) and os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return True
    return init_evomem(agent_id)


def _atomic_write(path: str, content: str) -> None:
    """Write content atomically (temp file in same dir + os.replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _yaml_escape(s: str) -> str:
    """Quote a scalar for YAML frontmatter (double-quoted, escaped)."""
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


def _yaml_list(items) -> str:
    """Render a flow-style YAML list of scalars."""
    return "[" + ", ".join(_yaml_escape(str(i)) for i in items) + "]"


def _parse_frontmatter(text: str):
    """Split a markdown doc into (frontmatter_dict, body).

    Minimal YAML: scalar and flow-list values only. Unknown structure is kept
    as a raw string so we never lose data on rewrite.
    """
    fm, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return fm, text
    raw, body = m.group(1), m.group(2)
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            items = []
            if inner:
                items = [p.strip().strip('"').strip("'") for p in inner.split(",")]
            fm[key] = [i for i in items if i]
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, body


def _render_doc(title: str, doc_type: str, description: str, tags, aliases,
                created: str, updated: str, body: str,
                thumbnail: str = None, expires: str = None) -> str:
    """Render a full doc (frontmatter + body). Body is used verbatim (its inline
    [[wiki-links]] are the graph edges)."""
    lines = ["---",
             f"title: {_yaml_escape(title)}",
             f"type: {doc_type if doc_type in DOC_TYPES else 'note'}",
             f"description: {_yaml_escape(description)}",
             f"tags: {_yaml_list(tags or [])}"]
    if aliases:
        lines.append(f"aliases: {_yaml_list(aliases)}")
    if thumbnail:
        lines.append(f"thumbnail: {_yaml_escape(thumbnail)}")
    if expires:                                   # optional lifetime: see sweep_expired
        lines.append(f"expires: {expires}")
    lines.append(f"created: {created}")
    lines.append(f"updated: {updated}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def _folder_title(folder: str) -> str:
    return folder.replace("-", " ").strip().capitalize() or folder


def _ensure_collection(agent_id: str, folder: str) -> bool:
    """Make ``folder`` a collection if it is not one yet. Evomem only indexes the knowledge root and collection
    folders (a folder with an ``index.md``): a document written into any other folder exists on disk and in the list
    view but is silently left out of the index, the graph and search."""
    folder = (folder or "").strip("/")
    if not folder or folder == "inbox" or slugify(folder) != folder:
        return False
    if os.path.exists(_doc_path(agent_id, f"{folder}/index")):
        return True
    return bool(create_collection(agent_id, folder, _folder_title(folder), kind="group"))


def ensure_collections(agent_id: str) -> list:
    """Repair: every folder directly under the knowledge root that holds documents but has no ``index.md`` becomes a
    collection, so its documents are indexed on the next sync. Hidden folders and ``inbox`` are left alone.
    Returns the folders it fixed."""
    root = _get_evomem_dir(agent_id)
    fixed = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return fixed
    for name in names:
        full = os.path.join(root, name)
        if name.startswith(".") or name == "inbox" or not os.path.isdir(full):
            continue
        has_docs = any(f.endswith(".md") for f in os.listdir(full))
        if has_docs and not os.path.exists(os.path.join(full, "index.md")) and _ensure_collection(agent_id, name):
            fixed.append(name)
    return fixed


def delete_doc(agent_id: str, slug: str) -> bool:
    """Remove a document (its file) from the knowledge base and schedule a sync so it leaves the index and the graph.
    Collection indexes are never deleted. Returns True if a file was removed."""
    slug = (slug or "").strip("/")
    if not slug or slug == "index" or slug.endswith("/index"):
        return False
    path = _doc_path(agent_id, slug)
    root = os.path.abspath(_get_evomem_dir(agent_id))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return False
    try:
        os.remove(path)
    except OSError as e:
        logger.debug("delete_doc failed for %s/%s: %s", agent_id, slug, e)
        return False
    vlog("writer[%s]: delete_doc %s", agent_id, slug)
    mark_dirty(agent_id)
    return True


def list_docs(agent_id: str, limit: int = 0) -> list:
    """The agent's knowledge documents, most recently updated first: ``[{slug, title, description, tags, updated,
    body}]``. Collection indexes, hidden folders and ``inbox`` are skipped. ``limit`` 0 means all."""
    root = os.path.abspath(_get_evomem_dir(agent_id))
    docs = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "inbox"]
        for f in files:
            if not f.endswith(".md") or f == "index.md":
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    fm, body = _parse_frontmatter(fh.read())
            except OSError:
                continue
            slug = os.path.relpath(os.path.join(dirpath, f), root)[:-3].replace(os.sep, "/")
            docs.append({"slug": slug, "title": fm.get("title") or slug.rsplit("/", 1)[-1],
                         "description": fm.get("description", ""), "tags": fm.get("tags", []) or [],
                         "updated": fm.get("updated", ""), "body": body.strip()})
    docs.sort(key=lambda d: d["updated"], reverse=True)
    return docs[:limit] if limit else docs


def parse_expiry(value) -> "datetime | None":
    """A doc's ``expires`` as a UTC datetime. A bare date means the end of that day. None when absent or unreadable
    (an unreadable value never makes a doc expire)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def set_expiry(agent_id: str, slug: str, expires: str = None) -> bool:
    """Give a doc a lifetime (an ISO date or date-time), or remove it (``None``) so the doc is kept for good."""
    path = _doc_path(agent_id, slug)
    try:
        with open(path, encoding="utf-8") as f:
            fm, body = _parse_frontmatter(f.read())
        title = fm.get("title") or slug.rsplit("/", 1)[-1]
        _atomic_write(path, _render_doc(title, fm.get("type") or "note", fm.get("description") or title,
                                        fm.get("tags", []) or [], fm.get("aliases", []) or [],
                                        fm.get("created") or _now_iso(), _now_iso(), body,
                                        thumbnail=fm.get("thumbnail"), expires=(expires or None)))
        return True
    except (OSError, ValueError) as e:
        logger.debug("set_expiry failed for %s/%s: %s", agent_id, slug, e)
        return False


def sweep_expired(agent_id: str, now: "datetime | None" = None) -> dict:
    """Delete the docs whose ``expires`` has passed. Docs without one are never touched. A doc that other docs link
    to is not deleted (that would leave dangling links): it loses its expiry and is kept for good. Collection
    indexes, hidden folders and ``inbox`` are skipped. Returns ``{'deleted': [slugs], 'kept': [slugs]}``."""
    now = now or datetime.now(timezone.utc)
    root = os.path.abspath(_get_evomem_dir(agent_id))
    out = {"deleted": [], "kept": []}
    due = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "inbox"]
        for f in files:
            if not f.endswith(".md") or f == "index.md":
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    fm, _ = _parse_frontmatter(fh.read())
            except OSError:
                continue
            when = parse_expiry(fm.get("expires"))
            if when and when <= now:
                due.append(os.path.relpath(os.path.join(dirpath, f), root)[:-3].replace(os.sep, "/"))
    for slug in due:
        if linked_from(agent_id, slug):
            if set_expiry(agent_id, slug, None):
                out["kept"].append(slug)
        elif delete_doc(agent_id, slug):
            out["deleted"].append(slug)
    return out


def linked_from(agent_id: str, slug: str) -> list:
    """Slugs of the other documents that link to this one with ``[[wiki links]]`` (by title, alias or slug). Read from
    the files themselves, so it works for documents that have not been indexed. Collection indexes do not count."""
    doc = read_doc(agent_id, slug)
    if doc is None:
        return []
    fm = doc.get("frontmatter") or {}
    names = {str(n).strip().lower() for n in [doc.get("title"), slug.rsplit("/", 1)[-1], *(fm.get("aliases") or [])] if n}
    root = os.path.abspath(_get_evomem_dir(agent_id))
    found = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in files:
            if not f.endswith(".md") or f == "index.md":
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), root)[:-3].replace(os.sep, "/")
            if rel == slug:
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    text = fh.read().lower()
            except OSError:
                continue
            if any(f"[[{n}]]" in text or f"[[{n}|" in text for n in names):
                found.append(rel)
    return found


def upsert_doc(agent_id: str, title: str, body: str, doc_type: str = "note",
               description: str = None, folder: str = "", tags=None,
               aliases=None, slug: str = None, thumbnail: str = None, expires: str = None) -> str:
    """Create or overwrite a doc. Returns its full slug (``<folder>/<slug>``) or ''.

    ``folder`` places the doc inside a collection (e.g. ``riset-xyz``); empty =
    root. ``expires`` (an ISO date or date-time) gives the doc an optional lifetime: once it has passed,
    :func:`sweep_expired` deletes the doc. A doc without it is never deleted; on an existing doc a missing
    ``expires`` keeps the one it has, and ``expires=""`` removes it. ``body`` is rich prose with inline ``[[Doc Title]]`` links — written
    verbatim, with no appended link block. On an existing doc the original
    ``created`` is kept, ``updated`` is refreshed, and tags/aliases are merged
    (union); the caller supplies the already-merged body.

    ``session``/``group`` are reserved for collection ``index.md`` files (created
    via :func:`create_collection`); a standalone doc must not carry them, so they
    are coerced to ``note`` here — otherwise an LLM type guess could mint a flat
    "session" file that looks like a collection but isn't one.
    """
    if doc_type in ("session", "group"):
        doc_type = "note"
    base = (slug or slugify(title)).strip("/")
    if not base or not _ensure_brain(agent_id):
        return ""
    folder = (folder or "").strip("/")
    if folder:
        _ensure_collection(agent_id, folder)          # a document in a folder must not fall outside the index
    rel = f"{folder}/{base}" if folder else base
    path = _doc_path(agent_id, rel)
    tags = list(tags or [])
    aliases = [a for a in (aliases or []) if a and a != title]
    created = _now_iso()
    # A caller-supplied thumbnail wins; otherwise preserve any existing one.
    thumbnail = (thumbnail or "").strip() or None
    keep_expires = expires is None            # None: leave an existing doc's lifetime alone; "": remove it

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                fm, _old = _parse_frontmatter(f.read())
            created = fm.get("created") or created
            tags = sorted(set(tags) | set(fm.get("tags", []) or []))
            aliases = sorted(set(aliases) | set(fm.get("aliases", []) or []))
            thumbnail = thumbnail or (fm.get("thumbnail") or None)
            if keep_expires:
                expires = fm.get("expires") or None
            if description is None:
                description = fm.get("description")
            # Don't downgrade a typed doc to a plain note on re-write — but never
            # adopt a stale session/group type from a mis-typed flat file.
            existing_type = fm.get("type")
            if (not doc_type or doc_type == "note") and existing_type \
                    and existing_type not in ("session", "group"):
                doc_type = existing_type
        except Exception:
            pass
    description = (description or title).strip()

    try:
        _atomic_write(path, _render_doc(title, doc_type, description, tags,
                                        aliases, created, _now_iso(), body,
                                        thumbnail=thumbnail, expires=(expires or None)))
        vlog("writer[%s]: upsert_doc %s (type=%s)", agent_id, rel, doc_type)
        return rel
    except Exception as e:
        logger.debug("upsert_doc failed for %s/%s: %s", agent_id, rel, e)
        return ""


# A YAML frontmatter delimiter followed by a frontmatter key — finding this INSIDE
# a body means a whole copy of the file was concatenated into it (self-duplication).
_EMBEDDED_FRONTMATTER_RE = re.compile(
    r'\n-{3,}[ \t]*\n[ \t]*(?:title|type|aliases|description|tags|created|updated)[ \t]*:')


def _dedupe_body(body: str, min_block: int = 40) -> str:
    """Remove self-concatenation duplication from a doc body:

    1. If a second YAML frontmatter block got concatenated in (the whole file was
       appended to itself), cut it and everything after.
    2. Drop any later paragraph block that VERBATIM-duplicates an earlier
       substantial block (>= ``min_block`` chars), keeping the first occurrence.

    Conservative: only exact duplicates of substantial blocks are removed, so
    legitimate short/again-different content is never touched.
    """
    if not body:
        return body
    m = _EMBEDDED_FRONTMATTER_RE.search(body)
    if m:
        body = body[:m.start()]
    seen, out = set(), []
    for block in re.split(r'\n[ \t]*\n', body):
        key = block.strip()
        if len(key) >= min_block and key in seen:
            continue
        if len(key) >= min_block:
            seen.add(key)
        out.append(block.rstrip())
    return "\n\n".join(b for b in out if b.strip()).strip()


def append_to_doc(agent_id: str, slug: str, delta_prose: str,
                  thumbnail: str = None) -> bool:
    """Append a new inline-linked paragraph to an existing doc, preserving the
    body and refreshing ``updated``. Optionally set/refresh the ``thumbnail``
    frontmatter (a caller-supplied value wins; otherwise the existing one is kept).
    Returns True if anything was written.
    """
    delta = (delta_prose or "").strip()
    new_thumb = (thumbnail or "").strip() or None
    path = _doc_path(agent_id, slug)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            fm, body = _parse_frontmatter(f.read())
        existing_thumb = fm.get("thumbnail") or None
        final_thumb = new_thumb or existing_thumb
        # No-op when there's no new prose and the thumbnail wouldn't change.
        if not delta and final_thumb == existing_thumb:
            return False
        title = fm.get("title") or slug.rsplit("/", 1)[-1]
        doc_type = fm.get("type") or "note"
        description = fm.get("description") or title
        created = fm.get("created") or _now_iso()
        # Dedupe the result so a delta that restates existing content can't
        # concatenate a duplicate copy into the doc.
        new_body = _dedupe_body(body.rstrip() + "\n\n" + delta) if delta else body
        _atomic_write(path, _render_doc(title, doc_type, description,
                                        fm.get("tags", []) or [],
                                        fm.get("aliases", []) or [],
                                        created, _now_iso(), new_body,
                                        thumbnail=final_thumb, expires=fm.get("expires")))
        vlog("writer[%s]: append_to_doc %s (+%d chars%s)", agent_id, slug, len(delta),
             ", +thumbnail" if new_thumb and new_thumb != existing_thumb else "")
        return True
    except Exception as e:
        logger.debug("append_to_doc failed for %s/%s: %s", agent_id, slug, e)
        return False


def _levenshtein(a: str, b: str) -> int:
    """Levenshtein edit distance between two strings (iterative, O(len(a)*len(b)))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# Fuzzy-edit safety knobs (env-overridable).
_EDIT_FUZZY_RATIO = float(os.environ.get("EVOMEM_KB_EDIT_FUZZY_RATIO", "0.85"))
_EDIT_FUZZY_MARGIN = float(os.environ.get("EVOMEM_KB_EDIT_FUZZY_MARGIN", "0.08"))
_EDIT_FUZZY_MIN_LEN = 8       # don't fuzzy-match very short needles (too easy to mis-hit)
_EDIT_FUZZY_MAX_BODY = 20000  # skip fuzzy on very large bodies (perf)


def _fuzzy_unique_window(body: str, needle: str):
    """Find the body span most similar to ``needle`` by Levenshtein ratio.

    Returns ``(start, end)`` ONLY for a high-confidence, UNAMBIGUOUS match:
    similarity ≥ ratio threshold AND no *other non-overlapping* span scores within
    ``margin`` of the best. Otherwise None — so a fuzzy edit never clobbers the
    wrong (or an equally-plausible) region. None for very short needles / huge bodies.
    """
    n = len(needle)
    if n < _EDIT_FUZZY_MIN_LEN or not body or len(body) > _EDIT_FUZZY_MAX_BODY:
        return None
    # Bound the Levenshtein scan cost (~ body * needle^2). A long needle on a big
    # body (e.g. an edit that mistakenly pastes a whole frontmatter block) would be
    # pathologically slow — and such a non-body needle won't match anyway, so refuse.
    if len(body) * n * n > 40_000_000:
        return None
    lengths = {max(1, n + d) for d in (-1, 0, 1)}   # absorb small indels
    matches = []  # (ratio, start, end)
    for wlen in lengths:
        for start in range(0, len(body) - wlen + 1):
            window = body[start:start + wlen]
            ratio = 1.0 - _levenshtein(window, needle) / max(wlen, n)
            if ratio >= _EDIT_FUZZY_RATIO:
                matches.append((ratio, start, start + wlen))
    if not matches:
        return None
    matches.sort(reverse=True)
    br, bs, be = matches[0]
    for r, s, e in matches[1:]:
        if (e <= bs or s >= be) and r >= br - _EDIT_FUZZY_MARGIN:
            return None   # a different region matches nearly as well → refuse
    return (bs, be)


def replace_in_doc(agent_id: str, slug: str, old_str: str, new_str: str,
                   fuzzy: bool = True) -> bool:
    """Surgical edit: replace ``old_str`` with ``new_str`` in a doc's body.

    SAFE BY DESIGN. Resolution order:
      1. EXACT, unique (``old_str`` occurs exactly once) → replace.
      2. EXACT but >1 occurrences → refuse (ambiguous).
      3. Not found → FUZZY: replace the single high-similarity, unambiguous span
         (Levenshtein ratio, see ``_fuzzy_unique_window``) so a near-miss copy of
         ``old_str`` (whitespace/typo drift) still lands — but a vague or
         multiply-matching edit is refused. Returns False on any refusal/no write.

    Body-only; frontmatter is preserved. Used to fix a wrong inline ``[[link]]`` or
    a factual error in existing prose.
    """
    if not old_str or old_str == new_str:
        return False
    path = _doc_path(agent_id, slug)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            fm, body = _parse_frontmatter(f.read())
        cnt = body.count(old_str)
        if cnt == 1:
            new_body = body.replace(old_str, new_str, 1)
            how = "exact"
        elif cnt > 1:
            return False                       # ambiguous exact → refuse
        elif fuzzy and (win := _fuzzy_unique_window(body, old_str)):
            s, e = win
            new_body = body[:s] + new_str + body[e:]
            how = "fuzzy"
        else:
            return False                       # not found (and no safe fuzzy match)
        title = fm.get("title") or slug.rsplit("/", 1)[-1]
        doc_type = fm.get("type") or "note"
        description = fm.get("description") or title
        created = fm.get("created") or _now_iso()
        _atomic_write(path, _render_doc(title, doc_type, description,
                                        fm.get("tags", []) or [],
                                        fm.get("aliases", []) or [],
                                        created, _now_iso(), new_body,
                                        thumbnail=fm.get("thumbnail"), expires=fm.get("expires")))
        vlog("writer[%s]: replace_in_doc %s (%s)", agent_id, slug, how)
        return True
    except Exception as e:
        logger.debug("replace_in_doc failed for %s/%s: %s", agent_id, slug, e)
        return False


def _first_unlinked_span(body: str, text: str):
    """Span of the FIRST plain-text (not already inside ``[[ ]]``) occurrence of
    ``text`` in ``body``, or None. Boundaries reject partial-word hits ("ERP" in
    "ERPNext") and any mention already adjacent to ``[``/``]``/``|`` (i.e. already a
    link or alias), so a re-link is never attempted on text that's already linked.
    """
    m = re.search(r'(?<![\w\[|])' + re.escape(text) + r'(?![\w\]|])', body)
    return (m.start(), m.end()) if m else None


def link_in_doc(agent_id: str, slug: str, text: str, target: str = "") -> bool:
    """Retro-link: wrap the FIRST un-bracketed mention of ``text`` in a doc's body
    as an inline ``[[link]]``. Deterministic — the model only names the phrase and
    its target doc, so (unlike ``edit`` with old_str/new_str) it can never emit a
    no-op where the replacement equals the target.

    ``target`` is the canonical doc title to link to; defaults to ``text``. When it
    differs from the visible phrase, an alias link ``[[target|text]]`` is written so
    the prose still reads naturally. Body-only; frontmatter is preserved. Returns
    False when ``text`` is empty, the doc is missing, or no un-linked mention exists.
    """
    text = (text or "").strip()
    if not text:
        return False
    target = (target or "").strip() or text
    path = _doc_path(agent_id, slug)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            fm, body = _parse_frontmatter(f.read())
        span = _first_unlinked_span(body, text)
        if span is None:
            return False                       # already linked / not present → no-op
        s, e = span
        link = f"[[{target}]]" if target == text else f"[[{target}|{text}]]"
        new_body = body[:s] + link + body[e:]
        title = fm.get("title") or slug.rsplit("/", 1)[-1]
        doc_type = fm.get("type") or "note"
        description = fm.get("description") or title
        created = fm.get("created") or _now_iso()
        _atomic_write(path, _render_doc(title, doc_type, description,
                                        fm.get("tags", []) or [],
                                        fm.get("aliases", []) or [],
                                        created, _now_iso(), new_body,
                                        thumbnail=fm.get("thumbnail"), expires=fm.get("expires")))
        vlog("writer[%s]: link_in_doc %s -> [[%s]]", agent_id, slug, target)
        return True
    except Exception as e:
        logger.debug("link_in_doc failed for %s/%s: %s", agent_id, slug, e)
        return False


def create_collection(agent_id: str, folder: str, title: str,
                     kind: str = "session", description: str = None) -> str:
    """Create a collection folder (one level under kb/) with an ``index.md`` of
    ``type: session|group``. Idempotent. Returns the folder slug or ''.
    """
    folder = slugify(folder)
    if not folder or kind not in ("session", "group") or not _ensure_brain(agent_id):
        return ""
    path = _doc_path(agent_id, f"{folder}/index")
    description = (description or title).strip()
    if os.path.exists(path):
        return folder  # idempotent: keep existing index
    body = f"{description}\n\n## Contents\n"
    try:
        _atomic_write(path, _render_doc(title, kind, description, [kind], [],
                                        _now_iso(), _now_iso(), body))
        vlog("writer[%s]: create_collection %s (%s)", agent_id, folder, kind)
        return folder
    except Exception as e:
        logger.debug("create_collection failed for %s/%s: %s", agent_id, folder, e)
        return ""


def add_to_collection_index(agent_id: str, folder: str, doc_title: str) -> bool:
    """Append ``- [[doc_title]]`` under the collection index's ``## Contents``
    section (idempotent — skips if the link is already present)."""
    folder = (folder or "").strip("/")
    doc_title = (doc_title or "").strip()
    if not folder or not doc_title:
        return False
    path = _doc_path(agent_id, f"{folder}/index")
    if not os.path.exists(path):
        return False
    link = f"[[{doc_title}]]"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if link in content:
            return False
        if "## Contents" in content:
            content = content.rstrip() + f"\n- {link}\n"
        else:
            content = content.rstrip() + f"\n\n## Contents\n- {link}\n"
        _atomic_write(path, content)
        return True
    except Exception as e:
        logger.debug("add_to_collection_index failed for %s/%s: %s", agent_id, folder, e)
        return False


def dedupe_doc(agent_id: str, slug: str) -> bool:
    """Clean a doc that got self-duplicated (a copy of its content concatenated into
    the body). Rewrites the body via :func:`_dedupe_body`, preserving frontmatter.
    Returns True if the doc changed, False if it was already clean / missing.
    """
    path = _doc_path(agent_id, slug)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            fm, body = _parse_frontmatter(f.read())
        cleaned = _dedupe_body(body)
        if cleaned.strip() == (body or "").strip():
            return False  # already clean
        title = fm.get("title") or slug.rsplit("/", 1)[-1]
        _atomic_write(path, _render_doc(title, fm.get("type") or "note",
                                        fm.get("description") or title,
                                        fm.get("tags", []) or [], fm.get("aliases", []) or [],
                                        fm.get("created") or _now_iso(), _now_iso(), cleaned,
                                        thumbnail=fm.get("thumbnail"), expires=fm.get("expires")))
        vlog("writer[%s]: dedupe_doc %s (%d -> %d chars)", agent_id, slug, len(body), len(cleaned))
        return True
    except Exception as e:
        logger.debug("dedupe_doc failed for %s/%s: %s", agent_id, slug, e)
        return False


def read_doc(agent_id: str, slug: str) -> dict | None:
    """Read a doc: returns ``{title, body, frontmatter}`` or None if missing.
    Used by the authoring pipeline for dedupe/merge decisions."""
    path = _doc_path(agent_id, slug)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except (FileNotFoundError, OSError):
        return None
    fm, body = _parse_frontmatter(raw)
    return {"title": fm.get("title", ""), "body": body.strip(), "frontmatter": fm}


def rename_doc(agent_id: str, old_slug: str, new_title: str, add_alias: bool = True) -> str:
    """Rename a doc to fix a typo/misspelled name. Returns the new slug, or ''.

    Renames the file (slug follows the new title) inside the same folder and sets
    the new ``title``, preserving body/type/description/tags/created. The OLD title
    is kept as an alias (so existing inline ``[[Old Name]]`` links still resolve)
    unless ``add_alias`` is False. Old file is removed when the slug actually changes.
    """
    doc = read_doc(agent_id, old_slug)
    if doc is None:
        return ""
    fm, body = doc["frontmatter"], doc["body"]
    old_slug = (old_slug or "").strip("/")
    folder, _, _base = old_slug.rpartition("/")  # folder == '' when no slash
    new_base = slugify(new_title)
    if not new_base:
        return ""
    new_rel = f"{folder}/{new_base}" if folder else new_base

    aliases = list(fm.get("aliases") or [])
    old_title = (fm.get("title") or "").strip()
    if add_alias and old_title and old_title.casefold() != new_title.strip().casefold() \
            and old_title not in aliases:
        aliases.append(old_title)

    rel = upsert_doc(agent_id, title=new_title, body=body,
                     doc_type=fm.get("type") or "note", description=fm.get("description"),
                     folder=folder, tags=fm.get("tags") or [], aliases=aliases, slug=new_base)
    if not rel:
        return ""
    if new_rel != old_slug:
        try:
            os.remove(_doc_path(agent_id, old_slug))
        except OSError:
            pass
    vlog("writer[%s]: rename_doc %s -> %s", agent_id, old_slug, new_rel)
    return new_rel


def _do_sync(agent_id: str) -> None:
    with _sync_lock:
        _sync_timers.pop(agent_id, None)
    logger.info("evomem_writer[%s]: debounced sync firing", agent_id)
    try:
        ensure_collections(agent_id)
        ok = _evomem_sync(agent_id)
        logger.info("evomem_writer[%s]: sync completed (ok=%s)", agent_id, ok)
    except Exception as e:
        logger.warning("evomem_writer[%s]: debounced sync failed: %s", agent_id, e)


def mark_dirty(agent_id: str) -> None:
    """Schedule a debounced background sync for the agent, coalescing bursts."""
    with _sync_lock:
        existing = _sync_timers.get(agent_id)
        if existing is not None:
            existing.cancel()
        timer = threading.Timer(_SYNC_DEBOUNCE_SECONDS, _do_sync, args=(agent_id,))
        timer.daemon = True
        _sync_timers[agent_id] = timer
        timer.start()
    logger.info("evomem_writer[%s]: sync scheduled in %.1fs%s",
                agent_id, _SYNC_DEBOUNCE_SECONDS,
                " (coalesced)" if existing is not None else "")


def sync_now(agent_id: str) -> bool:
    """Synchronous sync (used by the backfill script). Cancels any pending timer."""
    with _sync_lock:
        existing = _sync_timers.pop(agent_id, None)
        if existing is not None:
            existing.cancel()
    vlog("writer[%s]: sync now (synchronous)", agent_id)
    try:
        ensure_collections(agent_id)
        return _evomem_sync(agent_id)
    except Exception:
        logger.warning("evomem_writer[%s]: sync_now failed", agent_id, exc_info=True)
        return False
