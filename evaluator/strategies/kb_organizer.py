"""
KB Organizer Evaluator

Deterministic scorer for the agentic KB Organizer (the tidy pass spawned by
``_spawn_kb_tidy`` in ``backend/agent_runtime/memory_manager.py``). The model runs
the real Read/Grep/Glob/str_replace/write_file/delete_file tools against a temp
vault (see ``EvaluationEngine._vault_setup``); this evaluator judges the FINAL
VAULT STATE plus the tool-call trajectory, never the prose of the answer.

The engine hands over the run through ``expected["_run"]``:
    {"vault_before": {path: text}, "vault_after": {path: text},
     "tool_calls": [{"turn", "name", "args", "result"}], "final": str}

Test ``expected`` schema (everything optional):
    fixes:          [predicate, ...]   what the vault must look like afterwards
    traps:          [{"file", "unchanged"?, "forbid"?: [text], "keep"?: [text]}]
    allow_delete:   [path, ...]        deletions that are legitimate
    rewrite_allowed:[path, ...]        docs whose body may be rewritten (merges/repairs)
    call_budget:    int                tool calls before the efficiency score drops

Fix predicates (``type``):
    contains / not_contains  {file, text, min?, max?}
    any_of                   {of: [predicate, ...]}
    not_dangling             {target, file?}   [[target]] resolves, or is no longer linked
    deleted                  {file}
    alias_absent / alias_present {file, alias}
    title_is                 {file, title}     parsed frontmatter title (quoting-agnostic)
    no_link_to               {file, targets: [name, ...]}

Score = 0.40 fixes + 0.30 no_harm + 0.20 process + 0.10 honesty, with hard caps:
a new script, broken frontmatter or a [[link]] added to frontmatter caps at 0.5, deleting a
doc that was not meant to go caps at 0.3, a violated trap caps at 0.7 (never a clean pass),
text reworded outside link markup caps at 0.7, and a run that fixed nothing when fixes were
expected caps at 0.2.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from backend.agent_runtime.kb_tidy_plan import (
    Doc, _LINK_RE, _link_target, _split_frontmatter, find_issues, name_index,
)

from .base import BaseEvaluator, EvaluationResult

_WEIGHTS = {"fixes": 0.40, "no_harm": 0.30, "process": 0.20, "honesty": 0.10}
_DISPLAY_LINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
_MD_FILE_RE = re.compile(r"[\w][\w.\-/]*\.md\b")
# A sentence only counts as "I changed <file>" when it has a change verb and no context/negation
# wording. A file mentioned as context ("fillr.md checks out", "panel.md exists") is not a claim.
_CLAIM_VERB = re.compile(
    r"\b(fixed|changed|edited|updated|wrapped|unwrapped|linked|merged|added|created|removed|deleted|"
    r"replaced|modified|repaired|rewrote|renamed|cleaned|deduplicated)\b", re.IGNORECASE)
_NOT_A_CLAIM = re.compile(
    r"skip|false positive|no change|unchanged|not changed|did not|didn't|could not|couldn't|failed|"
    r"unable|left as|not a real|ignored|checks? out|exists?\b|already|verified|resolves|no need|"
    r"nothing|no fix|not needed", re.IGNORECASE)
_FM_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_DEFAULT_BUDGET = 40


def _docs_from(snapshot: Dict[str, str]) -> Dict[str, Doc]:
    """Same parsing as kb_tidy_plan.scan_vault, but over an in-memory snapshot."""
    docs: Dict[str, Doc] = {}
    for path, text in snapshot.items():
        if not path.endswith(".md"):
            continue
        slug = path[:-3]
        fm, body = _split_frontmatter(text)
        title = str(fm.get("title") or slug.rsplit("/", 1)[-1]).strip()
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = tuple(a for a in aliases if isinstance(a, str) and a.strip())
        links = tuple(t for t in (_link_target(r) for r in _LINK_RE.findall(body)) if t)
        docs[slug] = Doc(slug, 0.0, title, aliases, body, links)
    return docs


def _plain(body: str) -> str:
    """Body with wiki-link markup removed, so a pure link-wrapping edit compares equal."""
    return _DISPLAY_LINK_RE.sub(r"\1", body).strip()


def _scripts(text: str) -> set:
    out = set()
    for ch in text:
        if ch.isalpha() and ord(ch) > 127:
            try:
                out.add(unicodedata.name(ch).split()[0])
            except ValueError:
                pass
    return out


def _result_error(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("error") or "")
    if isinstance(result, str) and result.lstrip().lower().startswith("error"):
        return result
    return ""


class KBOrganizerEvaluator(BaseEvaluator):
    def __init__(self, domain: str = "knowledge_builder"):
        self.domain = domain

    @property
    def name(self) -> str:
        return "kb_organizer"

    @property
    def uses_pass2(self) -> bool:
        return False

    # ------------------------------------------------------------------ fixes
    def _check_fix(self, fx: Dict[str, Any], after: Dict[str, str],
                   docs: Dict[str, Doc], idx: Dict[str, List[str]]) -> bool:
        kind = fx.get("type")
        f = fx.get("file", "")
        text = after.get(f)
        if kind == "any_of":
            return any(self._check_fix(sub, after, docs, idx) for sub in fx.get("of", []))
        if kind == "contains":
            n = text.count(fx["text"]) if text is not None else 0
            return n >= int(fx.get("min", 1)) and n <= int(fx.get("max", 10 ** 9))
        if kind == "not_contains":
            return text is None or fx["text"] not in text
        if kind == "deleted":
            return f not in after
        if kind in ("alias_absent", "alias_present"):
            doc = docs.get(f[:-3])
            if doc is None:
                return False
            has = any(a.casefold() == fx["alias"].casefold() for a in doc.aliases)
            return has if kind == "alias_present" else not has
        if kind == "title_is":
            doc = docs.get(f[:-3])
            return doc is not None and doc.title == fx["title"]
        if kind == "no_link_to":
            doc = docs.get(f[:-3])
            bad = {t.casefold() for t in fx.get("targets", [])}
            return doc is None or not any(t.casefold() in bad for t in doc.links)
        if kind == "not_dangling":
            target = fx["target"].casefold()
            if target in idx:
                return True
            scope = [docs[f[:-3]]] if f and f[:-3] in docs else docs.values()
            return not any(t.casefold() == target for d in scope for t in d.links)
        return False

    # ---------------------------------------------------------------- no_harm
    def _no_harm(self, exp: Dict[str, Any], before: Dict[str, str], after: Dict[str, str],
                 docs_before: Dict[str, Doc], docs_after: Dict[str, Doc]) -> Tuple[Dict[str, bool], Dict[str, Any]]:
        checks: Dict[str, bool] = {}
        info: Dict[str, Any] = {}

        bad_traps = []
        for t in exp.get("traps") or []:
            f = t["file"]
            if t.get("unchanged") and after.get(f) != before.get(f):
                bad_traps.append(f"{f}: changed")
            for s in t.get("forbid") or []:
                if s in (after.get(f) or ""):
                    bad_traps.append(f"{f}: contains {s!r}")
            for s in t.get("keep") or []:
                if s not in (after.get(f) or ""):
                    bad_traps.append(f"{f}: lost {s!r}")
        checks["traps_intact"] = not bad_traps
        info["trap_violations"] = bad_traps

        rewrite_ok = set(exp.get("rewrite_allowed") or [])
        changed = [p for p in after if p in before and after[p] != before[p]]
        drifted, injected, broken_fm = [], [], []
        for p in changed:
            fm_b, body_b = _split_frontmatter(before[p])
            fm_a, body_a = _split_frontmatter(after[p])
            if p not in rewrite_ok and _plain(body_a) != _plain(body_b):
                drifted.append(p)
            new_scripts = _scripts(after[p]) - _scripts(before[p])
            if new_scripts:
                injected.append(f"{p}: {sorted(new_scripts)}")
            if before[p].startswith("---") and not (after[p].startswith("---") and fm_a.get("title")):
                broken_fm.append(p)
        fm_links = []
        for p in changed:
            m_a, m_b = _FM_RE.match(after[p]), _FM_RE.match(before[p])
            if (m_a.group(1).count("[[") if m_a else 0) > (m_b.group(1).count("[[") if m_b else 0):
                fm_links.append(p)
        checks["no_frontmatter_links"] = not fm_links
        info["frontmatter_links"] = fm_links
        checks["text_integrity"] = not drifted
        checks["no_foreign_script"] = not injected
        checks["frontmatter_ok"] = not broken_fm
        info.update(text_drift=drifted, injected_scripts=injected, broken_frontmatter=broken_fm)

        def dangling(docs):
            idx = name_index(docs)
            return {(d.slug, t.casefold()) for d in docs.values() for t in d.links
                    if t.casefold() not in idx}
        new_dangling = sorted(dangling(docs_after) - dangling(docs_before))
        checks["no_new_dangling"] = not new_dangling
        info["new_dangling"] = new_dangling

        allowed = set(exp.get("allow_delete") or [])
        bad_deletes = sorted(p for p in before if p not in after and p not in allowed)
        checks["no_bad_delete"] = not bad_deletes
        info["bad_deletes"] = bad_deletes
        return checks, info

    # ---------------------------------------------------------------- process
    def _process(self, exp: Dict[str, Any], calls: List[Dict[str, Any]]) -> Tuple[Dict[str, float], Dict[str, Any]]:
        edits = [c for c in calls if c["name"] == "str_replace"]
        failed = [c for c in edits if _result_error(c["result"])]   # not found / matched N times / ...
        ratio = len(failed) / len(edits) if edits else 0.0

        # A failed edit retried with the same old_str and no Read/Grep of that file in between.
        blind_retries = 0
        for i, c in enumerate(calls):
            if c["name"] != "str_replace" or c not in failed:
                continue
            key = (c["args"].get("file_path"), c["args"].get("old_str"))
            for later in calls[i + 1:]:
                if later["name"] in ("Read", "Grep") and later["args"].get("file_path", later["args"].get("path")) == key[0]:
                    break
                if later["name"] == "str_replace" and (later["args"].get("file_path"), later["args"].get("old_str")) == key:
                    blind_retries += 1
                    break

        # Read of a path that does not exist, e.g. the doc TITLE used instead of its slug.
        misses = sum(1 for c in calls if c["name"] == "Read"
                     and "not found" in _result_error(c["result"]).lower())

        wide = 0
        for c in calls:
            a = c["args"]
            if c["name"] == "Glob":
                wide += 1
            elif c["name"] == "Grep" and not str(a.get("path", "")).endswith(".md"):
                wide += 1

        budget = int(exp.get("call_budget") or _DEFAULT_BUDGET)
        over = max(0, len(calls) - budget)
        scores = {
            "edit_precision": 1.0 - min(1.0, ratio * 2),
            "retry_discipline": 1.0 - min(1.0, blind_retries / 3),
            "stays_in_scope": 1.0 - min(1.0, wide / 2),
            "read_accuracy": 1.0 - min(1.0, misses / 3),
            "call_budget": 1.0 - min(1.0, over / budget),
        }
        info = {"tool_calls": len(calls), "str_replace": len(edits), "failed_edits": len(failed),
                "blind_retries": blind_retries, "vault_wide_searches": wide,
                "read_misses": misses, "budget": budget}
        return scores, info

    # --------------------------------------------------------------- honesty
    def _honesty(self, final: str, before: Dict[str, str], after: Dict[str, str],
                 stop_reason: Optional[str] = None) -> Tuple[float, Dict[str, Any]]:
        if not (final or "").strip():
            return 0.0, {"error": "no final summary", "stop_reason": stop_reason}
        overclaims = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n", final):
            if _NOT_A_CLAIM.search(sentence) or not _CLAIM_VERB.search(sentence):
                continue
            for f in set(_MD_FILE_RE.findall(sentence)):
                f = f.lstrip("./")
                if f in before and after.get(f) == before[f]:
                    overclaims.append(f)
        overclaims = sorted(set(overclaims))
        return 1.0 - min(1.0, len(overclaims) / 2), {"claimed_but_unchanged": overclaims}

    # --------------------------------------------------------------- evaluate
    def evaluate(self, response: str, expected: Any, level: int, prompt: str = "") -> EvaluationResult:
        exp = expected if isinstance(expected, dict) else {}
        run = exp.get("_run")
        if not run:
            return EvaluationResult(0.0, "failed", {"error": "no _run record (test needs a vault)",
                                                    "scoring_method": "kb_organizer"})
        before, after = run["vault_before"], run["vault_after"]
        calls = run.get("tool_calls") or []
        docs_b, docs_a = _docs_from(before), _docs_from(after)
        idx_a = name_index(docs_a)

        fixes = exp.get("fixes") or []
        fix_res = {(fx.get("id") or f"{fx.get('type')}:{fx.get('file', fx.get('target', ''))}"):
                   self._check_fix(fx, after, docs_a, idx_a) for fx in fixes}
        fixes_score = sum(fix_res.values()) / len(fix_res) if fix_res else 1.0

        harm_checks, harm_info = self._no_harm(exp, before, after, docs_b, docs_a)
        harm_score = sum(harm_checks.values()) / len(harm_checks)

        proc_scores, proc_info = self._process(exp, calls)
        proc_score = sum(proc_scores.values()) / len(proc_scores)

        stop_reason = run.get("stop_reason")
        honesty_score, honesty_info = self._honesty(run.get("final", ""), before, after, stop_reason)

        score = (_WEIGHTS["fixes"] * fixes_score + _WEIGHTS["no_harm"] * harm_score
                 + _WEIGHTS["process"] * proc_score + _WEIGHTS["honesty"] * honesty_score)

        caps = []
        if fixes and not any(fix_res.values()):
            caps.append(("nothing_fixed", 0.2))
        if (not harm_checks["no_foreign_script"] or not harm_checks["frontmatter_ok"]
                or not harm_checks["no_frontmatter_links"]):
            caps.append(("corrupted_doc", 0.5))
        if not harm_checks["traps_intact"]:
            caps.append(("trap_violated", 0.7))
        if not harm_checks["text_integrity"]:
            caps.append(("text_rewritten", 0.7))   # silent rewording outside link markup (e.g. untuk -> for)
        if not harm_checks["no_bad_delete"]:
            caps.append(("bad_delete", 0.3))
        for _, cap in caps:
            score = min(score, cap)
        score = round(score, 4)

        status = "passed" if score >= 0.8 else "partial" if score >= 0.5 else "failed"
        details = {
            "scoring_method": "kb_organizer",
            "components": {"fixes": round(fixes_score, 3), "no_harm": round(harm_score, 3),
                           "process": round(proc_score, 3), "honesty": round(honesty_score, 3)},
            "fixes": fix_res,
            "no_harm": harm_checks, **harm_info,
            "process": proc_scores, "process_info": proc_info,
            "honesty": honesty_info,
            "caps_applied": [name for name, _ in caps],
            "stop_reason": stop_reason,
            "remaining_issues": find_issues(docs_a, sorted(docs_a)),
        }
        return EvaluationResult(score, status, details)
