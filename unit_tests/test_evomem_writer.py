"""Tests for evomem_writer.py — structured markdown writer.

These tests write to a temporary brain dir (no evomem binary needed): the
writer is pure Python. `_ensure_brain` is patched to True so writes proceed
without a real `.evomem.db`.
"""

import os
import pytest
from unittest.mock import patch

from backend.agent_runtime import evomem_writer as W


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """Point the writer at a temp brain dir and skip the DB-existence check."""
    root = tmp_path / "agents" / "a1" / "brain"
    monkeypatch.setattr(W, "_get_evomem_dir", lambda agent_id: str(root))
    monkeypatch.setattr(W, "_ensure_brain", lambda agent_id: True)
    return root


class TestSlugify:
    def test_deterministic_and_ascii(self):
        assert W.slugify("Acme Corp") == "acme-corp"
        assert W.slugify("Acme Corp") == W.slugify("Acme Corp")

    def test_strips_accents_and_symbols(self):
        assert W.slugify("Café R&D!!") == "cafe-r-d"

    def test_empty(self):
        assert W.slugify("") == ""
        assert W.slugify("!!!") == ""


class TestUpsertDoc:
    def test_creates_doc_with_frontmatter_and_inline_links(self, brain):
        slug = W.upsert_doc("a1", title="Jakarta",
                            body="Ibu kota Indonesia; dekat [[Monas]].",
                            doc_type="place", description="Ibu kota Indonesia",
                            tags=["place"], aliases=["DKI Jakarta"])
        assert slug == "jakarta"
        text = (brain / "jakarta.md").read_text()
        assert 'title: "Jakarta"' in text
        assert "type: place" in text
        assert 'description: "Ibu kota Indonesia"' in text
        assert "[[Monas]]" in text             # inline link kept in the body
        assert "DKI Jakarta" in text           # alias
        assert "## Relationships" not in text  # no blockquote-edge section

    def test_places_doc_in_collection_folder(self, brain):
        slug = W.upsert_doc("a1", title="Monas", body="Tugu di [[Jakarta]].",
                            doc_type="place", description="Monas", folder="riset-jkt")
        assert slug == "riset-jkt/monas"
        assert (brain / "riset-jkt" / "monas.md").exists()

    def test_rewrite_preserves_created_and_merges_tags(self, brain):
        import re
        W.upsert_doc("a1", title="X", body="a", doc_type="note",
                     description="d", tags=["t1"])
        created = re.search(r"created: (\S+)", (brain / "x.md").read_text()).group(1)
        W.upsert_doc("a1", title="X", body="b", doc_type="note",
                     description="d", tags=["t2"])
        text = (brain / "x.md").read_text()
        assert f"created: {created}" in text          # created preserved
        assert "t1" in text and "t2" in text          # tags merged (union)


class TestSessionGroupCoercion:
    """session/group are reserved for collection index.md; a standalone doc that
    asks for them is coerced to `note` (prevents stray flat 'session' files)."""

    def test_session_coerced_to_note(self, brain):
        W.upsert_doc("a1", title="Trip", body="x", doc_type="session", description="d")
        text = (brain / "trip.md").read_text()
        assert "type: note" in text and "type: session" not in text

    def test_group_coerced_to_note(self, brain):
        W.upsert_doc("a1", title="Grp", body="x", doc_type="group", description="d")
        assert "type: note" in (brain / "grp.md").read_text()


class TestAppendToDoc:
    def test_appends_paragraph_preserving_body(self, brain):
        W.upsert_doc("a1", title="Jakarta", body="Paragraf satu.",
                     doc_type="place", description="d")
        assert W.append_to_doc("a1", "jakarta", "Paragraf dua dengan [[Monas]].")
        text = (brain / "jakarta.md").read_text()
        assert "Paragraf satu." in text and "Paragraf dua" in text
        assert "[[Monas]]" in text

    def test_returns_false_when_doc_missing(self, brain):
        assert W.append_to_doc("a1", "nope", "x") is False


