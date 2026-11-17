"""Real, isolated integration tests for the doc-model knowledge pipeline.

These exercise the FULL stack with the real evomem binary — they produce actual
KB markdown files on disk and index them, then assert the Obsidian-style graph:
inline ``[[links]]`` resolved by title across folders, collections (session/group
folders + index.md), and dangling links.

Base data mirrors one of a real agent's conversations (Jakarta / Monumen Nasional /
Andi Wijaya). The only thing mocked is the authoring LLM call — its JSON output
is fixed to a realistic representative shape so the test is deterministic; every
file write, ``sync``, and graph query below is real.

Isolation: ``monkeypatch.chdir(tmp_path)`` makes the evomem knowledge root
(``agents/<id>/kb``) live under a throwaway temp dir, so nothing touches the real
``agents/``. Skips automatically when the evomem binary isn't built.
"""

import glob
import os
import threading
from unittest.mock import patch

import pytest

from backend.agent_runtime.evomem_client import (
    is_available, get_graph_for_viz, query_kb_graph, _run, _get_evomem_dir,
)
from backend.agent_runtime import evomem_writer as W
from backend.agent_runtime import memory_manager as M

pytestmark = pytest.mark.skipif(
    not is_available(), reason="evomem binary not available")

AGENT = "agent_a_kb_test"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    """Isolate the evomem knowledge root under a temp cwd + force the evomem engine.

    Disables the Knowledge Organizer sub-agent so these tests exercise the
    deterministic author→write→sync→graph path with a mocked ``_kb_llm_json`` (the
    organizer is covered separately in test_kb_organizer.py).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
    # Use the deterministic non-agentic author path (a single ``_kb_llm_json``
    # call), never the agentic organizer sub-agent. ``off`` would skip filing
    # entirely, so it must be ``non-agentic`` here. Zero the persistent filing
    # cooldown so successive ``process_knowledge`` calls in one test all run.
    monkeypatch.setenv("EVOMEM_KB_ORGANIZER", "non-agentic")
    monkeypatch.setenv("EVOMEM_KB_ORGANIZER_MIN_INTERVAL_SECONDS", "0")
    return tmp_path


def _read(slug: str) -> str:
    with open(os.path.join(_get_evomem_dir(AGENT), slug + ".md"), encoding="utf-8") as f:
        return f.read()


def _stats() -> dict:
    return _run(_get_evomem_dir(AGENT), ["stats"]) or {}


# ── 1. Writer → sync → graph (deterministic core, no LLM) ────────────────────

def test_writer_produces_inline_linked_docs_and_indexes(brain):
    """upsert_doc writes rich, inline-linked docs; sync builds the graph with
    Obsidian title resolution across docs and dangling for missing targets."""
    assert W.upsert_doc(
        AGENT, title="Jakarta", doc_type="place",
        description="Ibu kota Indonesia; lokasi User.",
        body="Jakarta adalah ibu kota Indonesia. User tinggal di sini.",
        tags=["place"]) == "jakarta"
    assert W.upsert_doc(
        AGENT, title="Monumen Nasional", doc_type="place",
        description="Monas, tugu di pusat Jakarta.",
        body="Monumen Nasional (Monas) adalah tugu di pusat [[Jakarta]]. "
             "Terletak dekat [[Hotel Borobudur]].",
        tags=["place", "monument"]) == "monumen-nasional"
    assert W.upsert_doc(
        AGENT, title="Andi Wijaya", doc_type="person",
        description="User; tinggal di Jakarta.",
        body="Andi Wijaya adalah User yang tinggal di [[Jakarta]].",
        tags=["person"]) == "andi-wijaya"
    assert W.sync_now(AGENT)

    # On disk: rich frontmatter + INLINE links (NOT a bottom "Relationships" list).
    monas = _read("monumen-nasional")
    assert "type: place" in monas
    assert "[[Jakarta]]" in monas and "[[Hotel Borobudur]]" in monas
    assert "## Relationships" not in monas

    # [[Jakarta]] resolves by TITLE to jakarta.md (Obsidian), not by raw slug.
    g = query_kb_graph(AGENT, "monumen-nasional.md")
    assert "jakarta" in [o["slug"] for o in g["outgoing"]]
    # [[Hotel Borobudur]] has no doc → dangling, never guessed.
    assert any("borobudur" in d.lower() for d in g["outgoing_dangling"])

    # Incoming to Jakarta is matched by resolved dst_doc_id (title links count).
    inc = {i["slug"] for i in query_kb_graph(AGENT, "jakarta.md")["incoming"]}
    assert {"monumen-nasional", "andi-wijaya"} <= inc

    st = _stats()
    assert st["docs"] == 3

    # Viz: every live doc is a typed node; no entities/ dir involved.
    viz = get_graph_for_viz(AGENT)
    assert set(viz["nodes"]) == {"jakarta", "monumen-nasional", "andi-wijaya"}
    assert viz["nodes"]["jakarta"]["type"] == "place"
    assert viz["nodes"]["andi-wijaya"]["type"] == "person"


# ── 2. Full authoring pipeline from a summary (LLM mocked) ────────────────────

def test_authoring_pipeline_from_summary(brain):
    """process_knowledge → _author_docs writes the LLM's docs and indexes them."""
    docs_json = {"docs": [
        {"action": "create", "title": "Jakarta", "type": "place",
         "description": "Ibu kota Indonesia.", "tags": ["place"],
         "body": "Jakarta adalah ibu kota Indonesia tempat User tinggal."},
        {"action": "create", "title": "Monumen Nasional", "type": "place",
         "description": "Monas di Jakarta.", "tags": ["place"],
         "body": "Monumen Nasional adalah tugu di pusat [[Jakarta]]."},
    ]}
    summary = (
        "User membahas Jakarta dan Monumen Nasional (Monas) yang berada di "
        "pusat Jakarta.\n\n## Entities & Relationships\n"
        "- Jakarta — ibu kota Indonesia; lokasi User\n"
        "- Monumen Nasional — tugu di Jakarta; located_in Jakarta")

    with patch.object(M, "_kb_llm_json", return_value=docs_json):
        M.process_knowledge({"id": AGENT}, "sess-1", summary, threading.Lock())
    assert W.sync_now(AGENT)

    assert os.path.isfile(os.path.join(_get_evomem_dir(AGENT), "jakarta.md"))
    monas = _read("monumen-nasional")
    assert "type: place" in monas and "[[Jakarta]]" in monas
    g = query_kb_graph(AGENT, "monumen-nasional.md")
    assert "jakarta" in [o["slug"] for o in g["outgoing"]]
    assert _stats()["docs"] == 2


