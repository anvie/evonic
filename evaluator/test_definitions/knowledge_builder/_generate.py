"""
Generate Knowledge Builder domain + test cases from the LIVE author-docs prompt.

The production prompt (``_AUTHOR_DOCS_PROMPT`` in
``backend/agent_runtime/memory_manager.py``) has a static instruction head and a
dynamic ``EXISTING documents: / SOURCE:`` tail. We split it there:

- the **static head** (authoring rules + JSON shape) becomes the domain-level
  ``system_prompt`` — shared by every test, set once (not duplicated per test);
- the **dynamic tail**, filled with each scenario's existing docs + source,
  becomes that test's ``prompt``.

Both halves are derived from the live constant, so re-running this script keeps
the eval in lockstep with production. A drift-guard unit test
(unit_tests/test_knowledge_builder_eval.py) fails if they fall out of sync.

    python -m evaluator.test_definitions.knowledge_builder._generate
"""

import json
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Marker where the static instructions end and the per-test payload begins.
_SPLIT_MARKER = "EXISTING documents:"

DOMAIN_META = {
    "id": "knowledge_builder",
    "name": "Knowledge Builder",
    "description": (
        "Tests a model's ability to produce valid author-docs JSON for the evomem "
        "knowledge pipeline (structure, doc types, create/update + dedup, expected "
        "entities, inline [[wiki-links]]). The shared authoring instructions live in "
        "this domain's system_prompt; both it and the per-test prompts are generated "
        "from the live _AUTHOR_DOCS_PROMPT via _generate.py."
    ),
    "icon": "book",
    "color": "#7C3AED",
    "evaluator_id": "knowledge_builder",
    "system_prompt_mode": "overwrite",
}


def _existing_text(existing):
    """Mirror the EXISTING-docs rendering in memory_manager._author_docs."""
    return "\n".join(
        f"[{d['slug']}] {d['title']} :: {d['description']}" for d in existing
    ) or "(none yet)"


def _split_template():
    from backend.agent_runtime.memory_manager import _AUTHOR_DOCS_PROMPT, _DEFAULT_KB_GUIDANCE
    head, tail = _AUTHOR_DOCS_PROMPT.split(_SPLIT_MARKER, 1)
    return head, tail, _DEFAULT_KB_GUIDANCE


# Maintenance ops on EXISTING docs. The live agentic organizer
# (_KB_ORGANIZER_SYSTEM_PROMPT) documents these; the non-agentic author prompt
# does not, so we append a concise contract here to let the eval exercise them.
_EXTRA_OPS_DOC = """

MAINTENANCE OPERATIONS on EXISTING docs (identify the doc by its `slug`):
- "edit": fix a wrong phrase or [[link]] in a doc's body. Shape:
  {"action":"edit","slug":"<existing slug>","old_str":"<short UNIQUE exact text from the body>","new_str":"<replacement>"}
  `new_str` MUST differ from `old_str` — never emit a no-op edit. Also use this to wrap a
  plain-text mention of an entity that now has its own doc into a [[link]]
  (e.g. old_str "bekerja dengan Anwar Saputra" -> new_str "bekerja dengan [[Anwar Saputra]]").
- "rename": fix a typo'd or nickname-polluted doc name. Shape:
  {"action":"rename","slug":"<existing slug>","new_title":"<corrected PURE name>"}
- "dedupe": remove duplicated content from a self-concatenated doc. Shape:
  {"action":"dedupe","slug":"<existing slug>"}
Use these only when the SOURCE warrants it; otherwise stick to create/update."""


def render_system_prompt():
    """Static authoring instructions — the domain-level system prompt."""
    head, _, guidance = _split_template()
    return head.format(guidance=guidance).strip() + _EXTRA_OPS_DOC


def render_user_prompt(existing, source):
    """Per-test payload: the EXISTING docs + SOURCE the model must act on."""
    _, tail, _ = _split_template()
    return (_SPLIT_MARKER + tail).format(
        existing=_existing_text(existing), source=source)


