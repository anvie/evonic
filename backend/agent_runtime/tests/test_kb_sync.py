"""
Unit tests for the consolidated evomem/KB layout in evomem_client.py.

The agent's kb/ directory IS the evomem knowledge root: markdown files are
scanned in place (no brain/ mirror), top-level kb/*.md become KB pages with
slug = filename stem, and KB-to-KB wiki-links use the bare [[slug]] form.
"""
import os
import sys
import shutil
import tempfile
import unittest

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from backend.agent_runtime.evomem_client import (
    _get_evomem_dir,
    _run,
    is_available,
)


class TestPathResolution(unittest.TestCase):
    def test_get_evomem_dir(self):
        """The agent's kb/ dir is the evomem knowledge root."""
        self.assertEqual(_get_evomem_dir("testagent"), "agents/testagent/kb")


@unittest.skipUnless(is_available(), "evomem binary not available")
class TestKBSyncIntegration(unittest.TestCase):
    """Integration tests using the evomem binary against a kb/ knowledge root."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.kb_dir = os.path.join(self.tmpdir, "kb")  # kb/ IS the knowledge root
        os.makedirs(self.kb_dir)
        _run(self.kb_dir, ["init"], expect_json=False)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sync(self):
        return _run(self.kb_dir, ["sync"])

    def _stats(self):
        return _run(self.kb_dir, ["stats"])

    def _page(self, slug):
        return _run(self.kb_dir, ["page", slug])

    def _add(self, filename, content):
        with open(os.path.join(self.kb_dir, filename), "w") as f:
            f.write(content)

    def _remove(self, filename):
        os.remove(os.path.join(self.kb_dir, filename))

    # ---- slugs ----

    def test_top_level_slug_is_stem(self):
        """A top-level kb/<name>.md gets slug=<name> (no prefix, no extension)."""
        self._add("evonic.md", "---\ntitle: Evonic\ntype: note\n---\n# Evonic")
        self._sync()
        page = self._page("evonic")
        self.assertIsNotNone(page)
        self.assertEqual(page["slug"], "evonic")

    def test_dotted_filename_slug(self):
        """KB filename with dots: my.file.name.md -> slug='my.file.name'."""
        self._add("my.file.name.md", "---\ndescription: dotted\n---\n# Dotted")
        self._sync()
        self.assertEqual(self._page("my.file.name")["slug"], "my.file.name")

    # ---- wiki-links (bare form) ----

    def test_bare_wikilink_resolves(self):
        """[[evonic]] resolves to the top-level evonic page."""
        self._add("evonic.md", "---\ntitle: Evonic\n---\n# Evonic")
        self._add("linker.md", "---\ntitle: Linker\n---\n# Linker\n[[evonic]]")
        result = self._sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.get("links_resolved", 0), 1)

    def test_multiple_bare_wikilinks(self):
        """Multiple bare wiki-links in one document are all parsed."""
        self._add("page-a.md", "---\ntitle: A\n---\n# A")
        self._add("page-b.md", "---\ntitle: B\n---\n# B")
        self._add("linker.md",
                  "---\ntitle: L\n---\n# L\nSee [[page-a]] and [[page-b]]")
        self.assertEqual(self._sync().get("links_resolved", 0), 2)

    def test_dangling_wikilink(self):
        """Links to non-existent pages are reported as dangling, not crashes."""
        self._add("linker.md", "---\ntitle: L\n---\n# L\n[[missing1]] [[missing2]]")
        self._sync()
        self.assertEqual(self._stats()["dangling_links"], 2)

    def test_entities_link_still_works(self):
        """[[entities/demo]] (subdir-prefixed) continues to resolve."""
        ent_dir = os.path.join(self.kb_dir, "entities")
        os.makedirs(ent_dir, exist_ok=True)
        with open(os.path.join(ent_dir, "demo.md"), "w") as f:
            f.write("---\ntitle: Demo\ntype: entity\n---\n# Demo")
        self._add("linker.md", "---\ntitle: L\n---\n# L\n[[entities/demo]]")
        self.assertGreaterEqual(self._sync().get("links_resolved", 0), 1)

    # ---- deletion ----

    def test_file_removal_soft_deletes(self):
        """Removing a KB file results in soft-deletion (deleted_pages > 0)."""
        self._add("to-delete.md", "---\ndescription: x\n---\n# Delete Me")
        self._sync()
        self.assertEqual(self._stats()["pages"], 1)
        self._remove("to-delete.md")
        self._sync()
        stats = self._stats()
        self.assertEqual(stats["pages"], 0)
        self.assertEqual(stats["deleted_pages"], 1)

    # ---- chunking ----

    def test_large_file_chunking(self):
        """Very large KB files are added (chunked) without error."""
        content = "---\ndescription: large\n---\n# Large\n\n" + "Paragraph. " * 500
        self._add("large.md", content)
        self.assertGreaterEqual(self._sync().get("added", 0), 1)


if __name__ == "__main__":
    unittest.main()
