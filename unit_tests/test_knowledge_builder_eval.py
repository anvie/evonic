"""
Tests for the Knowledge Builder eval domain.

Covers:
- KnowledgeBuilderEvaluator scoring (good vs. malformed / invalid-type / no-links /
  wrong-dedup / expect-empty / trailing-"Relations"-block).
- Drift guard: stored test prompts stay identical to the live _AUTHOR_DOCS_PROMPT.
- Domain/evaluator registration & discovery.
"""

import json
import os

import pytest

from evaluator.strategies.knowledge_builder import (
    KnowledgeBuilderEvaluator,
    _has_trailing_link_block,
)
from evaluator.domain_evaluators import get_evaluator
from evaluator.test_loader import test_loader
from evaluator.test_definitions.knowledge_builder import _generate


@pytest.fixture
def ev():
    return KnowledgeBuilderEvaluator()


# ---------------------------------------------------------------- scoring ----

def _good_create():
    return json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "person",
        "description": "Product manager at Evonic.", "tags": ["person"],
        "body": "Sari adalah product manager di [[Evonic]], bekerja bersama User.",
    }]})


GOOD_EXPECT = {"expect_actions": {"create": [{"title": "Sari", "type": "person"}]},
               "require_links": True}


def test_good_create_scores_high(ev):
    r = ev.evaluate(_good_create(), GOOD_EXPECT, level=2)
    assert r.score >= 0.8
    assert r.status == "passed"
    assert r.pass2_used is False


def test_invalid_json_fails(ev):
    r = ev.evaluate("not json at all", GOOD_EXPECT, level=2)
    assert r.score == 0.0
    assert r.status == "failed"


def test_code_fenced_json_is_parsed(ev):
    fenced = "```json\n" + _good_create() + "\n```"
    r = ev.evaluate(fenced, GOOD_EXPECT, level=2)
    assert r.score >= 0.8


def test_invalid_type_penalized(ev):
    bad = json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "human",  # not a valid DOC_TYPE
        "description": "PM.", "body": "Sari di [[Evonic]].",
    }]})
    good = ev.evaluate(_good_create(), GOOD_EXPECT, level=2).score
    assert ev.evaluate(bad, GOOD_EXPECT, level=2).score < good


def test_missing_links_when_required_penalized(ev):
    no_links = json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "person",
        "description": "PM.", "body": "Sari adalah product manager.",
    }]})
    good = ev.evaluate(_good_create(), GOOD_EXPECT, level=2).score
    assert ev.evaluate(no_links, GOOD_EXPECT, level=2).score < good


def test_thumbnail_expected_rewards_and_penalizes(ev):
    expect = {"expect_actions": {"create": [
        {"title": "Sari", "type": "person", "thumbnail": True}]},
        "require_links": False}
    with_thumb = json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "person",
        "description": "PM.", "thumbnail": "https://cdn.example.com/sari.webp",
        "body": "Sari adalah product manager di [[Evonic]].",
    }]})
    without_thumb = json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "person",
        "description": "PM.", "body": "Sari adalah product manager di [[Evonic]].",
    }]})
    r_with = ev.evaluate(with_thumb, expect, level=2)
    r_without = ev.evaluate(without_thumb, expect, level=2)
    assert r_with.score > r_without.score
    assert r_with.details["components"]["thumbnail"] == 1.0
    assert r_without.details["components"]["thumbnail"] == 0.0


def test_thumbnail_skipped_when_not_expected(ev):
    # No entity flags thumbnail -> component is a no-op (1.0), regardless of output.
    r = ev.evaluate(_good_create(), GOOD_EXPECT, level=2)
    assert r.details["components"]["thumbnail"] == 1.0


def test_edit_op_scored(ev):
    expect = {"expect_actions": {"edit": [{"slug": "anwar-saputra"}]}}
    good = json.dumps({"docs": [{"action": "edit", "slug": "anwar-saputra",
                                 "old_str": "bekerja di Acme",
                                 "new_str": "bekerja di [[Acme]]"}]})
    r = ev.evaluate(good, expect, level=5)
    assert r.status == "passed"
    assert r.details["missing_entities"] == []