class TestCollections:
    def test_create_collection_writes_typed_index(self, brain):
        folder = W.create_collection("a1", folder="Riset XYZ", title="Riset XYZ",
                                     kind="session", description="riset")
        assert folder == "riset-xyz"
        idx = (brain / "riset-xyz" / "index.md").read_text()
        assert "type: session" in idx and "## Contents" in idx

    def test_create_collection_rejects_bad_kind(self, brain):
        assert W.create_collection("a1", "X", "X", kind="bogus", description="d") == ""

    def test_add_to_collection_index_idempotent(self, brain):
        W.create_collection("a1", "Riset", "Riset", kind="group", description="d")
        assert W.add_to_collection_index("a1", "riset", "Monas") is True
        assert "[[Monas]]" in (brain / "riset" / "index.md").read_text()
        # second add of the same title is a no-op
        assert W.add_to_collection_index("a1", "riset", "Monas") is False


class TestReadDoc:
    def test_reads_frontmatter_and_body(self, brain):
        W.upsert_doc("a1", title="Jakarta", body="Body dengan [[Monas]].",
                     doc_type="place", description="d")
        doc = W.read_doc("a1", "jakarta")
        assert doc["title"] == "Jakarta"
        assert doc["frontmatter"]["type"] == "place"
        assert "[[Monas]]" in doc["body"]

    def test_missing_returns_none(self, brain):
        assert W.read_doc("a1", "nope") is None


class TestMarkDirty:
    def test_schedules_debounced_sync(self, monkeypatch):
        calls = []
        monkeypatch.setattr(W, "_SYNC_DEBOUNCE_SECONDS", 0.05)
        monkeypatch.setattr(W, "_evomem_sync", lambda agent_id: calls.append(agent_id))
        W.mark_dirty("a1")
        import time
        time.sleep(0.2)
        assert calls == ["a1"]

    def test_bursts_coalesce_into_one_sync(self, monkeypatch):
        calls = []
        monkeypatch.setattr(W, "_SYNC_DEBOUNCE_SECONDS", 0.1)
        monkeypatch.setattr(W, "_evomem_sync", lambda agent_id: calls.append(agent_id))
        for _ in range(5):
            W.mark_dirty("a1")
        import time
        time.sleep(0.3)
        assert calls == ["a1"]  # coalesced


class TestFolderBecomesACollection:
    """Evomem indexes the root and collection folders only; a document in any other folder would be invisible to the
    index, the graph and search while still showing in the list view."""

    def test_writing_into_a_new_folder_creates_its_collection_index(self, brain):
        slug = W.upsert_doc("a1", title="Advisory", body="Text.", folder="inbox-notes")
        assert slug == "inbox-notes/advisory"
        index = brain / "inbox-notes" / "index.md"
        assert index.exists() and "type: group" in index.read_text()
        assert (brain / "inbox-notes" / "advisory.md").exists()

    def test_an_existing_collection_index_is_left_alone(self, brain):
        W.create_collection("a1", "riset", "Riset", kind="session")
        before = (brain / "riset" / "index.md").read_text()
        W.upsert_doc("a1", title="Doc", body="x", folder="riset")
        assert (brain / "riset" / "index.md").read_text() == before

    def test_the_inbox_and_folders_that_are_not_slugs_are_not_turned_into_collections(self, brain):
        W.upsert_doc("a1", title="Mail", body="x", folder="inbox")
        W.upsert_doc("a1", title="Odd", body="x", folder="My Folder")
        assert not (brain / "inbox" / "index.md").exists() and not (brain / "My Folder" / "index.md").exists()

    def test_ensure_collections_repairs_existing_folders(self, brain):
        (brain / "pring").mkdir(parents=True)
        (brain / "pring" / "old-note.md").write_text("---\ntitle: Old\ntype: note\n---\nBody")
        (brain / "empty").mkdir()
        (brain / ".hidden").mkdir()
        (brain / ".hidden" / "x.md").write_text("x")
        (brain / "inbox").mkdir()
        (brain / "inbox" / "m.md").write_text("m")
        (brain / "riset").mkdir()
        (brain / "riset" / "index.md").write_text("---\ntitle: R\ntype: session\n---\n")
        assert W.ensure_collections("a1") == ["pring"]
        assert (brain / "pring" / "index.md").exists() and not (brain / "empty" / "index.md").exists()
        assert not (brain / "inbox" / "index.md").exists() and W.ensure_collections("a1") == []      # idempotent

    def test_a_sync_repairs_folders_first(self, brain):
        (brain / "pring").mkdir(parents=True)
        (brain / "pring" / "n.md").write_text("---\ntitle: N\ntype: note\n---\nBody")
        seen = []
        with patch.object(W, "_evomem_sync", lambda agent_id: seen.append((brain / "pring" / "index.md").exists()) or True):
            W.sync_now("a1")
        assert seen == [True]                                  # the index existed by the time the sync ran


