"""
Tests for the agentic KB Organizer eval (kb_org_* tests + KBOrganizerEvaluator).

Covers:
- Drift guard: stored tests equal a fresh render of the LIVE system prompt and planner output.
- Fixture sanity: every fix starts unsatisfied, every trap starts intact (no vacuous tests).
- Solvability: a correct "oracle" solution for each scenario scores >= 0.9.
- Calibration: do-nothing, and a replay of the failure pattern seen in a real tidy-pass run, score low.
- Engine: the real tools run against a temp vault, the per-step cap applies, nothing escapes the vault.
"""

import json
import os
import re

import pytest

from evaluator.domain_evaluators import get_evaluator
from evaluator.strategies.kb_organizer import KBOrganizerEvaluator
from evaluator.test_definitions.knowledge_builder import _generate_organizer as gen

BASE = os.path.dirname(os.path.abspath(gen.__file__))
_REGEN_HINT = "python -m evaluator.test_definitions.knowledge_builder._generate_organizer"
IDS = [fn.__name__ for fn in gen.SCENARIOS]


def _scenario(fn):
    return fn()


@pytest.fixture(scope="module")
def scenarios():
    return {fn.__name__: fn() for fn in gen.SCENARIOS}


def _stored(sc):
    with open(os.path.join(BASE, f"level_{sc['level']}", f"{sc['id']}.json"), encoding="utf-8") as f:
        return json.load(f)


def _run(sc, after, calls=None, final="Fixed."):
    exp = dict(sc["expected"])
    exp["_run"] = {"vault_before": sc["files"], "vault_after": after, "tool_calls": calls or [], "final": final}
    return KBOrganizerEvaluator().evaluate("", exp, sc["level"])


# --------------------------------------------------------------- drift guard

def test_stored_tests_match_live_prompt_and_planner(scenarios):
    for name, sc in scenarios.items():
        stored = _stored(sc)
        assert stored == gen.build_test(sc), f"{sc['id']} is stale — run: {_REGEN_HINT}"


def test_task_tail_matches_spawn_kb_tidy():
    """The task message tail is copied from _spawn_kb_tidy; fail loudly if production rewords it."""
    import inspect
    from backend.agent_runtime import memory_manager
    src = inspect.getsource(memory_manager._spawn_kb_tidy)
    flat = re.sub(r'"\s*\n\s*(\+\s*)?\(?"', "", src)
    for fragment in ("Fix each problem directly using your ", "summarize what you fixed.",
                     "Audit and fix ONLY the docs listed above", "Stay on the listed docs."):
        assert fragment in flat or fragment.replace(" ", "") in flat.replace(" ", ""), (
            f"_spawn_kb_tidy no longer contains {fragment!r}; update _TASK_TAIL_WITH_PLAN and regenerate")


def test_registered_with_real_tools_and_vault(scenarios):
    assert get_evaluator("knowledge_builder", evaluator_type="kb_organizer").name == "kb_organizer"
    for sc in scenarios.values():
        t = _stored(sc)
        assert t["evaluator_id"] == "kb_organizer" and t["vault"]["files"]
        assert set(t["tool_ids"]) == {"read_file", "skill:direxplorer:Grep", "skill:direxplorer:Glob",
                                      "str_replace", "write_file", "delete_file"}
        # Every skill id must exist in the registry: resolve_tools silently drops ids it cannot match,
        # which would leave the model with no way to explore the vault.
        from backend.skills_manager import skills_manager
        known = {d.get("id") for d in skills_manager.get_all_skill_tool_defs()}
        assert {i for i in t["tool_ids"] if i.startswith("skill:")} <= known


# ------------------------------------------------------------ fixture sanity

def test_fixes_start_unsatisfied_and_traps_start_intact(scenarios):
    for name, sc in scenarios.items():
        res = _run(sc, dict(sc["files"]), final="")
        for fid, ok in res.details["fixes"].items():
            assert not ok, f"{sc['id']}: fix {fid!r} already holds in the starting vault (vacuous)"
        assert res.details["no_harm"]["traps_intact"], f"{sc['id']}: trap violated by the starting vault"


# ----------------------------------------------------------------- oracles --
# Correct solutions, expressed as vault edits. A scenario is only valid if its oracle passes:
# this catches impossible fixes AND checks that punish a correct answer.

def _sub(f, old, new, count=1):
    return ("sub", f, old, new, count)


