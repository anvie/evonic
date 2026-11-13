"""Unit tests for the Knowledge Organizer KB pipeline (memory_manager).

Covers the pure helpers (doc listing, slug resolution, JSON parsing) and the
``_author_docs`` integration with the organizer sub-agent mocked: the
create-collision guard (never mint a duplicate), update-by-slug for name variants,
skip-on-failure, and the server-local /_self/kb config used so Grep/Read/Glob hit
the server KB even on a remote workplace.
"""

import glob
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

import backend.agent_runtime.notifier as notifier_mod
import backend.subagent_manager as subagent_mod
import backend.agent_report_to as report_mod
import backend.event_stream as event_mod
import backend.tools._workspace as workspace_mod
from backend.agent_runtime import evomem_writer as W
from backend.agent_runtime import memory_manager as M

AGENT = "kb_org_test"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
    monkeypatch.setenv("EVOMEM_KB_ORGANIZER", "on")
    monkeypatch.setenv("EVOMEM_KB_ORGANIZER_DEBOUNCE_SECONDS", "0")  # don't debounce in tests
    # Bypass _ensure_brain (needs evomem binary) — upsert_doc writes files directly
    monkeypatch.setattr(W, "_ensure_brain", lambda aid: True)
    # Fake evomem availability so resolve_memory_engine returns "evomem"
    # (resolve_memory_engine calls is_available() via get_engine() and evomem_available)
    monkeypatch.setattr(M, "get_engine", lambda: "evomem")
    monkeypatch.setattr(M, "evomem_available", lambda: True)
    M._organizer_running.clear()
    M._organizer_last_start.clear()
    return tmp_path


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_kb_doc_listing_surfaces_alias(brain):
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="Daerah di Jakarta.",
                 body="Gambir adalah daerah di [[Jakarta]].", aliases=["Stasiun Gambir"])
    text, name_to_slug = M._kb_doc_listing(AGENT)
    assert "[gambir] Gambir" in text
    assert "aka Stasiun Gambir" in text
    assert name_to_slug["gambir"] == "gambir"
    assert name_to_slug["stasiun gambir"] == "gambir"  # alias maps to the canonical doc


def test_kb_doc_listing_empty(brain):
    text, name_to_slug = M._kb_doc_listing(AGENT)
    assert text == "(none yet)" and name_to_slug == {}


def test_resolve_doc_slug2_variants(brain):
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="b")
    n2s = {"gambir": "gambir", "stasiun gambir": "gambir"}
    # alias/title variant resolves to the canonical doc
    assert M._resolve_doc_slug2(AGENT, "", "Stasiun Gambir", "", n2s) == "gambir"
    # exact title
    assert M._resolve_doc_slug2(AGENT, "", "Gambir", "", n2s) == "gambir"
    # slug hint wins
    assert M._resolve_doc_slug2(AGENT, "gambir", "Whatever", "", {}) == "gambir"
    # unrelated subject → create (None)
    assert M._resolve_doc_slug2(AGENT, "", "Bandung", "", n2s) is None


def test_format_recent_messages():
    msgs = [
        {"role": "user", "content": "Aku ke Jakarta naik Kereta Bima"},
        {"role": "assistant", "content": "Wah, asik!"},
        {"role": "user", "content": "Besok Minggu 28 Juni 2026 ada acara lagi"},
    ]
    out = M._format_recent_messages(msgs, n=6)
    assert "User: Aku ke Jakarta naik Kereta Bima" in out
    assert "Assistant: Wah, asik!" in out
    assert "Besok Minggu 28 Juni 2026" in out
    # honors the tail window and tolerates 'type' (jsonl) shape
    assert M._format_recent_messages([{"type": "user", "content": "hi"}]) == "User: hi"
    assert M._format_recent_messages([]) == ""
    assert M._format_recent_messages(None) == ""


