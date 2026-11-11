"""Regression tests for super-agent SYSTEM.md handling.

Background: ``set_owner_name`` used to copy ``defaults/super_agent_system_prompt.md``
verbatim over ``agents/<id>/SYSTEM.md``. Because that template starts with the
literal ``{communication_style}`` placeholder, every call wiped the operator's
customisations and leaked the placeholder into the model prompt.
"""

import os
import tempfile
import threading
import unittest

from backend.agent_runtime import context
from backend.system_prompt_store import write_system_prompt
from backend.tools import super_agent_tools
from models.db import db

STYLE_PLACEHOLDER = "{communication_style}"


class SuperAgentPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.db_path
        self.original_agents_dir = context._AGENTS_DIR
        self.original_tool_agents_dir = super_agent_tools.AGENTS_DIR

        db.db_path = os.path.join(self.temp_dir.name, "evonic.db")
        db._tls = threading.local()
        db._init_tables()

        agents_dir = os.path.join(self.temp_dir.name, "agents")
        context._AGENTS_DIR = agents_dir
        super_agent_tools.AGENTS_DIR = agents_dir
        context._system_prompt_cache.clear()

        self.super_id = "test_super"
        self.agent_dir = os.path.join(agents_dir, self.super_id)
        os.makedirs(self.agent_dir, exist_ok=True)
        db.create_agent({"id": self.super_id, "name": "Test Super", "is_super": True})
        db.set_setting("super_agent_id", self.super_id)

    def tearDown(self):
        context._system_prompt_cache.clear()
        context._AGENTS_DIR = self.original_agents_dir
        super_agent_tools.AGENTS_DIR = self.original_tool_agents_dir
        db._tls = threading.local()
        db.db_path = self.original_db_path
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------
    @property
    def prompt_path(self):
        return os.path.join(self.agent_dir, "SYSTEM.md")

    @property
    def backup_dir(self):
        return os.path.join(self.agent_dir, ".backup")

    def write_prompt(self, content):
        with open(self.prompt_path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def read_prompt(self):
        with open(self.prompt_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def agent_dict(self):
        return {
            "id": self.super_id,
            "is_super": True,
            "inject_datetime": False,
            "sandbox_enabled": False,
        }

    # -- set_owner_name must never rewrite SYSTEM.md ----------------------
    def test_set_owner_name_preserves_customised_prompt(self):
        custom = "You are my custom super agent.\n\nRule: answer in Indonesian.\n"
        self.write_prompt(custom)

        result = super_agent_tools._exec_set_owner_name({"name": "Robin"})

        self.assertTrue(result.get("success"))
        self.assertEqual(db.get_setting("owner_name"), "Robin")
        self.assertEqual(self.read_prompt(), custom)

    def test_set_owner_name_does_not_seed_a_prompt_file(self):
        if os.path.exists(self.prompt_path):
            os.remove(self.prompt_path)

        super_agent_tools._exec_set_owner_name({"name": "Robin"})

        self.assertFalse(os.path.exists(self.prompt_path))

    def test_set_owner_name_requires_a_name(self):
        self.write_prompt("keep me")
        self.assertIn("error", super_agent_tools._exec_set_owner_name({"name": "  "}))
        self.assertEqual(self.read_prompt(), "keep me")

    # -- placeholder sanitisation ----------------------------------------
    def test_setup_system_prompt_has_no_style_placeholder(self):
        from backend.setup import build_system_prompt

        prompt = build_system_prompt()
        self.assertTrue(prompt.strip())
        self.assertNotIn(STYLE_PLACEHOLDER, prompt)

    def test_setup_system_prompt_substitutes_style_text(self):
        from backend.setup import build_system_prompt

        prompt = build_system_prompt("Be formal.")
        self.assertIn("Be formal.", prompt)
        self.assertNotIn(STYLE_PLACEHOLDER, prompt)

    def test_runtime_prompt_strips_leaked_placeholder(self):
        self.write_prompt(STYLE_PLACEHOLDER + "\n\nCustom body.\n")

        prompt = context.build_system_prompt(self.agent_dict())

        self.assertNotIn(STYLE_PLACEHOLDER, prompt)
        self.assertIn("Custom body.", prompt)

    # -- backup rotation --------------------------------------------------
    def test_write_system_prompt_backs_up_previous_content(self):
        self.write_prompt("old prompt")

        write_system_prompt(self.agent_dir, "new prompt")

        self.assertEqual(self.read_prompt(), "new prompt")
        backups = sorted(os.listdir(self.backup_dir))
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.backup_dir, backups[0]), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old prompt")

    def test_write_system_prompt_skips_backup_for_identical_content(self):
        self.write_prompt("same prompt")

        write_system_prompt(self.agent_dir, "same prompt")

        self.assertFalse(os.path.exists(self.backup_dir))

    def test_write_system_prompt_rotates_old_backups(self):
        for index in range(8):
            write_system_prompt(self.agent_dir, "prompt %d" % index)

        backups = os.listdir(self.backup_dir)
        self.assertEqual(len(backups), 5)

    # -- onboarding injection still works ---------------------------------
    def test_onboarding_footer_present_without_owner_name(self):
        self.write_prompt("Base prompt.\n")
        db.set_setting("owner_name", "")

        prompt = context.build_system_prompt(self.agent_dict())

        self.assertIn("First-Time Onboarding", prompt)

    def test_owner_name_is_injected_after_onboarding(self):
        self.write_prompt("Base prompt.\n")
        db.set_setting("owner_name", "Robin")

        prompt = context.build_system_prompt(self.agent_dict())

        self.assertIn("**Robin**", prompt)
        self.assertNotIn("First-Time Onboarding", prompt)


if __name__ == "__main__":
    unittest.main()