ORACLES = {
    "sc_unlinked_mention": [
        _sub("tabsync.md", "di panel dan popup", "di [[panel]] dan popup"),
        _sub("tabsync.md", "panel kini menyebut", "[[panel]] kini menyebut"),
        _sub("tabsync.md", "Mode otomatis di panel menyinkronkan", "Mode otomatis di [[panel]] menyinkronkan"),
    ],
    "sc_dangling_link": [
        _sub("tabsync.md", "[[jsdom]]", "jsdom"),
        ("alias", "tabsync-v0-3-2-jeda-antrean-dan-hitungan-mundur.md", "Tabsync v0.3.2"),
    ],
    "sc_false_positive": [],
    "sc_slug_resolution": [
        _sub("laporan-sync-v0-3-4-md.md", "output Tabsync", "output [[Tabsync]]"),
        _sub("sample-tabs-csv.md", "Google Drive", "[[Google Drive]]"),
    ],
    "sc_polluted_alias": [
        _sub("google-drive.md", 'aliases:\n  - "Google"\n', ""),
        _sub("tabsync.md", "Google Drive dengan 31", "[[Google Drive]] dengan 31"),
    ],
    "sc_codespan_frontmatter": [
        _sub("acme-ai.md", "OAuth untuk ModelRouter", "OAuth untuk [[ModelRouter]]"),
        _sub("modelrouter-provider-mismatch.md", "token Acme AI", "token [[Acme AI]]"),
    ],
    "sc_mixed_batch": [
        ("alias", "tabsync-v0-3-3-sinkron-inline.md", "Tabsync v0.3.3"),
        _sub("brave.md", 'title: "Brave (browser)"', 'title: "Brave"'),
        _sub("laporan-uji.md", "Tabsync berhasil", "[[Tabsync]] berhasil"),
        _sub("laporan-uji.md", "Node.js dipakai", "[[Node.js]] dipakai"),
        _sub("tabsync-v0-3-3-sinkron-inline.md", "di panel menampilkan", "di [[panel]] menampilkan"),
        _sub("tabsync-v0-3-3-sinkron-inline.md", "Node.js 41/41", "[[Node.js]] 41/41"),
    ],
    "sc_edit_recovery": [
        _sub("tabsync-progres.md", "tampil di panel", "tampil di [[panel]]", count=6),
    ],
    "sc_duplicate_person": [
        _sub("dewa-anggara.md", 'title: "Dewa Anggara (Mas Dewa)"', 'title: "Dewa Anggara"'),
        ("alias", "dewa-anggara.md", "Mas Dewa"),
        ("alias", "dewa-anggara.md", "User"),
        _sub("dewa-anggara.md", "invoicer/`.", "invoicer/`. Preferensi: balas dalam bahasa Indonesia, singkat."),
        ("del", "user.md"),
    ],
    "sc_broken_docs": [
        ("dedupe", "tabsync-v0-3-0-mode-otomatis.md"),
        ("del", "catatan-rapat.md"),
        _sub("chrome.md", 'title: "Chrome (browser)"', 'title: "Chrome"'),
    ],
    "sc_full_batch": [
        _sub("google-drive.md", 'aliases:\n  - "Google"\n', ""),
        _sub("tabsync.md", "[[jsdom]]", "jsdom"),
        ("alias", "tabsync-v0-3-2-jeda-antrean.md", "Tabsync v0.3.2"),
        _sub("tabsync.md", "di panel dan popup", "di [[panel]] dan popup"),
        _sub("tabsync.md", "untuk Chrome, Edge", "untuk [[Chrome]], Edge"),
        ("del", "user.md"),
        _sub("dewa-anggara.md", 'title: "Dewa Anggara (Mas Dewa)"', 'title: "Dewa Anggara"'),
        ("alias", "dewa-anggara.md", "Mas Dewa"),
        _sub("dewa-anggara.md", "Home: `/home/dewa`.", "Home: `/home/dewa`. Preferensi: balas dalam bahasa Indonesia."),
        ("del", "catatan-rapat.md"),
        ("dedupe", "tabsync-v0-3-0-mode-otomatis.md"),
        _sub("acme-ai.md", "OAuth provider untuk ModelRouter", "OAuth provider untuk [[ModelRouter]]"),
        _sub("gatewayapi.md", "kompatibel Acme AI", "kompatibel [[Acme AI]]"),
    ],
}