def test_tail_messages_keeps_user_turns_drops_tool_noise():
    """Tail context must contain real user+assistant turns only — tool/thinking
    entries must not masquerade as 'assistant' and crowd out the user message."""
    from backend.agent_runtime.summarizer import _tail_messages_from_entries
    entries = [
        {"type": "user", "content": "nama yg benar Yondix bukan Yonix"},
        {"type": "thinking", "content": "let me think"},
        {"type": "tool_call", "content": "search(...)"},
        {"type": "tool_output", "content": "results"},
        {"type": "intermediate", "content": "Oke, mencari dulu"},
        {"type": "final", "content": "Sudah kucatat."},
        {"type": "user", "content": "   "},                                    # empty -> dropped
        {"type": "user", "content": "x", "metadata": {"bash_exec": True}},     # bash -> dropped
    ]
    tail = _tail_messages_from_entries(entries)
    assert tail == [
        {"role": "user", "content": "nama yg benar Yondix bukan Yonix", "ts": 0},
        {"role": "assistant", "content": "Oke, mencari dulu", "ts": 0},
        {"role": "assistant", "content": "Sudah kucatat.", "ts": 0},
    ]
    out = M._format_recent_messages(tail, n=6)
    assert "User: nama yg benar Yondix bukan Yonix" in out   # the user turn survives
    assert "Assistant: Sudah kucatat." in out


def test_parse_organizer_docs():
    p = M._parse_organizer_docs
    op = '{"action": "create", "title": "X", "type": "note", "description": "d", "body": "b"}'
    # canonical
    assert p('{"docs": [%s]}' % op)[0]["title"] == "X"
    assert p('{"docs": []}') == []
    # fenced + prose before AND after (the real failure the user saw)
    assert p("```json\n{\"docs\": [%s]}\n```" % op)[0]["title"] == "X"
    assert p('Sure! Here is the result:\n{"docs": [%s]}\nI have filed it.' % op)[0]["title"] == "X"
    # prose contains an UNRELATED brace object before the real ops — must skip it
    assert p('ctx {"unrelated": true}. result: {"docs": [%s]}' % op)[0]["title"] == "X"
    # trailing comma (common LLM slip)
    assert p('{"docs": [%s,]}' % op)[0]["title"] == "X"
    # missing wrapper: bare array of ops, or a single bare op
    assert p('[%s]' % op)[0]["title"] == "X"
    assert p(op)[0]["title"] == "X"
    # thinking tags around it
    assert p('<think>let me decide</think>\n{"docs": [%s]}' % op)[0]["title"] == "X"
    # LITERAL newlines inside a string value (what LLMs emit for multi-line bodies);
    # strict json.loads rejects these — must still parse (strict=False).
    multiline = ('Now the JSON:\n```\n{"docs":[{"action":"create","title":"X","type":"note",'
                 '"description":"d","body":"line one\nline two\nline three with [[Link]]"}]}\n```')
    got = p(multiline)
    assert got is not None and got[0]["title"] == "X"
    assert "line one\nline two" in got[0]["body"]
    # genuinely unusable
    assert p("not json at all") is None
    assert p('{"items": []}') is None
    assert p('') is None
    assert p(None) is None


def test_parse_organizer_docs_reconstructs_malformed():
    """Last-resort reconstruction recovers ops from JSON that standard parsing can't."""
    p = M._parse_organizer_docs
    # unescaped inner double-quotes inside a string value
    bad = ('{"docs":[{"action":"create","title":"X","type":"note","description":"d",'
           '"body":"he said "halo" to me"}]}')
    got = p(bad)
    assert got is not None and got[0]["title"] == "X"
    assert "halo" in got[0]["body"]
    # truncated / unterminated output (cut off mid-string) — close string + brackets
    trunc = ('Here you go:\n```\n{"docs":[{"action":"create","title":"Y","type":"note",'
             '"description":"d","body":"this got cut o')
    got2 = p(trunc)
    assert got2 is not None and got2[0]["title"] == "Y"
    # _reconstruct_json directly: ignores trailing prose after the top-level object
    fixed = M._reconstruct_json('{"a": 1} and then some prose')
    assert json.loads(fixed) == {"a": 1}


# ── _author_docs integration (organizer mocked) ──────────────────────────────

def _run_author(ops):
    """Drive _author_docs with the organizer returning ``ops`` (list or None)."""
    with patch.object(M, "_get_active_collection", return_value=""), \
         patch.object(M, "_kb_organizer_infra_ok", return_value=True), \
         patch.object(M, "_spawn_kb_organizer", return_value=ops), \
         patch.object(W, "mark_dirty"), \
         patch.object(M, "_emit_doc_updated"):
        M._author_docs({"id": AGENT}, "s", "some source", threading.Lock())


def _kb_glob(pattern):
    return glob.glob(os.path.join(f"agents/{AGENT}/kb", pattern))