class TestDeleteDoc:
    def test_removes_the_file_and_schedules_a_sync(self, brain):
        slug = W.upsert_doc("a1", title="Advisory", body="x", folder="pring")
        marked = []
        with patch.object(W, "mark_dirty", lambda agent_id: marked.append(agent_id)):
            assert W.delete_doc("a1", slug) is True
        assert not (brain / "pring" / "advisory.md").exists() and marked == ["a1"]
        assert (brain / "pring" / "index.md").exists()          # the collection itself stays

    def test_refuses_what_it_should_not_touch(self, brain, tmp_path):
        W.upsert_doc("a1", title="Doc", body="x", folder="pring")
        outside = tmp_path / "agents" / "a1" / "secret.md"
        outside.write_text("keep")
        with patch.object(W, "mark_dirty", lambda agent_id: None):
            assert W.delete_doc("a1", "pring/index") is False and W.delete_doc("a1", "index") is False
            assert W.delete_doc("a1", "") is False and W.delete_doc("a1", "pring/missing") is False
            assert W.delete_doc("a1", "../secret") is False
        assert outside.exists() and (brain / "pring" / "index.md").exists()


class TestLinkedFrom:
    def test_finds_documents_that_link_to_a_doc_by_title_alias_or_slug(self, brain):
        target = W.upsert_doc("a1", title="Ivanti Flaw", body="x", folder="pring", aliases=["Ivanti"])
        W.upsert_doc("a1", title="Plan", body="We should look at [[Ivanti Flaw]] first.")
        W.upsert_doc("a1", title="Other", body="See [[ivanti|the Ivanti thing]].")
        W.upsert_doc("a1", title="Unrelated", body="Nothing here.")
        assert sorted(W.linked_from("a1", target)) == ["other", "plan"]

    def test_collection_indexes_and_the_doc_itself_do_not_count(self, brain):
        target = W.upsert_doc("a1", title="Ivanti Flaw", body="Mentions [[Ivanti Flaw]] itself.", folder="pring")
        W.add_to_collection_index("a1", "pring", "Ivanti Flaw")
        assert W.linked_from("a1", target) == []
        assert W.linked_from("a1", "pring/missing") == []