def test_noop_edit_penalized(ev):
    expect = {"expect_actions": {"edit": [{"slug": "anwar-saputra"}]}}
    good = json.dumps({"docs": [{"action": "edit", "slug": "anwar-saputra",
                                 "old_str": "bekerja di Acme",
                                 "new_str": "bekerja di [[Acme]]"}]})
    noop = json.dumps({"docs": [{"action": "edit", "slug": "anwar-saputra",
                                 "old_str": "same", "new_str": "same"}]})
    assert ev.evaluate(noop, expect, level=5).score < ev.evaluate(good, expect, level=5).score


def test_rename_op_scored(ev):
    expect = {"expect_actions": {"rename": [{"slug": "anwar-saputra-pak-anwar"}]}}
    good = json.dumps({"docs": [{"action": "rename", "slug": "anwar-saputra-pak-anwar",
                                 "new_title": "Anwar Saputra"}]})
    r = ev.evaluate(good, expect, level=5)
    assert r.status == "passed"
    # rename has no body/type/links -> those components must not drag it down
    assert r.details["components"]["valid_type"] == 1.0
    assert r.details["components"]["links"] == 1.0


def test_dedupe_op_scored(ev):
    expect = {"expect_actions": {"dedupe": [{"slug": "borobudur"}]}}
    good = json.dumps({"docs": [{"action": "dedupe", "slug": "borobudur"}]})
    r = ev.evaluate(good, expect, level=5)
    assert r.status == "passed"


def test_maintenance_op_missing_slug_not_satisfied(ev):
    expect = {"expect_actions": {"rename": [{"slug": "anwar-saputra-pak-anwar"}]}}
    wrong = json.dumps({"docs": [{"action": "rename", "slug": "someone-else",
                                  "new_title": "X"}]})
    assert ev.evaluate(wrong, expect, level=5).details["missing_entities"] == ["anwar-saputra-pak-anwar"]


def test_update_dedup_slug(ev):
    expect = {"expect_actions": {"update": [
        {"title": "Jakarta", "type": "place", "slug": "jakarta"}]}}
    right = json.dumps({"docs": [{
        "action": "update", "slug": "jakarta", "title": "Jakarta", "type": "place",
        "description": "Capital.", "body": "User membuka kantor baru di sini.",
    }]})
    wrong = json.dumps({"docs": [{
        "action": "update", "slug": "bandung", "title": "Jakarta", "type": "place",
        "description": "Capital.", "body": "User membuka kantor baru di sini.",
    }]})
    # Right slug fully satisfies the update entity; wrong slug does not.
    assert ev.evaluate(right, expect, level=4).score > ev.evaluate(wrong, expect, level=4).score
    assert ev.evaluate(wrong, expect, level=4).details["missing_entities"] == ["Jakarta"]


def test_create_instead_of_update_not_satisfied(ev):
    """A subject that should be updated must not be satisfied by a create."""
    expect = {"expect_actions": {"update": [
        {"title": "Jakarta", "type": "place", "slug": "jakarta"}]}}
    created = json.dumps({"docs": [{
        "action": "create", "title": "Jakarta", "type": "place",
        "description": "Capital.", "body": "User membuka kantor baru di [[Jakarta]].",
    }]})
    assert ev.evaluate(created, expect, level=4).details["missing_entities"] == ["Jakarta"]


def test_expect_empty(ev):
    expect = {"expect_actions": {"create": [], "update": []}}
    assert ev.evaluate('{"docs": []}', expect, level=5).score == 1.0
    non_empty = json.dumps({"docs": [{"action": "create", "title": "X", "type": "note",
                                       "description": "d", "body": "b"}]})
    assert ev.evaluate(non_empty, expect, level=5).score == 0.0


def test_missing_docs_key_fails(ev):
    assert ev.evaluate('{"items": []}', GOOD_EXPECT, level=2).score == 0.0