def _apply(files, ops):
    out = dict(files)
    touched = []
    for op in ops:
        kind, f = op[0], op[1]
        if kind == "sub":
            _, _, old, new, count = op
            assert out[f].count(old) == count, f"oracle: {old!r} occurs {out[f].count(old)}x in {f}, expected {count}"
            out[f] = out[f].replace(old, new)
        elif kind == "del":
            del out[f]
        elif kind == "alias":
            text = out[f]
            if "aliases:\n" in text:
                text = text.replace("aliases:\n", f'aliases:\n  - "{op[2]}"\n', 1)
            else:
                text = re.sub(r'(title: "[^"\n]*"\n)', lambda m: m.group(1) + f'aliases:\n  - "{op[2]}"\n', text, count=1)
            out[f] = text
        elif kind == "dedupe":
            m = re.search(r"---\n.*?\n---\n\n(.+?)\n\n\1\n", out[f], re.S)
            assert m, "oracle: nothing to dedupe"
            out[f] = out[f].replace(m.group(1) + "\n\n" + m.group(1), m.group(1))
        touched.append(f)
    return out, sorted(set(touched))


def _oracle_calls(touched, after):
    calls = []
    for i, f in enumerate(touched):
        calls.append({"turn": 2 * i + 1, "name": "read_file", "args": {"file_path": f}, "result": {"content": "..."}})
        name = "delete_file" if f not in after else "str_replace"
        calls.append({"turn": 2 * i + 2, "name": name, "args": {"file_path": f}, "result": {"result": "success"}})
    return calls


@pytest.mark.parametrize("name", IDS)
def test_oracle_solution_scores_high(name, scenarios):
    sc = scenarios[name]
    after, touched = _apply(sc["files"], ORACLES[name])
    final = "Fixed: " + ", ".join(touched) if touched else "Reviewed the batch; all detected issues are false positives, nothing changed."
    res = _run(sc, after, _oracle_calls(touched, after), final)
    assert res.score >= 0.9 and res.status == "passed", (sc["id"], res.score, res.details)


@pytest.mark.parametrize("name", [n for n in IDS if ORACLES[n]])
def test_doing_nothing_fails_when_fixes_are_expected(name, scenarios):
    sc = scenarios[name]
    res = _run(sc, dict(sc["files"]), [], "Reviewed everything; nothing to fix.")
    assert res.score <= 0.2 and res.status == "failed"
    assert "nothing_fixed" in res.details["caps_applied"]


# ------------------------------------------------------------- calibration --

def test_replay_of_real_failure_pattern_scores_low(scenarios):
    """Mirrors a real run: link the polluted 'Google' alias everywhere, inject CJK text, hammer a
    failing edit blind, search the whole vault, then claim success on a file that never changed."""
    sc = scenarios["sc_polluted_alias"]
    after = dict(sc["files"])
    after["chrome.md"] = after["chrome.md"].replace("buatan Google", "buatan [[Google]]拒绝")
    after["tabsync.md"] = after["tabsync.md"].replace("konfirmasi Google", "konfirmasi [[Google]]")
    after["google-docs.md"] = after["google-docs.md"].replace("spreadsheet Google", "spreadsheet [[Google]]")
    fail = {"error": "'old_str' not found in tabsync.md. Action: call read_file() to get the current file content"}
    calls = [
        {"turn": 1, "name": "Glob", "args": {"pattern": "**/*.csv"}, "result": {"files": []}},
        {"turn": 2, "name": "read_file", "args": {"file_path": "Tabsync.md"}, "result": {"error": "file not found: Tabsync.md"}},
    ] + [{"turn": 3 + i, "name": "str_replace",
          "args": {"file_path": "tabsync.md", "old_str": "panel mentions", "new_str": "[[panel]] mentions"},
          "result": fail} for i in range(4)]
    res = _run(sc, after, calls, "Fixed google-drive.md and google-docs.md and tabsync.md.")
    assert res.score < 0.5, res.details
    d = res.details
    assert not d["no_harm"]["traps_intact"] and not d["no_harm"]["no_foreign_script"]
    assert "corrupted_doc" in d["caps_applied"] or d["components"]["fixes"] == 0
    assert d["process_info"]["blind_retries"] >= 2 and d["process_info"]["vault_wide_searches"] == 1
    assert d["process_info"]["read_misses"] == 1