def test_pipeline_never_writes_a_flat_session_doc(brain):
    """A standalone doc the LLM mis-types as `session` is coerced to `note`
    (session/group are reserved for collections via create_collection)."""
    docs_json = {"docs": [
        {"action": "create", "title": "Jakarta Session", "type": "session",
         "description": "Konteks lokasi User di Jakarta.", "tags": [],
         "body": "Catatan: User berada di [[Jakarta]]."}]}
    with patch.object(M, "_kb_llm_json", return_value=docs_json):
        M.process_knowledge({"id": AGENT}, "s", "User di Jakarta.", threading.Lock())
    assert W.sync_now(AGENT)

    doc = _read("jakarta-session")
    assert "type: note" in doc
    assert "type: session" not in doc
    # It is a flat file, not a collection folder.
    assert not os.path.isdir(os.path.join(_get_evomem_dir(AGENT), "jakarta-session"))


# ── 3. Collections (session/group folders + index.md) ────────────────────────

def test_collection_placement_and_index(brain):
    """create_collection activates a folder; docs authored next land inside it
    and are listed in the collection's index.md."""
    r = M.create_collection_tool(AGENT, "sess-2", "Riset Jakarta", "session", "riset jkt")
    assert r.get("folder") == "riset-jakarta"

    docs_json = {"docs": [
        {"action": "create", "title": "Monumen Nasional", "type": "place",
         "description": "Monas.", "tags": ["place"],
         "body": "Monas adalah tugu di pusat [[Jakarta]]."}]}
    with patch.object(M, "_kb_llm_json", return_value=docs_json):
        M.process_knowledge({"id": AGENT}, "sess-2", "Riset Monas di Jakarta.", threading.Lock())
    assert W.sync_now(AGENT)

    # Doc landed inside the collection folder.
    assert os.path.isfile(
        os.path.join(_get_evomem_dir(AGENT), "riset-jakarta", "monumen-nasional.md"))
    # Collection index is a session doc that lists the member.
    idx = _read("riset-jakarta/index")
    assert "type: session" in idx
    assert "[[Monumen Nasional]]" in idx

    viz = get_graph_for_viz(AGENT)
    assert "riset-jakarta/monumen-nasional" in viz["nodes"]
    assert "riset-jakarta/index" in viz["nodes"]
    assert viz["nodes"]["riset-jakarta/index"]["type"] == "session"