def test_entity_coverage(ev):
    """Producing all expected entities scores higher than missing one."""
    expect = {"expect_actions": {"create": [
        {"title": "Budi", "type": "person"},
        {"title": "Acme Corp", "type": ["company", "organization"]}]}}
    full = json.dumps({"docs": [
        {"action": "create", "title": "Budi", "type": "person", "description": "Engineer.",
         "body": "Budi adalah backend engineer di [[Acme Corp]]."},
        {"action": "create", "title": "Acme Corp", "type": "company", "description": "Company.",
         "body": "Acme Corp adalah perusahaan tempat [[Budi]] bekerja."},
    ]})
    partial = json.dumps({"docs": [
        {"action": "create", "title": "Budi", "type": "person", "description": "Engineer.",
         "body": "Budi adalah backend engineer."},
    ]})
    full_r = ev.evaluate(full, expect, level=1)
    partial_r = ev.evaluate(partial, expect, level=1)
    assert full_r.details["missing_entities"] == []
    assert "Acme Corp" in partial_r.details["missing_entities"]
    assert full_r.score > partial_r.score


def test_entity_wrong_type_not_matched(ev):
    """An entity created with the wrong doc type does not count as covered."""
    expect = {"expect_actions": {"create": [{"title": "Bandung", "type": "place"}]}}
    wrong_type = json.dumps({"docs": [
        {"action": "create", "title": "Bandung", "type": "person", "description": "x",
         "body": "Bandung."},
    ]})
    assert "Bandung" in ev.evaluate(wrong_type, expect, level=1).details["missing_entities"]


def test_entity_alias_and_substring_match(ev):
    """Lenient title matching: 'Candi Borobudur' satisfies an expected 'Borobudur'."""
    expect = {"expect_actions": {"create": [{"title": "Borobudur", "type": ["place", "venue"]}]}}
    r = json.dumps({"docs": [
        {"action": "create", "title": "Candi Borobudur", "type": "venue", "description": "x",
         "body": "Candi Borobudur."},
    ]})
    assert ev.evaluate(r, expect, level=3).details["missing_entities"] == []


def test_trailing_relations_block_detected():
    assert _has_trailing_link_block("Sari is a PM.\n\n[[Evonic]]\n[[User]]") is True
    assert _has_trailing_link_block("## Relations\n[[Evonic]]") is True
    assert _has_trailing_link_block("Sari works at [[Evonic]] with User.") is False


# ----------------------------------------------------------- drift guard ----

_REGEN_HINT = "python -m evaluator.test_definitions.knowledge_builder._generate"


def test_domain_system_prompt_matches_live():
    """The domain system_prompt must equal the static head of the live prompt.

    If this fails, production _AUTHOR_DOCS_PROMPT changed — re-run: """ + _REGEN_HINT
    base = os.path.dirname(os.path.abspath(_generate.__file__))
    with open(os.path.join(base, "domain.json"), encoding="utf-8") as f:
        domain = json.load(f)
    assert domain.get("system_prompt") == _generate.render_system_prompt(), (
        "domain system_prompt is stale — regenerate")


def test_stored_prompts_match_live_author_docs_prompt():
    """Every generated test prompt must equal a fresh render of the dynamic tail.

    If this fails, the production _AUTHOR_DOCS_PROMPT changed — re-run: """ + _REGEN_HINT
    base = os.path.dirname(os.path.abspath(_generate.__file__))
    for sc in _generate.SCENARIOS:
        path = os.path.join(base, f"level_{sc['level']}", f"{sc['id']}.json")
        assert os.path.exists(path), f"missing generated test: {path}"
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        expected_prompt = _generate.render_user_prompt(sc["existing"], sc["source"])
        assert stored["prompt"] == expected_prompt, (
            f"{sc['id']} prompt is stale — regenerate test cases")


# --------------------------------------------------------- registration ----

def test_domain_discovered():
    domain_ids = {d.id for d in test_loader.scan_domains()}
    assert "knowledge_builder" in domain_ids


def test_evaluator_resolves():
    ev = get_evaluator("knowledge_builder", evaluator_type="knowledge_builder")
    assert isinstance(ev, KnowledgeBuilderEvaluator)
    assert ev.uses_pass2 is False