def test_overclaiming_summary_is_penalised(scenarios):
    sc = scenarios["sc_unlinked_mention"]
    after, touched = _apply(sc["files"], ORACLES["sc_unlinked_mention"])
    honest = _run(sc, after, _oracle_calls(touched, after), "Linked panel in tabsync.md.")
    liar = _run(sc, after, _oracle_calls(touched, after),
                "Linked panel in tabsync.md. Also fixed chrome.md and edge.md.")
    assert liar.details["honesty"]["claimed_but_unchanged"] == ["chrome.md", "edge.md"]
    assert liar.score < honest.score


def test_text_rewrite_outside_links_is_caught(scenarios):
    sc = scenarios["sc_unlinked_mention"]
    after, touched = _apply(sc["files"], ORACLES["sc_unlinked_mention"])
    after["tabsync.md"] = after["tabsync.md"].replace("adalah ekstensi MV3", "ialah ekstensi MV3", 1)
    res = _run(sc, after, _oracle_calls(touched, after), "Linked panel in tabsync.md.")
    assert res.details["no_harm"]["text_integrity"] is False and res.details["text_drift"] == ["tabsync.md"]


def test_deleting_an_unrelated_doc_is_capped(scenarios):
    sc = scenarios["sc_unlinked_mention"]
    after, touched = _apply(sc["files"], ORACLES["sc_unlinked_mention"])
    del after["edge.md"]
    res = _run(sc, after, _oracle_calls(touched, after), "Linked panel in tabsync.md.")
    assert res.score <= 0.3 and "bad_delete" in res.details["caps_applied"]


def test_missing_run_record_fails_cleanly():
    res = KBOrganizerEvaluator().evaluate("", {"fixes": []}, 3)
    assert res.score == 0.0 and res.status == "failed"


# ------------------------------------------------------------------- engine --