def test_collision_guard_blocks_duplicate_on_mislabeled_create(brain):
    """Organizer wrongly says CREATE for an entity that already exists (exact title)
    → the collision guard converts it to an update; no duplicate file is minted."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d",
                 body="Gambir adalah daerah di Jakarta.")
    _run_author([{"action": "create", "title": "Gambir", "type": "place",
                  "description": "d", "body": "User menambah foto ![f](http://x/g.jpg)."}])
    assert len(_kb_glob("gambir*.md")) == 1                    # no duplicate
    assert "![f](http://x/g.jpg)" in W.read_doc(AGENT, "gambir")["body"]


def test_update_by_slug_for_name_variant(brain):
    """Organizer returns update with the existing slug for a name variant
    ("Stasiun Gambir" → gambir) → appends to the existing doc, no new file."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d",
                 body="Gambir adalah daerah.")
    _run_author([{"action": "update", "slug": "gambir", "title": "Stasiun Gambir",
                  "type": "place", "description": "d", "body": "Foto baru ![f](http://x/g.jpg)."}])
    assert not os.path.exists(f"agents/{AGENT}/kb/stasiun-gambir.md")
    assert len(_kb_glob("gambir*.md")) == 1
    assert "![f](http://x/g.jpg)" in W.read_doc(AGENT, "gambir")["body"]


def test_skip_when_organizer_returns_none(brain):
    """Organizer failure → skip entirely (never a dupe-prone fallback); no crash."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="orig body")
    before = W.read_doc(AGENT, "gambir")["body"]
    _run_author(None)
    assert len(_kb_glob("*.md")) == 1
    assert W.read_doc(AGENT, "gambir")["body"] == before      # untouched


# ── rename (fix typo'd doc name) ─────────────────────────────────────────────

def test_rename_doc(brain):
    """evomem_writer.rename_doc fixes a misspelled doc name, keeping the old name
    as an alias and preserving the body."""
    W.upsert_doc(AGENT, title="Yonix", doc_type="person", description="teman",
                 body="Yonix teman SD [[Demo]].")
    new = W.rename_doc(AGENT, "yonix", "Yondix")
    assert new == "yondix"
    assert not os.path.exists(f"agents/{AGENT}/kb/yonix.md")     # old file gone
    d = W.read_doc(AGENT, "yondix")
    assert d["frontmatter"]["title"] == "Yondix"
    assert "Yonix" in (d["frontmatter"].get("aliases") or [])   # old name → alias
    assert "teman SD [[Demo]]" in d["body"]                     # body preserved


def test_author_docs_rename_action(brain):
    """The organizer's 'rename' op renames the doc and folds in new prose."""
    W.upsert_doc(AGENT, title="Yonix", doc_type="person", description="d", body="orig.")
    _run_author([{"action": "rename", "slug": "yonix", "new_title": "Yondix",
                  "body": "Teman SD di [[Magelang]]."}])
    assert not os.path.exists(f"agents/{AGENT}/kb/yonix.md")
    d = W.read_doc(AGENT, "yondix")
    assert d is not None and d["frontmatter"]["title"] == "Yondix"
    assert "Teman SD di [[Magelang]]" in d["body"]               # new info folded in


# ── pure-name slug + aliases ─────────────────────────────────────────────────

def test_split_title_aliases():
    s = M._split_title_aliases
    assert s("Abdurrahman Wahid (Gus Dur)") == ("Abdurrahman Wahid", ["Gus Dur"])
    assert s("Susilo Bambang Yudhoyono (SBY)") == ("Susilo Bambang Yudhoyono", ["SBY"])
    assert s("Joko Widodo [Jokowi]") == ("Joko Widodo", ["Jokowi"])
    assert s("Plain Name") == ("Plain Name", [])
    assert s("(only paren)") == ("(only paren)", [])


def test_create_strips_nickname_into_alias_keeps_slug_pure(brain):
    _run_author([{"action": "create", "title": "Abdurrahman Wahid (Gus Dur)", "type": "person",
                  "description": "Presiden RI ke-4", "body": "[[Abdurrahman Wahid]] presiden ke-4."}])
    assert os.path.exists(f"agents/{AGENT}/kb/abdurrahman-wahid.md")
    assert not os.path.exists(f"agents/{AGENT}/kb/abdurrahman-wahid-gus-dur.md")
    d = W.read_doc(AGENT, "abdurrahman-wahid")
    assert d["frontmatter"]["title"] == "Abdurrahman Wahid"
    assert "Gus Dur" in (d["frontmatter"].get("aliases") or [])


