"""Unit tests for :mod:`backend.agent_factory`.

The factory is the single source of truth for the agent lifecycle.  These
tests pin the contract that:

* every allowlisted spec field is actually persisted (a field silently
  dropped because ``Database.update_agent()``'s allowlist was not a superset
  of the spec surface would fail the round-trip test),
* an agent created from a spec is byte-for-byte what ``routes/agents.py``
  would have produced (defaults, managed tools, filesystem layout),
* creation is atomic: a failure at any step leaves no row, no mappings and no
  directories behind, and the original exception propagates unchanged,
* secret variable values never reach the error or log path,
* concurrent instantiation cannot create two agents with the same id.
"""

import ast
import io
import json
import logging
import os
import shutil
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from models.db import db

from backend import agent_factory
from backend.agent_factory import (
    ARTIFACT_TOOLS,
    DEFAULTS,
    DEFAULT_KB_FILES,
    SPEC_FIELD_ALLOWLIST,
    VISION_TOOLS,
    AgentAlreadyExistsError,
    AgentNotFoundError,
    SpecValidationError,
    apply_spec,
    create_agent,
    normalize_spec,
    resolve_tools,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: One distinctive, non-default value for every allowlisted spec field.  Kept
#: as a literal (not derived from ``DEFAULTS``) so that the round-trip test
#: cannot agree with the implementation by construction.
FULL_SPEC_VALUES = {
    "name": "Round Trip Agent",
    "description": "Round trip description",
    "avatar_path": "avatars/round_trip.png",
    "enabled": 0,
    "vision_enabled": 0,
    "summarize_threshold": 7,
    "summarize_tail": 9,
    "summarize_prompt": "Summarize in five words.",
    "message_buffer_seconds": 4.5,
    "inject_agent_id": 0,
    "inject_datetime": 0,
    "send_intermediate_responses": 0,
    "outbound_buffer_seconds": 3.5,
    "enable_agent_state": 0,
    "sandbox_enabled": 1,
    "attachments_enabled": 1,
    "attachment_max_size_mb": 42,
    "artifacts_enabled": 0,
    "safety_checker_enabled": 0,
    "disable_parallel_tool_execution": 1,
    "disable_turn_prefetch": 1,
    "agent_messaging_enabled": 0,
    "tool_compression_enabled": 0,
    "message_wrapper_enabled": 0,
    "fallback_model_id": "fallback-model-x",
    "model_id": "model-x",
    "audio_enabled": 1,
    "video_enabled": 1,
    "run_as_user": "agentuser",
    "bash_exec_enabled": 1,
    "vision_model_id": "vision-model-x",
    "inter_agent_clear_context": 1,
    "builtin_tools_enabled": 0,
    "messaging_acl": json.dumps(["someone_else"]),
    "messaging_acl_mode": "blacklist",
    "memory_engine": "fts5",
    "kb_organizer_mode": "off",
    "enable_atg": 1,
    "enable_cmp": 1,
    "always_execute": 1,
}


def _read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class AgentFactoryTestBase(unittest.TestCase):
    """Shared temp workspace that mimics the repository root layout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent_factory_test_")
        defaults_dir = os.path.join(self.tmp, "defaults")
        os.makedirs(defaults_dir, exist_ok=True)
        self.default_kb_content = {}
        for target_name, source_name in DEFAULT_KB_FILES:
            content = "default kb for {}".format(target_name)
            self.default_kb_content[target_name] = content
            with open(os.path.join(defaults_dir, source_name), "w", encoding="utf-8") as handle:
                handle.write(content)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent_dir(self, agent_id):
        return os.path.join(self.tmp, "agents", agent_id)

    def kb_dir(self, agent_id):
        return os.path.join(self.agent_dir(agent_id), "kb")

    def workspace_dir(self, agent_id):
        return os.path.join(self.tmp, "shared", "agents", agent_id)

    def assert_agent_artifacts_removed(self, agent_id):
        self.assertIsNone(db.get_agent(agent_id))
        self.assertEqual(db.get_agent_tools(agent_id), [])
        self.assertEqual(db.get_agent_skills(agent_id), [])
        self.assertEqual(db.get_agent_variables(agent_id), [])
        self.assertFalse(os.path.exists(self.agent_dir(agent_id)))
        self.assertFalse(os.path.exists(self.workspace_dir(agent_id)))

    def assert_default_kb_copied(self, agent_id):
        for target_name, content in self.default_kb_content.items():
            path = os.path.join(self.kb_dir(agent_id), target_name)
            self.assertTrue(os.path.isfile(path), "missing default KB file {}".format(target_name))
            self.assertEqual(_read_text(path), content)


class SpecNormalizationTests(AgentFactoryTestBase):
    def test_allowlist_excludes_server_controlled_columns(self):
        for key in ("is_super", "created_at", "updated_at", "last_active_at", "session_count", "owner", "id"):
            self.assertNotIn(key, SPEC_FIELD_ALLOWLIST)

    def test_defaults_cover_every_allowlisted_field(self):
        self.assertEqual(set(DEFAULTS), set(SPEC_FIELD_ALLOWLIST))

    def test_full_spec_values_cover_every_allowlisted_field(self):
        self.assertEqual(set(FULL_SPEC_VALUES), set(SPEC_FIELD_ALLOWLIST))

    def test_unknown_key_is_rejected_loudly(self):
        with self.assertRaises(SpecValidationError) as ctx:
            normalize_spec({"id": "x_agent", "workspace": "/tmp/elsewhere"})
        self.assertIn("workspace", str(ctx.exception))

    def test_server_controlled_key_is_rejected(self):
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "is_super": True})

    def test_unknown_configuration_key_is_rejected(self):
        with self.assertRaises(SpecValidationError) as ctx:
            normalize_spec({"id": "x_agent", "configuration": {"not_a_setting": 1}})
        self.assertIn("not_a_setting", str(ctx.exception))

    def test_flat_and_nested_collision_is_rejected(self):
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "enabled": True, "configuration": {"enabled": False}})

    def test_invalid_id_is_rejected(self):
        for bad_id in ("Bad Id", "Bad-Id", "agent_sub_1"):
            with self.assertRaises(SpecValidationError, msg=bad_id):
                normalize_spec({"id": bad_id, "name": "X"})

    def test_length_limits(self):
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "name": "n" * 201})
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "description": "d" * 2001})
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "system_prompt": "p" * 102401})

    def test_messaging_acl_list_is_serialized(self):
        normalized = normalize_spec({"id": "x_agent", "messaging_acl": ["a", "b"]})
        self.assertEqual(normalized["values"]["messaging_acl"], json.dumps(["a", "b"]))

    def test_empty_model_id_resets_to_default(self):
        normalized = normalize_spec({"id": "x_agent", "model_id": ""})
        self.assertIsNone(normalized["values"]["model_id"])

    def test_enum_aliases_are_canonicalized(self):
        normalized = normalize_spec({"id": "x_agent", "kb_organizer_mode": "on"})
        self.assertEqual(normalized["values"]["kb_organizer_mode"], "agentic")

    def test_invalid_enum_is_rejected(self):
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "memory_engine": "sqlite"})

    def test_variable_validation(self):
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "variables": [{"key": "1BAD", "value": "v"}]})
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "variables": [
                {"key": "DUP", "value": "a"}, {"key": "DUP", "value": "b"}]})
        with self.assertRaises(SpecValidationError):
            normalize_spec({"id": "x_agent", "variables": [{"key": "OK", "value": "v", "extra": 1}]})

    def test_variable_error_never_echoes_the_value(self):
        secret_value = "sensitive-value-must-not-leak-42"
        with self.assertRaises(SpecValidationError) as ctx:
            normalize_spec({"id": "x_agent", "variables": [
                {"key": "TOKEN", "value": {"nested": secret_value}}]})
        self.assertNotIn(secret_value, str(ctx.exception))

    def test_kb_path_traversal_is_rejected(self):
        for bad_path in ("../escape.md", "/absolute.md", "dir\\win.md", ""):
            with self.assertRaises(SpecValidationError, msg=bad_path):
                normalize_spec({"id": "x_agent", "knowledge_base": [{"path": bad_path, "content": "x"}]})

    def test_resolve_tools_lock(self):
        self.assertEqual(
            resolve_tools(["web_search"], artifacts_enabled=True, vision_enabled=True),
            sorted({"web_search"} | ARTIFACT_TOOLS | VISION_TOOLS),
        )
        self.assertEqual(
            resolve_tools(
                ["web_search", "save_artifact", "describe_image"],
                artifacts_enabled=False,
                vision_enabled=False,
            ),
            ["web_search"],
        )


class CreateAgentTests(AgentFactoryTestBase):
    def test_round_trip_persists_every_allowlisted_field(self):
        spec = dict(FULL_SPEC_VALUES)
        spec.update({
            "id": "round_trip_agent",
            "system_prompt": "You are a round trip agent.",
            "tools": ["web_search"],
            "skills": ["github", "kanban"],
            "variables": [{"key": "TOKEN", "value": "round-trip-secret", "is_secret": True}],
            "knowledge_base": [{"path": "guide/start.md", "content": "Spec KB file"}],
        })

        agent_id = create_agent(spec, base_dir=self.tmp)
        self.assertEqual(agent_id, "round_trip_agent")

        row = db.get_agent(agent_id)
        self.assertIsNotNone(row)
        for key, expected in FULL_SPEC_VALUES.items():
            self.assertEqual(row[key], expected, "field '{}' did not round-trip".format(key))

        # Fields the factory owns on behalf of the server.
        self.assertEqual(row["system_prompt"], spec["system_prompt"])
        self.assertEqual(row["is_super"], 0)
        self.assertEqual(row["workspace"], self.workspace_dir(agent_id))

        # Managed tool lock: both capabilities are off in the spec.
        tools = set(db.get_agent_tools(agent_id))
        self.assertIn("web_search", tools)
        self.assertFalse(tools & ARTIFACT_TOOLS)
        self.assertFalse(tools & VISION_TOOLS)

        self.assertEqual(set(db.get_agent_skills(agent_id)), {"github", "kanban"})
        variables = {row_["key"]: row_ for row_ in db.get_agent_variables(agent_id)}
        self.assertEqual(set(variables), {"TOKEN"})
        self.assertEqual(variables["TOKEN"]["value"], "round-trip-secret")
        self.assertEqual(variables["TOKEN"]["is_secret"], 1)

        # Filesystem layout.
        self.assertEqual(_read_text(os.path.join(self.agent_dir(agent_id), "SYSTEM.md")),
                         spec["system_prompt"])
        self.assert_default_kb_copied(agent_id)
        self.assertEqual(_read_text(os.path.join(self.kb_dir(agent_id), "guide", "start.md")),
                         "Spec KB file")
        self.assertTrue(os.path.isdir(os.path.join(self.workspace_dir(agent_id), "artifacts")))

    def test_defaults_match_the_create_path(self):
        agent_id = create_agent({"id": "defaults_agent"}, base_dir=self.tmp)
        row = db.get_agent(agent_id)

        for key, expected in DEFAULTS.items():
            if key == "name":
                continue  # defaults to the agent id
            self.assertEqual(row[key], expected, "default for '{}' drifted".format(key))
        self.assertEqual(row["name"], "defaults_agent")
        self.assertEqual(row["description"], "")

        tools = set(db.get_agent_tools(agent_id))
        self.assertEqual(tools, set(ARTIFACT_TOOLS) | set(VISION_TOOLS))
        self.assertEqual(db.get_agent_skills(agent_id), [])
        self.assertEqual(db.get_agent_variables(agent_id), [])
        self.assertTrue(os.path.isfile(os.path.join(self.agent_dir(agent_id), "SYSTEM.md")))

    def test_nested_configuration_form_is_supported(self):
        agent_id = create_agent({
            "id": "nested_agent",
            "name": "Nested Agent",
            "configuration": {"enabled": False, "summarize_threshold": 8, "vision_enabled": False},
        }, base_dir=self.tmp)
        row = db.get_agent(agent_id)
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["summarize_threshold"], 8)
        self.assertEqual(row["vision_enabled"], 0)
        self.assertEqual(row["summarize_tail"], DEFAULTS["summarize_tail"])
        tools = set(db.get_agent_tools(agent_id))
        self.assertEqual(tools, set(ARTIFACT_TOOLS))

    def test_derived_id_is_unique_per_name(self):
        first = create_agent({"name": "Derived Name"}, base_dir=self.tmp)
        second = create_agent({"name": "Derived Name"}, base_dir=self.tmp)
        self.assertEqual(first, "derived_name")
        self.assertEqual(second, "derived_name_2")
        self.assertNotEqual(first, second)

    def test_replay_with_explicit_id_returns_existing_agent(self):
        spec = {"id": "replay_agent", "name": "Replay Agent"}
        first = create_agent(spec, base_dir=self.tmp)
        second = create_agent(spec, base_dir=self.tmp)
        self.assertEqual(first, second)
        # Replay must not re-run the filesystem phase (single SYSTEM.md, no error).
        self.assertTrue(os.path.isfile(os.path.join(self.agent_dir(first), "SYSTEM.md")))

    def test_if_exists_error_raises_for_duplicate_id(self):
        create_agent({"id": "dupe_agent", "name": "Dupe"}, base_dir=self.tmp)
        with self.assertRaises(AgentAlreadyExistsError):
            create_agent({"id": "dupe_agent", "name": "Dupe"}, base_dir=self.tmp, if_exists="error")

    def test_if_exists_return_requires_explicit_id(self):
        with self.assertRaises(SpecValidationError):
            create_agent({"name": "No Id"}, base_dir=self.tmp, if_exists="return")

    def test_invalid_spec_creates_nothing(self):
        with self.assertRaises(SpecValidationError):
            create_agent({"id": "invalid_agent", "is_super": True}, base_dir=self.tmp)
        self.assert_agent_artifacts_removed("invalid_agent")

    def test_concurrent_create_has_exactly_one_winner(self):
        spec = {"id": "race_agent", "name": "Race Agent"}
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            try:
                results.append(("created", create_agent(spec, base_dir=self.tmp, if_exists="error")))
            except AgentAlreadyExistsError:
                results.append(("exists", None))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "worker thread did not finish")

        outcomes = sorted(status for status, _ in results)
        self.assertEqual(outcomes, ["created", "exists"], results)
        row = db.get_agent("race_agent")
        self.assertIsNotNone(row)
        self.assertEqual(set(db.get_agent_tools("race_agent")), set(ARTIFACT_TOOLS) | set(VISION_TOOLS))

    def test_partial_failure_is_compensated_in_reverse(self):
        spec = {
            "id": "cleanup_agent",
            "name": "Cleanup Agent",
            "tools": ["web_search"],
            "skills": ["github"],
            "variables": [{"key": "TOKEN", "value": "cleanup-secret-value", "is_secret": True}],
            "knowledge_base": [{"path": "notes.md", "content": "x"}],
        }
        boom = OSError("default KB copy failed")

        with patch("shutil.copy2", side_effect=boom):
            with self.assertRaises(OSError) as ctx:
                create_agent(spec, base_dir=self.tmp)

        # The original exception is re-raised unchanged.
        self.assertIs(ctx.exception, boom)
        self.assert_agent_artifacts_removed("cleanup_agent")

    def test_failed_create_can_be_retried_with_the_same_id(self):
        spec = {"id": "retry_agent", "name": "Retry Agent"}
        with patch("shutil.copy2", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                create_agent(spec, base_dir=self.tmp)
        agent_id = create_agent(spec, base_dir=self.tmp)
        self.assertEqual(agent_id, "retry_agent")
        self.assertTrue(os.path.isfile(os.path.join(self.agent_dir(agent_id), "SYSTEM.md")))

    def test_secret_value_never_reaches_logs_or_stderr(self):
        secret = "super-secret-token-value-987654321"
        spec = {
            "id": "secret_agent",
            "name": "Secret Agent",
            "variables": [{"key": "API_TOKEN", "value": secret, "is_secret": True}],
        }

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        stderr = io.StringIO()
        try:
            with patch("shutil.copy2", side_effect=OSError("default KB copy failed")):
                with redirect_stderr(stderr):
                    with self.assertRaises(OSError) as ctx:
                        create_agent(spec, base_dir=self.tmp)
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(previous_level)

        logged = stream.getvalue()
        # The failure must actually have been logged, otherwise the test is vacuous.
        self.assertIn("agent_factory: create_agent failed", logged)
        self.assertNotIn(secret, logged)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, repr(ctx.exception))
        self.assert_agent_artifacts_removed("secret_agent")


class ApplySpecTests(AgentFactoryTestBase):
    def test_apply_spec_updates_only_the_provided_fields(self):
        agent_id = create_agent({"id": "apply_agent", "name": "Apply Agent"}, base_dir=self.tmp)
        before = db.get_agent(agent_id)

        apply_spec(agent_id, {
            "enabled": False,
            "summarize_threshold": 11,
            "system_prompt": "Updated system prompt.",
            "tools": ["web_search"],
            "skills": ["github"],
            "variables": [{"key": "PUBLIC", "value": "visible"}],
            "knowledge_base": [{"path": "notes/new.md", "content": "hello"}],
        }, base_dir=self.tmp)

        after = db.get_agent(agent_id)
        self.assertEqual(after["enabled"], 0)
        self.assertEqual(after["summarize_threshold"], 11)
        # Untouched columns keep their previous values.
        self.assertEqual(after["summarize_tail"], before["summarize_tail"])
        self.assertEqual(after["message_buffer_seconds"], before["message_buffer_seconds"])
        self.assertEqual(after["vision_enabled"], before["vision_enabled"])
        self.assertEqual(after["name"], "Apply Agent")

        tools = set(db.get_agent_tools(agent_id))
        self.assertIn("web_search", tools)
        self.assertTrue(ARTIFACT_TOOLS <= tools)
        self.assertTrue(VISION_TOOLS <= tools)
        self.assertEqual(db.get_agent_skills(agent_id), ["github"])
        self.assertEqual(
            {var["key"]: var["value"] for var in db.get_agent_variables(agent_id)},
            {"PUBLIC": "visible"},
        )
        # SYSTEM.md is the runtime source of truth for the prompt (see
        # backend/agent_runtime/context.py), exactly like api_update_agent().
        self.assertEqual(
            _read_text(os.path.join(self.agent_dir(agent_id), "SYSTEM.md")),
            "Updated system prompt.",
        )
        self.assertEqual(
            _read_text(os.path.join(self.kb_dir(agent_id), "notes", "new.md")), "hello")

    def test_apply_spec_strips_managed_tools_when_flag_disabled(self):
        agent_id = create_agent({"id": "lock_agent", "name": "Lock Agent"}, base_dir=self.tmp)
        self.assertTrue(ARTIFACT_TOOLS <= set(db.get_agent_tools(agent_id)))

        apply_spec(agent_id, {"artifacts_enabled": False, "vision_enabled": False}, base_dir=self.tmp)

        tools = set(db.get_agent_tools(agent_id))
        self.assertFalse(tools & ARTIFACT_TOOLS)
        self.assertFalse(tools & VISION_TOOLS)

        apply_spec(agent_id, {"artifacts_enabled": True, "vision_enabled": True}, base_dir=self.tmp)
        tools = set(db.get_agent_tools(agent_id))
        self.assertTrue(ARTIFACT_TOOLS <= tools)
        self.assertTrue(VISION_TOOLS <= tools)

    def test_apply_spec_with_empty_spec_is_a_noop(self):
        agent_id = create_agent({"id": "noop_agent", "name": "Noop Agent"}, base_dir=self.tmp)
        before = db.get_agent(agent_id)
        self.assertEqual(apply_spec(agent_id, {}, base_dir=self.tmp), agent_id)
        after = db.get_agent(agent_id)
        self.assertEqual(before["summarize_threshold"], after["summarize_threshold"])

    def test_apply_spec_missing_agent_raises(self):
        with self.assertRaises(AgentNotFoundError):
            apply_spec("does_not_exist", {"enabled": True}, base_dir=self.tmp)

    def test_apply_spec_mismatched_id_raises(self):
        agent_id = create_agent({"id": "match_agent", "name": "Match Agent"}, base_dir=self.tmp)
        with self.assertRaises(SpecValidationError):
            apply_spec(agent_id, {"id": "other_agent"}, base_dir=self.tmp)

    def test_apply_spec_rejects_unknown_keys_before_writing(self):
        agent_id = create_agent({"id": "reject_agent", "name": "Reject Agent"}, base_dir=self.tmp)
        with self.assertRaises(SpecValidationError):
            apply_spec(agent_id, {"summarize_threshold": 9, "is_super": True}, base_dir=self.tmp)
        self.assertEqual(db.get_agent(agent_id)["summarize_threshold"], DEFAULTS["summarize_threshold"])


class DriftTests(AgentFactoryTestBase):
    """Guard the two intentional duplications against silent drift."""

    def test_update_agent_allowlist_is_a_superset_of_the_spec_surface(self):
        from models.mixins.agents import AgentMixin

        source = ast.parse(
            textwrap.dedent(__import__("inspect").getsource(AgentMixin.update_agent))
        )
        allowed = None
        for node in ast.walk(source):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "allowed"
                for target in node.targets
            ):
                allowed = ast.literal_eval(node.value)
        self.assertIsNotNone(allowed, "could not locate update_agent()'s allowlist")
        missing = set(SPEC_FIELD_ALLOWLIST) - set(allowed)
        self.assertEqual(missing, set(), "update_agent() would silently drop: {}".format(sorted(missing)))

    def test_managed_tool_sets_match_the_routes_module(self):
        with open(os.path.join(REPO_ROOT, "routes", "agents.py"), "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in ("ARTIFACT_TOOLS", "VISION_TOOLS"):
                    # routes/agents.py declares these as frozenset({...}), so collect
                    # the string literals from the assigned expression.
                    found[target.id] = {
                        child.value
                        for child in ast.walk(node.value)
                        if isinstance(child, ast.Constant) and isinstance(child.value, str)
                    }

        self.assertEqual(found.get("ARTIFACT_TOOLS"), set(ARTIFACT_TOOLS))
        self.assertEqual(found.get("VISION_TOOLS"), set(VISION_TOOLS))

    def test_default_kb_sources_match_the_routes_module(self):
        with open(os.path.join(REPO_ROOT, "routes", "agents.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        for target_name, source_name in DEFAULT_KB_FILES:
            self.assertIn(target_name, source, "routes/agents.py no longer copies {}".format(target_name))
            self.assertIn(source_name, source, "routes/agents.py no longer copies {}".format(source_name))


class ModuleContractTests(AgentFactoryTestBase):
    def test_module_does_not_import_flask(self):
        with open(os.path.join(REPO_ROOT, "backend", "agent_factory.py"), "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("flask", imported)


if __name__ == "__main__":
    unittest.main()