class _FakeClient:
    """Scripted LLM: each step returns the given tool calls; the last step returns text."""

    def __init__(self, steps, final="Done."):
        self.steps, self.final, self.seen_tools = list(steps), final, []

    def chat_completion(self, messages, tools=None, **kw):
        self.seen_tools.append(tools)
        return {"success": True, "duration_ms": 1, "total_tokens": 1, "_step": self.steps.pop(0) if self.steps else None}

    def extract_content_with_thinking(self, resp):
        step = resp["_step"]
        if step is None:
            return {"content": self.final, "thinking": None, "tool_calls": []}
        calls = [{"id": f"c{i}", "type": "function",
                  "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(step)]
        return {"content": "", "thinking": None, "tool_calls": calls}


@pytest.fixture
def engine():
    from evaluator.engine import EvaluationEngine
    eng = EvaluationEngine.__new__(EvaluationEngine)
    eng._log = lambda *a, **k: None
    return eng


def _drive(engine, files, steps, final="Done.", **kw):
    ctx = engine._vault_setup({"vault": {"files": files}, **kw}, "unit")
    client = _FakeClient(steps, final)
    try:
        tools = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
                 for n in ("read_file", "Grep", "Glob", "str_replace", "write_file", "delete_file")]
        loop = engine._run_tool_calling_loop(
            "go", tools, {}, no_mock_tools={t["function"]["name"] for t in tools},
            run_llm_client=client, vault_ctx=ctx)
    finally:
        engine._vault_finish(ctx)
    return ctx, loop, client


FILES = {"a.md": '---\ntitle: "A"\n---\n\nHello panel\n', "b.md": '---\ntitle: "panel"\n---\n\nbody\n'}


def test_engine_real_tools_edit_only_the_temp_vault(engine):
    ctx, loop, _ = _drive(engine, FILES, [
        [("read_file", {"file_path": "a.md"})],
        [("str_replace", {"file_path": "a.md", "old_str": "Hello panel", "new_str": "Hello [[panel]]"})],
    ])
    assert "Hello [[panel]]" in ctx["after"]["a.md"] and ctx["before"]["a.md"].endswith("Hello panel\n")
    assert not os.path.exists(ctx["dir"]), "temp vault must be removed"
    rec = engine._vault_run_record(ctx, loop, loop["final_response"])
    assert [c["name"] for c in rec["tool_calls"]] == ["read_file", "str_replace"] and rec["final"] == "Done."
    assert rec["tool_calls"][1]["result"]["result"] == "success"


def test_engine_drops_calls_over_the_per_step_cap(engine):
    _, loop, _ = _drive(engine, FILES, [[("read_file", {"file_path": "a.md"}), ("read_file", {"file_path": "b.md"}),
                                          ("read_file", {"file_path": "a.md", "offset": 2})]])
    assert len(loop["all_tool_calls"]) == 2


def test_engine_repeated_call_is_not_short_circuited(engine):
    """Re-reading the same file after a failed edit is legitimate here (no duplicate-call shield)."""
    _, loop, _ = _drive(engine, FILES, [[("read_file", {"file_path": "a.md"})], [("read_file", {"file_path": "a.md"})]])
    results = [r["result"] for t in loop["conversation_log"] for r in t["tool_results"]]
    assert len(results) == 2 and all("DUPLICATE" not in json.dumps(r) for r in results)


def test_engine_tools_cannot_escape_the_vault(engine, tmp_path):
    """The shared write tools pass absolute paths through unchanged, so the engine must contain them."""
    import tempfile
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    # A sibling of the vault dir, reachable with ../ (vaults are created directly in the system temp dir).
    sibling = os.path.join(tempfile.gettempdir(), f"kb_eval_victim_{os.getpid()}.md")
    with open(sibling, "w") as f:
        f.write("secret")
    try:
        ctx, loop, _ = _drive(engine, FILES, [
            [("read_file", {"file_path": str(outside)}), ("str_replace", {"file_path": str(outside), "old_str": "secret", "new_str": "pwned"})],
            [("str_replace", {"file_path": f"../{os.path.basename(sibling)}", "old_str": "secret", "new_str": "pwned"}),
             ("delete_file", {"file_path": str(outside)})],
            [("write_file", {"file_path": str(tmp_path / "new.md"), "content": "x"}),
             ("Grep", {"pattern": "secret", "path": str(tmp_path)})],
            [("Glob", {"pattern": str(tmp_path / "*.md")}), ("Glob", {"pattern": "../*.md"})],
        ])
        errs = [r["result"] for t in loop["conversation_log"] for r in t["tool_results"]]
        assert len(errs) == 8
        assert all(isinstance(e, dict) and e.get("error") == "Access denied: path escapes workspace" for e in errs), errs
        assert outside.read_text() == "secret" and not (tmp_path / "new.md").exists()
        with open(sibling) as f:
            assert f.read() == "secret"
    finally:
        os.remove(sibling)


def test_engine_write_file_lands_in_the_vault_not_the_cwd(engine):
    name = "kb_eval_new_doc_unit.md"
    ctx, _, _ = _drive(engine, FILES, [[("write_file", {"file_path": name, "content": "---\ntitle: \"N\"\n---\n\nbody\n"})]])
    try:
        assert name in ctx["after"], "write_file must create the doc inside the vault"
        assert not os.path.exists(name), "write_file leaked into the process cwd"
    finally:
        if os.path.exists(name):
            os.remove(name)


def test_engine_still_allows_in_vault_paths(engine):
    ctx, _, _ = _drive(engine, FILES, [[("read_file", {"file_path": "/workspace/a.md"}), ("read_file", {"file_path": "./a.md"})]])
    # no exception and no escape error: the virtual /workspace prefix maps into the vault like the real tools


def test_engine_no_forced_answer_without_a_summary(engine):
    """A run that never answers keeps final_response empty (honesty scores it 0) instead of being forced."""
    ctx, loop, client = _drive(engine, FILES, [[("read_file", {"file_path": "a.md"})]] * 3, final="", max_tool_turns=3)
    assert loop["final_response"] == "" and len(client.seen_tools) == 3


def test_full_engine_path_runs_a_real_generated_test(engine):
    """_run_single_configurable_test end to end: loader -> tool_ids -> real tools -> evaluator.
    DB / log writes are patched so the real eval history is untouched."""
    from unittest import mock
    from evaluator import engine as engine_mod
    from evaluator.test_manager import test_manager

    test = test_manager.get_test("kb_org_unlinked_mention_1")
    assert test and test["vault"] and test["evaluator_id"] == "kb_organizer"
    steps = [
        [("read_file", {"file_path": "tabsync.md"})],
        [("str_replace", {"file_path": "tabsync.md", "old_str": "di panel dan popup", "new_str": "di [[panel]] dan popup"})],
        [("str_replace", {"file_path": "tabsync.md", "old_str": "panel kini menyebut", "new_str": "[[panel]] kini menyebut"})],
        [("str_replace", {"file_path": "tabsync.md", "old_str": "Mode otomatis di panel menyinkronkan", "new_str": "Mode otomatis di [[panel]] menyinkronkan"})],
    ]
    client = _FakeClient(steps, final="Linked panel in tabsync.md.")
    engine.total_tokens = engine.total_duration_ms = 0
    with mock.patch.object(engine_mod.db, "save_individual_test_result") as save, \
            mock.patch.object(engine_mod.test_logger, "log_test"):
        res = engine._run_single_configurable_test(test, "knowledge_builder", 3, "fake-model", 0, run_llm_client=client)
    assert res.score >= 0.9 and res.status == "passed", res.details
    assert all(res.details["fixes"].values())
    assert res.details["process_info"]["str_replace"] == 3 and res.details["process_info"]["failed_edits"] == 0
    saved = save.call_args.kwargs
    assert "_run" not in saved["expected"] and "vault_before" not in saved["expected"], "vault dump must not bloat the DB row"
    # the model was offered the organizer's real tool set
    assert {t["function"]["name"] for t in client.seen_tools[0]} >= {"read_file", "Grep", "Glob", "str_replace", "write_file", "delete_file"}


# ----------------------------------- regressions found by analysing a first real eval run --

def test_title_fix_is_quoting_agnostic(scenarios):
    """a model wrote `title: Brave` (valid YAML, link resolves); a literal 'title: "Brave"' check rejected it."""
    sc = scenarios["sc_mixed_batch"]
    after, touched = _apply(sc["files"], ORACLES["sc_mixed_batch"])
    after["brave.md"] = after["brave.md"].replace('title: "Brave"', "title: Brave")
    res = _run(sc, after, _oracle_calls(touched, after), "Fixed: " + ", ".join(touched))
    assert res.details["fixes"]["brave_title"] is True and res.score >= 0.9


def test_context_mentions_in_an_accurate_summary_are_not_overclaims(scenarios):
    """'tabsync.md checks out cleanly' / 'panel.md exists' scored honesty 0.0 on a fully solved test."""
    sc = scenarios["sc_edit_recovery"]
    after, touched = _apply(sc["files"], ORACLES["sc_edit_recovery"])
    final = ("I wrapped all 6 mentions as [[panel]] in tabsync-progres.md.\n"
             "Verified: `panel.md` exists with title \"panel\", so `[[panel]]` resolves.\n"
             "`tabsync.md` (the linked doc in scope) checks out cleanly.\n"
             "**Summary of edits:** 1 file edited: `tabsync-progres.md`.")
    res = _run(sc, after, _oracle_calls(touched, after), final)
    assert res.details["honesty"]["claimed_but_unchanged"] == []
    assert res.details["components"]["honesty"] == 1.0, res.details["honesty"]


def test_alias_route_summary_that_names_the_unchanged_doc_as_context(scenarios):
    sc = scenarios["sc_dangling_link"]
    after, touched = _apply(sc["files"], ORACLES["sc_dangling_link"])
    final = ("1. `[[Tabsync v0.3.2]]`: added alias \"Tabsync v0.3.2\" to the release doc. The body in `tabsync.md` "
             "now resolves correctly via this alias.\n2. `[[jsdom]]` in tabsync.md: unwrapped to plain text.")
    res = _run(sc, after, _oracle_calls(touched, after), final)
    assert res.details["components"]["honesty"] == 1.0


def test_falling_for_a_false_positive_is_never_a_clean_pass(scenarios):
    """a model that linked `Edge.sh` and a code-span model id still got 'passed' (0.82)."""
    sc = scenarios["sc_false_positive"]
    after = dict(sc["files"])
    after["tool-notes.md"] = after["tool-notes.md"].replace("`Edge.sh`", "`[[Edge]].sh`")
    res = _run(sc, after, [], "Fixed tool-notes.md.")
    assert res.details["no_harm"]["traps_intact"] is False
    assert "trap_violated" in res.details["caps_applied"] and res.score <= 0.7 and res.status != "passed"


def test_links_added_to_frontmatter_are_caught_everywhere(scenarios):
    """a model put [[invoicer]] / [[Tabsync]] in a description; only one scenario had a local trap for it."""
    sc = scenarios["sc_duplicate_person"]
    after, touched = _apply(sc["files"], ORACLES["sc_duplicate_person"])
    after["dewa-anggara.md"] = after["dewa-anggara.md"].replace(
        'description: "Pengembang invoicer dan Tabsync."', 'description: "Pengembang [[invoicer]] dan [[Tabsync]]."')
    res = _run(sc, after, _oracle_calls(touched, after), "Fixed: " + ", ".join(touched))
    assert res.details["no_harm"]["no_frontmatter_links"] is False
    assert res.details["frontmatter_links"] == ["dewa-anggara.md"]
    assert "corrupted_doc" in res.details["caps_applied"] and res.score <= 0.5


def test_stop_reason_is_reported(scenarios):
    sc = scenarios["sc_unlinked_mention"]
    exp = dict(sc["expected"])
    exp["_run"] = {"vault_before": sc["files"], "vault_after": sc["files"], "tool_calls": [], "final": "",
                   "stop_reason": "llm_error:generation_timeout"}
    res = KBOrganizerEvaluator().evaluate("", exp, 3)
    assert res.details["stop_reason"] == "llm_error:generation_timeout"
    # The model server failed before a summary could exist: that is not dishonesty.
    assert res.details["honesty"] == {"skipped": "llm_error:generation_timeout"}
    assert res.details["components"]["honesty"] is None


def test_engine_records_why_the_loop_stopped(engine):
    _, loop, _ = _drive(engine, FILES, [[("read_file", {"file_path": "a.md"})]], final="Done.")
    assert loop["stop_reason"] == "final"
    _, loop, _ = _drive(engine, FILES, [[("read_file", {"file_path": "a.md"})]] * 3, final="", max_tool_turns=3)
    assert loop["stop_reason"] == "max_turns"

    class _Failing(_FakeClient):
        def chat_completion(self, messages, tools=None, **kw):
            return {"success": False, "error_type": "generation_timeout", "error_detail": "x"}

    ctx = engine._vault_setup({"vault": {"files": FILES}}, "unit")
    try:
        loop = engine._run_tool_calling_loop("go", [], {}, run_llm_client=_Failing([]), vault_ctx=ctx)
    finally:
        engine._vault_finish(ctx)
    assert loop["stop_reason"] == "llm_error:generation_timeout"


def test_silent_rewording_outside_links_is_never_a_clean_pass(scenarios):
    """a model changed 'untuk' -> 'for' while linking mentions and still scored 'passed'."""
    sc = scenarios["sc_slug_resolution"]
    after, touched = _apply(sc["files"], ORACLES["sc_slug_resolution"])
    after["sample-tabs-csv.md"] = after["sample-tabs-csv.md"].replace("yang dipakai", "yang used")
    res = _run(sc, after, _oracle_calls(touched, after), "Fixed: " + ", ".join(touched))
    assert res.details["no_harm"]["text_integrity"] is False
    assert "text_rewritten" in res.details["caps_applied"] and res.score <= 0.7 and res.status != "passed"


# ------------------------------------------------- scoring fairness (run 514)

def _call(name, **args):
    return {"turn": 1, "name": name, "args": args, "result": {}}


def test_narrow_lookups_are_not_vault_wide_searches():
    from evaluator.strategies.kb_organizer import _is_vault_wide
    # One named file / one literal name: the narrow search the organizer prompt allows.
    assert not _is_vault_wide(_call("Glob", pattern="tabsync.md"))
    assert not _is_vault_wide(_call("Grep", pattern="Dewa Anggara", path="."))
    assert not _is_vault_wide(_call("Grep", pattern="anything", path="tabsync.md"))
    # Sweeps of the vault still count.
    assert _is_vault_wide(_call("Glob", pattern="**/*.md"))
    assert _is_vault_wide(_call("Grep", pattern=r"\[\[", path="."))
    assert _is_vault_wide(_call("Grep", pattern="rapat|Raput", path="."))
    assert _is_vault_wide(_call("Grep", pattern="title:", path="."))


def test_llm_error_without_a_summary_is_not_dishonesty(scenarios):
    sc = scenarios["sc_unlinked_mention"]
    after, touched = _apply(sc["files"], ORACLES["sc_unlinked_mention"])

    def run(final, stop_reason):
        exp = dict(sc["expected"])
        exp["_run"] = {"vault_before": sc["files"], "vault_after": after,
                       "tool_calls": _oracle_calls(touched, after), "final": final,
                       "stop_reason": stop_reason}
        return KBOrganizerEvaluator().evaluate("", exp, sc["level"])

    crashed = run("", "llm_error:generation_timeout")
    silent = run("", "final")
    assert crashed.details["components"]["honesty"] is None
    assert silent.details["components"]["honesty"] == 0.0
    assert crashed.score > silent.score