def test_create_passes_explicit_aliases(brain):
    _run_author([{"action": "create", "title": "Susilo Bambang Yudhoyono", "type": "person",
                  "aliases": ["SBY"], "description": "Presiden RI ke-6", "body": "[[SBY]] ke-6."}])
    assert "SBY" in (W.read_doc(AGENT, "susilo-bambang-yudhoyono")["frontmatter"].get("aliases") or [])


# ── surgical edit (fuzzy str-replace) ────────────────────────────────────────

def test_replace_in_doc_exact(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="User suka [[Gus Dur]].")
    assert W.replace_in_doc(AGENT, "x", "[[Gus Dur]]", "[[Abdurrahman Wahid]]") is True
    assert "[[Abdurrahman Wahid]]" in W.read_doc(AGENT, "x")["body"]


def test_replace_in_doc_refuses_ambiguous_exact(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="foo bar baz / foo bar baz")
    assert W.replace_in_doc(AGENT, "x", "foo bar baz", "Q") is False   # 2 matches → refuse


def test_replace_in_doc_fuzzy_typo(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d",
                 body="User mengunjungi [[Monumen Nasional]] kemarin sore.")
    # old_str has a typo vs the doc, but is unique & very close → fuzzy lands it
    assert W.replace_in_doc(AGENT, "x", "[[Monumen Nasionl]]", "[[Monas]]") is True
    assert "[[Monas]]" in W.read_doc(AGENT, "x")["body"]


def test_replace_in_doc_not_found(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="hello world")
    assert W.replace_in_doc(AGENT, "x", "an entirely unrelated phrase not present", "y") is False


def test_author_docs_edit_action(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="User suka [[Gus Dur]].")
    _run_author([{"action": "edit", "slug": "x", "old_str": "[[Gus Dur]]",
                  "new_str": "[[Abdurrahman Wahid]]"}])
    assert "[[Abdurrahman Wahid]]" in W.read_doc(AGENT, "x")["body"]


# ── deterministic retro-link (replaces no-op-prone edit retro-linking) ────────

def test_link_in_doc_wraps_plain_mention(brain):
    W.upsert_doc(AGENT, title="YMS", doc_type="note", description="d",
                 body="The team manages ERPNext operations daily.")
    assert W.link_in_doc(AGENT, "yms", "ERPNext", "ERPNext") is True
    assert "manages [[ERPNext]] operations" in W.read_doc(AGENT, "yms")["body"]


def test_link_in_doc_alias_form_when_target_differs(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="met Mas Wijaya there")
    assert W.link_in_doc(AGENT, "x", "Mas Wijaya", "Raden Wijaya Kusuma") is True
    assert "[[Raden Wijaya Kusuma|Mas Wijaya]]" in W.read_doc(AGENT, "x")["body"]


def test_link_in_doc_skips_already_linked(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="uses [[ERPNext]] daily")
    assert W.link_in_doc(AGENT, "x", "ERPNext", "ERPNext") is False   # no plain mention → no-op


def test_link_in_doc_no_partial_word_match(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="the ERP module only")
    assert W.link_in_doc(AGENT, "x", "ERPNext", "ERPNext") is False   # 'ERP' must not match


def test_link_in_doc_wraps_only_first_mention(brain):
    W.upsert_doc(AGENT, title="X", doc_type="note", description="d", body="ERPNext then ERPNext again")
    assert W.link_in_doc(AGENT, "x", "ERPNext", "ERPNext") is True
    assert W.read_doc(AGENT, "x")["body"] == "[[ERPNext]] then ERPNext again"


def test_author_docs_link_action(brain):
    W.upsert_doc(AGENT, title="YMS", doc_type="note", description="d",
                 body="manages ERPNext operations")
    _run_author([{"action": "link", "slug": "yms", "text": "ERPNext", "target": "ERPNext"}])
    assert "[[ERPNext]]" in W.read_doc(AGENT, "yms")["body"]


# ── dedupe self-duplicated docs ──────────────────────────────────────────────

def test_dedupe_body():
    d = W._dedupe_body
    para = "Gus Dur adalah Presiden RI ke-4 yang dikenal sebagai tokoh pluralis besar."
    # a substantial block that repeats verbatim is dropped (keep first)
    assert d(f"{para}\n\n{para}") == para
    # an embedded second YAML frontmatter (whole file concatenated) is cut
    body = (f"{para}\n\n---\ntitle: \"X\"\ntype: person\naliases: [\"Y\"]\n---\n\n{para}")
    out = d(body)
    assert "title:" not in out and out.startswith("Gus Dur")
    # short repeated lines (< min_block) are NOT touched
    assert d("- a\n\n- a").count("- a") == 2