# 2 scenarios per level, levels 1-5. `existing` + `source` feed the prompt;
# `expected` drives KnowledgeBuilderEvaluator scoring. `expect_actions` maps each
# action to the subjects that must surface under it (matched by title/alias, with
# an optional `type`; `update` entities carry the existing `slug` they map to).
# An all-empty expect_actions means "keep nothing". `require_links` is orthogonal.
SCENARIOS = [
    # ---- Level 1: single create, simple subject, links optional ----
    {
        "id": "kb_create_person_1", "level": 1,
        "name": "Create person doc",
        "description": "Author a person doc (and the company they work at) from a simple fact.",
        "existing": [],
        "source": "User bertemu Budi, seorang backend engineer di Acme Corp.",
        "expected": {
            "expect_actions": {"create": [
                {"title": "Budi", "type": "person"},
                {"title": "Acme Corp", "type": ["company", "organization"]},
            ]},
            "require_links": False,
        },
    },
    {
        "id": "kb_create_place_1", "level": 1,
        "name": "Create place doc",
        "description": "Bandung must be captured as a place entity.",
        "existing": [],
        "source": "User tinggal di Bandung, sebuah kota di Jawa Barat.",
        "expected": {
            "expect_actions": {"create": [
                {"title": "Bandung", "type": "place"},
                {"title": "Jawa Barat", "type": "place"},
            ]},
            "require_links": False,
        },
    },

    # ---- Level 2: create that mentions other subjects -> inline links required ----
    {
        "id": "kb_create_links_1", "level": 2,
        "name": "Create with inline links",
        "description": "A fact mentioning multiple subjects must weave inline [[links]].",
        "existing": [],
        "source": "User bekerja sebagai engineer di Evonic, sebuah perusahaan AI, "
                  "bersama rekannya Sari yang menjadi product manager.",
        "expected": {
            "expect_actions": {"create": [
                {"title": "Sari", "type": "person"},
                {"title": "Evonic", "type": ["company", "organization"]},
            ]},
            "require_links": True,
        },
    },
    {
        "id": "kb_create_event_1", "level": 2,
        "name": "Create event with links",
        "description": "An event mentioning a place and host should link them inline.",
        "existing": [],
        "source": "User menghadiri konferensi AI Summit di Jakarta yang diselenggarakan "
                  "oleh Nusantara Tech.",
        "expected": {
            "expect_actions": {"create": [
                {"title": "AI Summit", "type": ["event", "note"]},
                {"title": "Jakarta", "type": "place"},
                {"title": "Nusantara Tech", "type": ["organization", "company"]},
            ]},
            "require_links": True,
        },
    },

    # ---- Level 2: image in the source -> the subject's doc must set `thumbnail` ----
    {
        "id": "kb_create_thumbnail_1", "level": 2,
        "name": "Create with thumbnail (HTML img)",
        "description": "An <img> tag for a subject must become that doc's thumbnail.",
        "existing": [],
        "source": ("User menambahkan foto profil rekannya, Dewi Anggraini Putri: "
                   "<img src=\"https://cdn.example.com/dewi.webp\" "
                   "alt=\"Dewi Anggraini Putri\" width=\"150\">. "
                   "Dia seorang desainer produk di Acme Corp."),
        "expected": {
            "expect_actions": {"create": [
                {"title": "Dewi Anggraini Putri", "type": "person", "thumbnail": True},
                {"title": "Acme Corp", "type": ["company", "organization"]},
            ]},
            "require_links": False,
        },
    },
    {
        "id": "kb_create_thumbnail_2", "level": 2,
        "name": "Create with thumbnail (Markdown image)",
        "description": "A Markdown image (logo) for a company must become its thumbnail.",
        "existing": [],
        "source": ("User menyimpan logo perusahaan Larisin: "
                   "![Logo Larisin](https://cdn.example.com/larisin-logo.png). "
                   "Larisin adalah startup POS untuk UMKM."),
        "expected": {
            "expect_actions": {"create": [
                {"title": "Larisin", "type": ["company", "organization"], "thumbnail": True},
            ]},
            "require_links": False,
        },
    },

    # ---- Level 3: richer source -> multiple docs with cross-links ----
    {
        "id": "kb_multidoc_trip_1", "level": 3,
        "name": "Multiple docs from a trip",
        "description": "A richer narrative should yield several linked docs.",
        "existing": [],
        "source": "User berlibur ke Yogyakarta, mengunjungi Candi Borobudur, dan makan "
                  "gudeg di Warung Bu Tjitro bersama temannya Andi.",
        "expected": {
            "expect_actions": {"create": [
                {"title": "Yogyakarta", "type": "place"},
                {"title": "Candi Borobudur", "type": ["place", "venue"]},
                {"title": "Warung Bu Tjitro", "type": "venue"},
                {"title": "Andi", "type": "person"},
            ]},
            "require_links": True,
        },
    },
    {
        "id": "kb_multidoc_startup_1", "level": 3,
        "name": "Multiple docs from a startup story",
        "description": "Founder, company, and product should each be a linked doc.",
        "existing": [],
        "source": "User mendirikan startup bernama Larisin yang membuat produk POS bernama "
                  "LarisinKasir untuk UMKM di Indonesia.",
        "expected": {
            "expect_actions": {"create": [
                {"title": "Larisin", "type": ["company", "organization"]},
                {"title": "LarisinKasir", "type": "product"},
            ]},
            "require_links": True,
        },
    },

    # ---- Level 4: dedup -> update an existing doc with delta only ----
    {
        "id": "kb_update_place_1", "level": 4,
        "name": "Update existing place",
        "description": "New info about an already-known subject must be an update, not a duplicate.",
        "existing": [{"slug": "jakarta", "title": "Jakarta",
                      "description": "Capital of Indonesia; User's home city."}],
        "source": "User membuka kantor cabang baru perusahaannya di Jakarta tahun ini.",
        "expected": {
            "expect_actions": {"update": [
                {"title": "Jakarta", "type": "place", "slug": "jakarta"},
            ]},
            "require_links": False,
        },
    },
    {
        "id": "kb_update_person_1", "level": 4,
        "name": "Update existing person",
        "description": "A new fact about a known person updates that doc by slug.",
        "existing": [{"slug": "budi", "title": "Budi",
                      "description": "Backend engineer at Acme Corp."}],
        "source": "Budi baru saja dipromosikan menjadi engineering lead di Acme Corp.",
        "expected": {
            "expect_actions": {"update": [
                {"title": "Budi", "type": "person", "slug": "budi"},
            ]},
            "require_links": False,
        },
    },

    # ---- Level 5: edge cases ----
    {
        "id": "kb_skip_chatter_1", "level": 5,
        "name": "Skip ephemeral chatter",
        "description": "Pure pleasantries have nothing durable; the answer is no docs.",
        "existing": [],
        "source": "User menyapa, menanyakan kabar, kami bertukar salam, lalu User "
                  "mengucapkan terima kasih dan pamit.",
        "expected": {"expect_actions": {"create": [], "update": []}},
    },
    {
        "id": "kb_mixed_create_update_1", "level": 5,
        "name": "Mixed create + update",
        "description": "One known subject updated, one new subject created, both linked.",
        "existing": [{"slug": "jakarta", "title": "Jakarta",
                      "description": "Capital of Indonesia; User's home city."}],
        "source": "User memperkenalkan investornya, Rina, dari Sequoia, dan menyebut "
                  "bahwa kantornya di Jakarta kini punya 50 karyawan.",
        "expected": {
            "expect_actions": {
                "create": [
                    {"title": "Rina", "type": "person"},
                    {"title": "Sequoia", "type": ["company", "organization"]},
                ],
                "update": [
                    {"title": "Jakarta", "type": "place", "slug": "jakarta"},
                ],
            },
            "require_links": True,
        },
    },

    # ---- Level 5: maintenance ops on existing docs (edit / rename / dedupe) ----
    {
        "id": "kb_edit_link_1", "level": 5,
        "name": "Edit body to fix a link",
        "description": "Wrap a now-documented entity's plain-text mention into a [[link]] via an edit op.",
        "existing": [{"slug": "anwar-saputra", "title": "Anwar Saputra",
                      "description": "Software engineer."}],
        "source": ("Koreksi dokumen Anwar Saputra: frasa \"bekerja di Acme\" harus menjadi "
                   "\"bekerja di [[Acme]]\" karena Acme kini punya dokumennya sendiri."),
        "expected": {
            "expect_actions": {"edit": [{"slug": "anwar-saputra"}]},
            "require_links": False,
        },
    },
    {
        "id": "kb_rename_1", "level": 5,
        "name": "Rename a nickname-polluted doc",
        "description": "Fix a doc whose slug/title was polluted with a nickname; nickname becomes an alias.",
        "existing": [{"slug": "anwar-saputra-pak-anwar", "title": "Anwar Saputra (Pak Anwar)",
                      "description": "Engineer; the title was polluted with a nickname."}],
        "source": ("Rapikan dokumen berjudul \"Anwar Saputra (Pak Anwar)\": nama kanoniknya "
                   "seharusnya \"Anwar Saputra\" dengan \"Pak Anwar\" sebagai alias."),
        "expected": {
            "expect_actions": {"rename": [{"slug": "anwar-saputra-pak-anwar"}]},
            "require_links": False,
        },
    },
    {
        "id": "kb_dedupe_1", "level": 5,
        "name": "Dedupe a self-duplicated doc",
        "description": "A doc whose content was concatenated onto itself should be deduped.",
        "existing": [{"slug": "borobudur", "title": "Borobudur",
                      "description": "Candi; its body was accidentally duplicated (concatenated onto itself)."}],
        "source": ("Dokumen Borobudur isinya muncul dua kali (ter-append ke dirinya sendiri) — "
                   "rapikan duplikasinya."),
        "expected": {
            "expect_actions": {"dedupe": [{"slug": "borobudur"}]},
            "require_links": False,
        },
    },
]


def build_test(scenario):
    """Return the on-disk test JSON dict for a scenario."""
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "description": scenario["description"],
        "prompt": render_user_prompt(scenario["existing"], scenario["source"]),
        "expected": scenario["expected"],
        "evaluator_id": "knowledge_builder",
        "timeout_ms": 30000,
        "weight": 1.0,
        "enabled": True,
    }


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    # Domain definition carries the shared system prompt.
    domain = dict(DOMAIN_META)
    domain["system_prompt"] = render_system_prompt()
    _write_json(os.path.join(THIS_DIR, "domain.json"), domain)

    written = ["domain.json"]
    for sc in SCENARIOS:
        level_dir = os.path.join(THIS_DIR, f"level_{sc['level']}")
        os.makedirs(level_dir, exist_ok=True)
        path = os.path.join(level_dir, f"{sc['id']}.json")
        _write_json(path, build_test(sc))
        written.append(os.path.relpath(path, THIS_DIR))

    print(f"Wrote {len(written)} files:")
    for p in written:
        print("  " + p)


if __name__ == "__main__":
    main()