class TestExpiry:
    """An optional lifetime: docs with ``expires`` are deleted once it has passed, docs without are never touched."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    NOW = _dt(2026, 11, 1, 12, 0, tzinfo=_tz.utc)

    def make(self, brain, title, expires=None, **kw):
        return W.upsert_doc("a1", title=title, body=f"About {title}.", folder=kw.pop("folder", ""), expires=expires, **kw)

    def test_a_doc_can_be_written_with_a_lifetime_and_keeps_it_through_every_rewrite(self, brain):
        slug = self.make(brain, "Advisory", expires="2026-12-01", folder="pring")
        assert "expires: 2026-12-01" in (brain / "pring" / "advisory.md").read_text()
        W.upsert_doc("a1", title="Advisory", body="Rewritten.", folder="pring")                    # no expires given: kept
        assert W.read_doc("a1", slug)["frontmatter"]["expires"] == "2026-12-01"
        W.append_to_doc("a1", slug, "More.")
        W.replace_in_doc("a1", slug, "Rewritten.", "Changed.")
        W.dedupe_doc("a1", slug)
        assert W.read_doc("a1", slug)["frontmatter"]["expires"] == "2026-12-01"
        W.upsert_doc("a1", title="Advisory", body="Again.", folder="pring", expires="")            # explicitly removed
        assert "expires" not in W.read_doc("a1", slug)["frontmatter"]

    def test_a_new_expiry_replaces_the_old_one(self, brain):
        slug = self.make(brain, "Advisory", expires="2026-12-01")
        W.upsert_doc("a1", title="Advisory", body="x", expires="2027-03-01T08:00:00+00:00")
        assert W.read_doc("a1", slug)["frontmatter"]["expires"] == "2027-03-01T08:00:00+00:00"

    def test_expiry_values(self):
        assert W.parse_expiry("2026-12-01") == self._dt(2026, 12, 1, 23, 59, 59, tzinfo=self._tz.utc)   # a date: the end of that day
        assert W.parse_expiry("2026-12-01T08:00:00+00:00") == self._dt(2026, 12, 1, 8, 0, tzinfo=self._tz.utc)
        assert W.parse_expiry("2026-12-01T08:00:00") == self._dt(2026, 12, 1, 8, 0, tzinfo=self._tz.utc)
        assert W.parse_expiry("") is None and W.parse_expiry(None) is None and W.parse_expiry("next week") is None

    def test_the_sweep_deletes_what_has_expired_and_nothing_else(self, brain):
        with patch.object(W, "mark_dirty", lambda a: None):
            old = self.make(brain, "Old advisory", expires="2026-10-31", folder="pring")
            fresh = self.make(brain, "Fresh advisory", expires="2026-12-31", folder="pring")
            forever = self.make(brain, "Permanent note", folder="pring")                       # no lifetime: never deleted
            rooted = self.make(brain, "Root doc", expires="2026-01-01")
            out = W.sweep_expired("a1", now=self.NOW)
        assert sorted(out["deleted"]) == sorted([old, rooted]) and out["kept"] == []
        assert not (brain / "pring" / "old-advisory.md").exists() and not (brain / "root-doc.md").exists()
        assert (brain / "pring" / "fresh-advisory.md").exists() and (brain / "pring" / "permanent-note.md").exists()
        assert (brain / "pring" / "index.md").exists()                                         # collections stay
        with patch.object(W, "mark_dirty", lambda a: None):
            assert W.sweep_expired("a1", now=self.NOW + self._td(days=400))["deleted"] == [fresh]
        assert (brain / "pring" / "permanent-note.md").exists()                                # even a year on

    def test_a_doc_that_others_link_to_is_kept_for_good_instead_of_leaving_dangling_links(self, brain):
        slug = self.make(brain, "Old advisory", expires="2026-10-31", folder="pring")
        W.upsert_doc("a1", title="Plan", body="We follow [[Old advisory]] closely.")
        with patch.object(W, "mark_dirty", lambda a: None):
            out = W.sweep_expired("a1", now=self.NOW)
            assert out == {"deleted": [], "kept": [slug]}
            assert "expires" not in W.read_doc("a1", slug)["frontmatter"]
            assert W.sweep_expired("a1", now=self.NOW + self._td(days=999)) == {"deleted": [], "kept": []}

    def test_an_unreadable_expiry_never_deletes_anything(self, brain):
        slug = self.make(brain, "Odd", expires="whenever")
        with patch.object(W, "mark_dirty", lambda a: None):
            assert W.sweep_expired("a1", now=self.NOW + self._td(days=9999)) == {"deleted": [], "kept": []}
        assert (brain / "odd.md").exists()

    def test_hidden_folders_and_the_inbox_are_not_swept(self, brain):
        (brain / "inbox").mkdir(parents=True)
        (brain / "inbox" / "mail.md").write_text("---\ntitle: M\nexpires: 2020-01-01\n---\nx")
        (brain / ".trash").mkdir()
        (brain / ".trash" / "t.md").write_text("---\ntitle: T\nexpires: 2020-01-01\n---\nx")
        with patch.object(W, "mark_dirty", lambda a: None):
            assert W.sweep_expired("a1", now=self.NOW) == {"deleted": [], "kept": []}

    def test_set_expiry_adds_and_removes_a_lifetime(self, brain):
        slug = self.make(brain, "Doc")
        assert W.set_expiry("a1", slug, "2026-12-01") and W.read_doc("a1", slug)["frontmatter"]["expires"] == "2026-12-01"
        assert W.set_expiry("a1", slug, None) and "expires" not in W.read_doc("a1", slug)["frontmatter"]
        assert W.set_expiry("a1", "missing", "2026-12-01") is False


class TestKbExpiryJanitor:
    def test_the_janitor_sweeps_every_agent_and_reports_what_it_did(self, tmp_path, monkeypatch):
        from backend import kb_expiry
        roots = {a: tmp_path / a for a in ("a1", "a2", "a3")}
        for a, root in roots.items():
            if a != "a3":
                root.mkdir()
        monkeypatch.setattr(W, "_get_evomem_dir", lambda a: str(roots[a]))
        monkeypatch.setattr("backend.agent_runtime.evomem_client._get_evomem_dir", lambda a: str(roots[a]))
        monkeypatch.setattr(W, "mark_dirty", lambda a: None)
        (roots["a1"] / "stale.md").write_text("---\ntitle: Stale\ntype: note\nexpires: 2020-01-01\n---\nx")
        (roots["a1"] / "keep.md").write_text("---\ntitle: Keep\ntype: note\n---\nx")
        (roots["a2"] / "fine.md").write_text("---\ntitle: Fine\ntype: note\nexpires: 2999-01-01\n---\nx")
        agents = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]                       # a3 has no knowledge base at all
        import models.db as models_db
        monkeypatch.setattr(models_db.db, "get_agents", lambda: agents)
        assert kb_expiry.run_once() == {"a1": {"deleted": ["stale"], "kept": []}}
        assert not (roots["a1"] / "stale.md").exists() and (roots["a1"] / "keep.md").exists() and (roots["a2"] / "fine.md").exists()
        assert kb_expiry.run_once() == {}                                          # nothing left to do

    def test_the_janitor_starts_once(self, monkeypatch):
        from backend import kb_expiry
        monkeypatch.setattr(kb_expiry, "run_once", lambda: {})
        kb_expiry.stop()
        kb_expiry._thread = None
        try:
            assert kb_expiry.start(interval=0.05) is True and kb_expiry.start(interval=0.05) is False
        finally:
            kb_expiry.stop()
            kb_expiry._thread = None


class TestKbListShowsTheLifetime:
    def test_a_doc_with_a_lifetime_carries_it_in_the_tree(self, tmp_path):
        from routes.agents import _build_kb_tree, _kb_expiry
        tmp_path = tmp_path / "kb"
        tmp_path.mkdir()
        (tmp_path / "a.md").write_text('---\ntitle: "A"\nexpires: 2026-12-01\n---\nbody')
        (tmp_path / "b.md").write_text("---\ntitle: B\n---\nbody")
        (tmp_path / "c.md").write_text("no frontmatter, expires: 2026-12-01")
        (tmp_path / "d.txt").write_text("---\nexpires: 2026-12-01\n---\n")
        tree = {n["name"]: n["expires"] for n in _build_kb_tree(str(tmp_path))}
        assert tree == {"a.md": "2026-12-01", "b.md": None, "c.md": None, "d.txt": None}
        assert _kb_expiry(str(tmp_path / "missing.md")) is None