def test_dedupe_doc(brain):
    para = "Gus Dur adalah Presiden RI ke-4 yang dikenal sebagai tokoh pluralis besar."
    W.upsert_doc(AGENT, title="GD", doc_type="person", description="d", body=f"{para}\n\n{para}")
    assert W.dedupe_doc(AGENT, "gd") is True
    assert W.read_doc(AGENT, "gd")["body"].count(para) == 1
    assert W.dedupe_doc(AGENT, "gd") is False          # already clean -> no rewrite


def test_append_to_doc_auto_dedupes(brain):
    para = "Gus Dur adalah Presiden RI ke-4 yang dikenal sebagai tokoh pluralis besar."
    W.upsert_doc(AGENT, title="GD", doc_type="person", description="d", body=para)
    W.append_to_doc(AGENT, "gd", para)                 # appending a duplicate
    assert W.read_doc(AGENT, "gd")["body"].count(para) == 1   # not doubled


def test_author_docs_dedupe_action(brain):
    para = "Gus Dur adalah Presiden RI ke-4 yang dikenal sebagai tokoh pluralis besar."
    W.upsert_doc(AGENT, title="GD", doc_type="person", description="d", body=f"{para}\n\n{para}")
    _run_author([{"action": "dedupe", "slug": "gd"}])
    assert W.read_doc(AGENT, "gd")["body"].count(para) == 1


# ── single-instance + debounce ───────────────────────────────────────────────

def test_organizer_single_instance_and_debounce(monkeypatch):
    # Use fresh containers to avoid any state leaked by other tests via the
    # shared _organizer_running / _organizer_last_start / _organizer_guard.
    fresh_running: set = set()
    fresh_last_start: dict = {}
    monkeypatch.setattr(M, "_organizer_running", fresh_running)
    monkeypatch.setattr(M, "_organizer_last_start", fresh_last_start)
    monkeypatch.setattr(M, "_organizer_guard", threading.Lock())

    a = "agentX"
    # single instance: a second claim is refused while one is running
    assert M._organizer_try_claim(a, 0) is True
    assert M._organizer_try_claim(a, 0) is False
    M._organizer_release(a)
    # debounce: after release, an immediate re-claim within the window is refused
    # (every successful claim records last_start, including the debounce=0 one)
    assert M._organizer_try_claim(a, 3600) is False
    # outside the window the claim succeeds again
    monkeypatch.setitem(fresh_last_start, a, time.monotonic() - 7200)
    assert M._organizer_try_claim(a, 3600) is True
    M._organizer_release(a)
    # a different agent is unaffected
    assert M._organizer_try_claim("agentY", 3600) is True


def test_on_summary_updated_reads_flat_event(monkeypatch):
    """event_stream delivers the data dict flat — the handler must read fields off
    the event directly (not event['payload']), or it never reaches process_knowledge."""
    import backend.agent_runtime as AR
    from models.db import db
    seen = {}
    # get_agent is only reached if agent_id/summary were parsed from the event;
    # returning None makes the handler stop before spawning a thread.
    monkeypatch.setattr(db, "get_agent", lambda aid: seen.__setitem__("aid", aid))
    AR._on_summary_updated({"agent_id": "alice", "session_id": "s1", "summary": "sum"})
    assert seen.get("aid") == "alice"


def test_organizer_docs_from_falls_back_to_session_message():
    """If the final_answer event carried an intermediate turn (no JSON), recover the
    real ops from the session's last assistant message (ground truth)."""
    intermediate = "Now I'll build the complete JSON output:"   # no JSON -> unparseable
    real = ('{"docs":[{"action":"create","title":"X","type":"note","description":"d",'
            '"body":"b"}]}')
    with patch.object(M, "_read_last_assistant_message", return_value=real):
        docs = M._organizer_docs_from("alice", "alice_organizer_1", "sess", intermediate)
    assert docs is not None and docs[0]["title"] == "X"
    # when the event answer already parses, the DB is not consulted
    good = ('{"docs":[{"action":"create","title":"Y","type":"note","description":"d",'
            '"body":"b"}]}')
    with patch.object(M, "_read_last_assistant_message", return_value=None) as rd:
        assert M._organizer_docs_from("alice", "alice_organizer_1", "sess", good)[0]["title"] == "Y"
        rd.assert_not_called()


