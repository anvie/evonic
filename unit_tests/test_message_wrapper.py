import os
import tempfile
import threading
import unittest

from backend.agent_runtime import context
from backend.agent_runtime.runtime import _should_wrap_user_message
from models.boolean import message_wrapper_enabled, normalize_bool
from models.db import db


PROTOCOL_HEADING = "## Message Wrapper Protocol"


class MessageWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.db_path
        self.original_agents_dir = context._AGENTS_DIR
        db.db_path = os.path.join(self.temp_dir.name, "evonic.db")
        db._tls = threading.local()
        db._init_tables()
        context._AGENTS_DIR = os.path.join(self.temp_dir.name, "agents")
        context._system_prompt_cache.clear()

    def tearDown(self):
        context._system_prompt_cache.clear()
        context._AGENTS_DIR = self.original_agents_dir
        db._tls = threading.local()
        db.db_path = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def agent(agent_id="wrapper-agent", **overrides):
        agent = {
            "id": agent_id,
            "system_prompt": "Base prompt",
            "inject_agent_id": False,
            "inject_datetime": False,
            "sandbox_enabled": False,
        }
        agent.update(overrides)
        return agent

    def test_false_like_values_are_normalized(self):
        for value in (False, 0, "0", "false", "False", "no", "off", "disabled"):
            self.assertFalse(normalize_bool(value))
        for value in (True, 1, "1", "true", "yes", "on", "enabled"):
            self.assertTrue(normalize_bool(value))

    def test_global_setting_is_used_only_when_agent_setting_is_unset(self):
        db.set_setting("message_wrapper_enabled", "0")
        self.assertFalse(message_wrapper_enabled(self.agent(), db))
        self.assertFalse(_should_wrap_user_message(self.agent()))
        self.assertTrue(message_wrapper_enabled(
            self.agent(message_wrapper_enabled="1"), db
        ))

        db.set_setting("message_wrapper_enabled", "1")
        self.assertFalse(message_wrapper_enabled(
            self.agent(message_wrapper_enabled="0"), db
        ))
        self.assertFalse(_should_wrap_user_message(
            self.agent(message_wrapper_enabled=False)
        ))

    def test_agent_create_and_update_preserve_false_like_values(self):
        db.create_agent({"id": "wrapper-db-agent", "message_wrapper_enabled": 0})
        self.assertEqual(db.get_agent("wrapper-db-agent")["message_wrapper_enabled"], 0)

        self.assertTrue(db.update_agent(
            "wrapper-db-agent", {"message_wrapper_enabled": "false"}
        ))
        self.assertEqual(db.get_agent("wrapper-db-agent")["message_wrapper_enabled"], 0)

        self.assertTrue(db.update_agent(
            "wrapper-db-agent", {"message_wrapper_enabled": "1"}
        ))
        self.assertEqual(db.get_agent("wrapper-db-agent")["message_wrapper_enabled"], 1)

    def test_system_prompt_protocol_follows_effective_setting(self):
        db.set_setting("message_wrapper_enabled", "0")
        self.assertNotIn(PROTOCOL_HEADING, context.build_system_prompt(self.agent()))
        self.assertIn(PROTOCOL_HEADING, context.build_system_prompt(
            self.agent("wrapper-override-on", message_wrapper_enabled=True)
        ))

        db.set_setting("message_wrapper_enabled", "1")
        self.assertNotIn(PROTOCOL_HEADING, context.build_system_prompt(
            self.agent("wrapper-override-off", message_wrapper_enabled=False)
        ))

    def test_global_setting_change_invalidates_cached_system_prompt(self):
        agent = self.agent("wrapper-cache-agent")

        db.set_setting("message_wrapper_enabled", "1")
        self.assertIn(PROTOCOL_HEADING, context.build_system_prompt(agent))
        self.assertTrue(context._system_prompt_cache[agent["id"]]["message_wrapper_enabled"])

        db.set_setting("message_wrapper_enabled", "0")
        self.assertNotIn(PROTOCOL_HEADING, context.build_system_prompt(agent))
        self.assertFalse(context._system_prompt_cache[agent["id"]]["message_wrapper_enabled"])


if __name__ == "__main__":
    unittest.main()