def test_cross_collection_links_resolve_by_title(brain):
    """A doc in one collection links by title to a doc in another collection /
    root — the graph is one vault, folders are physical only."""
    # Collection A holds Pesanggrahan.
    M.create_collection_tool(AGENT, "sA", "Riset Jakarta", "session", "")
    with patch.object(M, "_kb_llm_json", return_value={"docs": [
            {"action": "create", "title": "Pesanggrahan", "type": "place",
             "description": "Kecamatan di Jakarta Selatan.", "tags": ["place"],
             "body": "Pesanggrahan adalah kecamatan di Jakarta Selatan."}]}):
        M.process_knowledge({"id": AGENT}, "sA", "Pesanggrahan.", threading.Lock())

    # Collection B holds a venue linking to Pesanggrahan (collection A).
    M.create_collection_tool(AGENT, "sB", "Riset Kuliner", "group", "")
    with patch.object(M, "_kb_llm_json", return_value={"docs": [
            {"action": "create", "title": "Ayam Taliwang", "type": "venue",
             "description": "Rumah makan.", "tags": ["venue"],
             "body": "Rumah makan enak di [[Pesanggrahan]]."}]}):
        M.process_knowledge({"id": AGENT}, "sB", "Makan di Pesanggrahan.", threading.Lock())
    assert W.sync_now(AGENT)

    g = query_kb_graph(AGENT, "riset-kuliner/ayam-taliwang.md")
    # [[Pesanggrahan]] (collection B doc) resolves to the doc in collection A.
    assert "riset-jakarta/pesanggrahan" in [o["slug"] for o in g["outgoing"]]
    assert g["outgoing_dangling"] == []


# ── 4. Dedupe / append (idempotent updates) ──────────────────────────────────

def test_dedupe_appends_to_existing_doc(brain):
    """A second pass about the same subject appends to the existing doc instead
    of creating a duplicate file."""
    with patch.object(M, "_kb_llm_json", return_value={"docs": [
            {"action": "create", "title": "Jakarta", "type": "place",
             "description": "Ibu kota.", "tags": ["place"],
             "body": "Jakarta adalah ibu kota Indonesia."}]}):
        M.process_knowledge({"id": AGENT}, "s", "Jakarta.", threading.Lock())
    assert W.sync_now(AGENT)
    before = _read("jakarta")

    new_prose = "User mengunjungi [[Monumen Nasional]] di Jakarta."
    with patch.object(M, "_kb_llm_json", return_value={"docs": [
            {"action": "update", "slug": "jakarta", "title": "Jakarta",
             "type": "place", "description": "Ibu kota.", "tags": ["place"],
             "body": new_prose}]}):
        M.process_knowledge({"id": AGENT}, "s", "Lebih banyak tentang Jakarta.", threading.Lock())
    assert W.sync_now(AGENT)
    after = _read("jakarta")

    assert "[[Monumen Nasional]]" in after
    assert len(after) > len(before)
    # Exactly one Jakarta doc — no duplicate created.
    assert len(glob.glob(os.path.join(_get_evomem_dir(AGENT), "jakarta*.md"))) == 1
    assert _stats()["docs"] == 1