def test_author_docs_skips_when_organizer_busy(brain):
    """A second summary while an organizer is already running spawns nothing."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="b")
    M._organizer_running.add(AGENT)                  # simulate an in-flight organizer
    called = {"spawn": False}

    def _spy(*a, **k):
        called["spawn"] = True
        return []

    try:
        with patch.object(M, "_get_active_collection", return_value=""), \
             patch.object(M, "_kb_organizer_infra_ok", return_value=True), \
             patch.object(M, "_spawn_kb_organizer", _spy):
            M._author_docs({"id": AGENT}, "s", "src", threading.Lock())
    finally:
        M._organizer_running.discard(AGENT)
    assert called["spawn"] is False                  # no double-spawn
    assert len(_kb_glob("*.md")) == 1                # nothing written


# ── server-local /_self/kb config (remote-workplace correctness) ─────────────

class _FakeES:
    def __init__(self):
        self.cbs = []

    def on(self, name, cb):
        self.cbs.append(cb)

    def off(self, name, cb):
        if cb in self.cbs:
            self.cbs.remove(cb)


def test_spawn_organizer_forces_server_local_kb_config(tmp_path):
    """The organizer config pins server-local execution at /_self/kb so its file
    tools work even when the parent agent runs on a remote workplace."""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    captured = {}
    es = _FakeES()

    def _fake_spawn(parent, build_config, id_kind="explorer"):
        captured["cfg"] = build_config("eid")
        captured["id_kind"] = id_kind
        return "eid"

    def _fake_notify(**kwargs):
        captured.setdefault("messages", []).append(kwargs.get("message", ""))
        for cb in list(es.cbs):
            cb({"agent_id": "eid", "answer": '{"docs": []}'})
        return {"success": True, "session_id": "sess"}

    agent = {"id": "a1", "name": "A", "user_id": "u", "channel_id": "c",
             "workplace_id": "remote-wp", "sandbox_enabled": 1, "run_as_user": "someuser"}

    from backend.agent_runtime import explorer as explorer_mod
    from models.db import db
    with patch.object(workspace_mod, "resolve_self_path", return_value=str(kb_dir)), \
         patch.object(subagent_mod.subagent_manager, "spawn_explorer", _fake_spawn), \
         patch.object(subagent_mod.subagent_manager, "destroy", return_value=None), \
         patch.object(notifier_mod, "notify_agent", _fake_notify), \
         patch.object(report_mod, "resolve_report_to_for_subagent_spawn",
                      return_value=(None, None, None)), \
         patch.object(event_mod, "event_stream", es), \
         patch.object(db, "get_setting", return_value=None), \
         patch.object(db, "get_default_model", return_value=None), \
         patch.object(explorer_mod, "resolve_tool_ids",
                      return_value=(["grep", "glob", "read"], None)), \
         patch.object(M, "evomem_available", lambda: True), \
         patch.object(M, "get_engine", lambda: "evomem"):
        result = M._spawn_kb_organizer(agent, "the summary", "User: ke Jakarta naik Bima")

    assert result == []                                       # parsed {"docs": []}
    cfg = captured["cfg"]
    assert cfg["workspace"] == str(kb_dir)                     # server-local KB
    assert cfg["workplace_id"] is None                        # not the remote workplace
    assert cfg["sandbox_enabled"] == 0
    assert cfg["run_as_user"] is None
    assert cfg["system_prompt"] == M._KB_ORGANIZER_SYSTEM_PROMPT
    assert captured["id_kind"] == "organizer"   # spawned as ..._organizer_N (UI label)
    assert cfg.get("is_kb_organizer") is True
    # The task hands the agent the recent conversation + summary, and does NOT
    # pre-list docs (the agent explores the vault itself).
    msg = captured["messages"][0]
    assert "RECENT CONVERSATION" in msg and "User: ke Jakarta naik Bima" in msg
    assert "the summary" in msg
    assert "KNOWN DOCS" not in msg


def test_author_docs_passes_recent_messages_to_organizer(brain):
    """_author_docs formats tail_messages and hands them to the organizer."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="b")
    seen = {}

    def _capture(agent, source_text, recent_text="", **kwargs):
        seen["recent"] = recent_text
        return []  # no ops

    with patch.object(M, "_get_active_collection", return_value=""), \
         patch.object(M, "_kb_organizer_infra_ok", return_value=True), \
         patch.object(M, "_spawn_kb_organizer", _capture):
        M._author_docs({"id": AGENT}, "s", "summary text", threading.Lock(),
                       recent_messages=[{"role": "user", "content": "halo dari Jakarta"}])
    assert "User: halo dari Jakarta" in seen["recent"]
