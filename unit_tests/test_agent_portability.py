import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from flask import Flask

from backend.agent_portability import (
    AgentPortabilityError,
    export_agent,
    import_agent,
    preflight_import,
    validate_payload,
)


def payload(**agent_overrides):
    agent = {
        "name": "Portable Agent",
        "description": "Description",
        "system_prompt": "Prompt",
        "configuration": {"enabled": 1, "tool_compression_enabled": 0},
        "tools": [],
        "skills": [],
        "variables": [{"key": "PUBLIC", "value": "visible"}],
        "knowledge_base": [{"path": "guide/start.md", "content": "Hello"}],
    }
    agent.update(agent_overrides)
    return {
        "schema": "evonic.agent",
        "version": 1,
        "metadata": {"omitted_secret_variable_keys": ["TOKEN"]},
        "agent": agent,
    }


class FakeDB:
    def __init__(self):
        self.agents = {}
        self.tools = {}
        self.skills = {}
        self.variables = {}
        self.deleted = []

    def get_agent(self, agent_id):
        return self.agents.get(agent_id)

    def create_agent(self, data):
        self.agents[data["id"]] = dict(data)

    def update_agent(self, agent_id, data):
        self.agents[agent_id].update(data)
        return True

    def delete_agent(self, agent_id):
        self.deleted.append(agent_id)
        return self.agents.pop(agent_id, None) is not None

    def get_agent_tools(self, agent_id):
        return self.tools.get(agent_id, [])

    def set_agent_tools(self, agent_id, values):
        self.tools[agent_id] = list(values)

    def get_agent_skills(self, agent_id):
        return self.skills.get(agent_id, [])

    def set_agent_skills(self, agent_id, values):
        self.skills[agent_id] = list(values)

    def get_agent_variables(self, agent_id):
        return self.variables.get(agent_id, [])

    def set_agent_variables_bulk(self, agent_id, values):
        self.variables[agent_id] = list(values)

    def get_tools(self):
        return []


class AgentPortabilityServiceTests(unittest.TestCase):
    def test_export_excludes_secret_values_and_uses_prompt_file(self):
        db = FakeDB()
        db.agents["source"] = {
            "id": "source", "name": "Source", "description": "D",
            "system_prompt": "stale", "workspace": "/secret/path", "is_super": 1,
            "model_id": "model", "enabled": 1,
        }
        db.variables["source"] = [
            {"key": "TOKEN", "value": "top-secret", "is_secret": 1},
            {"key": "PUBLIC", "value": "ok", "is_secret": 0},
        ]
        with tempfile.TemporaryDirectory() as root:
            agent_dir = os.path.join(root, "agents", "source")
            os.makedirs(os.path.join(agent_dir, "kb"))
            with open(os.path.join(agent_dir, "SYSTEM.md"), "w", encoding="utf-8") as handle:
                handle.write("authoritative")
            exported = export_agent(db, "source", root)
        encoded = json.dumps(exported)
        self.assertNotIn("top-secret", encoded)
        self.assertNotIn("/secret/path", encoded)
        self.assertNotIn('"is_super"', encoded)
        self.assertEqual(exported["agent"]["system_prompt"], "authoritative")
        self.assertEqual(exported["metadata"]["omitted_secret_variable_keys"], ["TOKEN"])

    def test_export_skips_hidden_runtime_kb_entries(self):
        db = FakeDB()
        db.agents["source"] = {
            "id": "source", "name": "Source", "description": "",
            "system_prompt": "", "enabled": 1,
        }
        with tempfile.TemporaryDirectory() as root:
            kb = os.path.join(root, "agents", "source", "kb")
            os.makedirs(os.path.join(kb, ".scratch"))
            for path, content in (
                (os.path.join(kb, "visible.md"), "visible"),
                (os.path.join(kb, ".evomem.db"), "runtime"),
                (os.path.join(kb, ".gitignore"), "ignored"),
                (os.path.join(kb, ".scratch", "draft.md"), "draft"),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
            exported = export_agent(db, "source", root)
        self.assertEqual(exported["agent"]["knowledge_base"], [
            {"path": "visible.md", "content": "visible"},
        ])

    def test_export_rejects_symlinked_knowledge_base_file(self):
        db = FakeDB()
        db.agents["source"] = {
            "id": "source", "name": "Source", "description": "",
            "system_prompt": "", "enabled": 1,
        }
        with tempfile.TemporaryDirectory() as root:
            kb = os.path.join(root, "agents", "source", "kb")
            os.makedirs(kb)
            outside = os.path.join(root, "outside.md")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("secret")
            os.symlink(outside, os.path.join(kb, "linked.md"))
            with self.assertRaisesRegex(AgentPortabilityError, "symbolic link"):
                export_agent(db, "source", root)

    def test_validation_rejects_unsafe_and_duplicate_kb_paths(self):
        for path in ("../escape.md", "/absolute.md", "bad\\path.md"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(AgentPortabilityError, "Knowledge-base path"):
                    validate_payload(payload(knowledge_base=[{"path": path, "content": "x"}]))
        with self.assertRaisesRegex(AgentPortabilityError, "Duplicate knowledge-base path"):
            validate_payload(payload(knowledge_base=[
                {"path": "a/../same.md", "content": "x"},
                {"path": "same.md", "content": "y"},
            ]))

    def test_validation_rejects_unknown_fields_and_duplicate_variables(self):
        bad = payload(configuration={"workspace": "/tmp"})
        with self.assertRaisesRegex(AgentPortabilityError, "unsupported keys"):
            validate_payload(bad)
        with self.assertRaisesRegex(AgentPortabilityError, "Duplicate variable key"):
            validate_payload(payload(variables=[
                {"key": "A", "value": "1"}, {"key": "A", "value": "2"},
            ]))
        with self.assertRaisesRegex(AgentPortabilityError, "JSON scalars"):
            validate_payload(payload(configuration={"enabled": []}))
        with self.assertRaisesRegex(AgentPortabilityError, "invalid identifier"):
            validate_payload(payload(tools=["bad identifier"]))
        with self.assertRaisesRegex(AgentPortabilityError, "Invalid variable key"):
            validate_payload(payload(variables=[{"key": "BAD-KEY", "value": "x"}]))

    @patch("backend.agent_portability._validate_dependencies")
    def test_import_rejects_non_lower_snake_case_id(self, dependencies):
        with self.assertRaisesRegex(AgentPortabilityError, "lowercase letters"):
            import_agent(FakeDB(), payload(), "/tmp/unused", agent_id="Invalid-Agent")

    @patch("backend.agent_portability._validate_dependencies")
    def test_import_rejects_reserved_subagent_id(self, dependencies):
        with self.assertRaisesRegex(AgentPortabilityError, "reserved sub-agent"):
            import_agent(FakeDB(), payload(), "/tmp/unused", agent_id="agent_sub_1")

    @patch("backend.agent_portability._validate_dependencies")
    def test_import_creates_regular_agent_and_files(self, dependencies):
        db = FakeDB()
        with tempfile.TemporaryDirectory() as root:
            imported = import_agent(db, payload(), root, agent_id="imported", name="Override")
            self.assertEqual(imported, "imported")
            self.assertFalse(db.agents["imported"]["is_super"])
            self.assertEqual(db.agents["imported"]["name"], "Override")
            self.assertEqual(db.agents["imported"]["tool_compression_enabled"], 0)
            with open(os.path.join(root, "agents", "imported", "kb", "guide", "start.md"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "Hello")
            self.assertTrue(os.path.isdir(os.path.join(root, "shared", "agents", "imported")))
        dependencies.assert_called_once()

    @patch("backend.agent_portability._validate_dependencies")
    def test_import_generates_unique_id_when_omitted(self, dependencies):
        db = FakeDB()
        db.agents["portable_agent"] = {"id": "portable_agent"}
        with tempfile.TemporaryDirectory() as root:
            imported = import_agent(db, payload(), root)
            self.assertEqual(imported, "portable_agent_2")
            self.assertIn("portable_agent_2", db.agents)

    @patch("backend.agent_portability._validate_dependencies")
    def test_duplicate_and_failure_rollback(self, dependencies):
        db = FakeDB()
        db.agents["taken"] = {"id": "taken"}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(AgentPortabilityError, "already exists"):
                import_agent(db, payload(), root, agent_id="taken")
            db.agents.clear()
            db.set_agent_variables_bulk = MagicMock(side_effect=RuntimeError("write failed"))
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                import_agent(db, payload(), root, agent_id="failed")
            self.assertNotIn("failed", db.agents)
            self.assertIn("failed", db.deleted)
            self.assertFalse(os.path.exists(os.path.join(root, "agents", "failed")))
            self.assertFalse(os.path.exists(os.path.join(root, "shared", "agents", "failed")))

    @patch("backend.agent_portability._available_tools", return_value={"known"})
    @patch("backend.skills_manager.skills_manager.list_skills", return_value=[{"id": "installed"}])
    def test_missing_dependencies_fail_before_creation(self, skills, tools):
        db = FakeDB()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(AgentPortabilityError, "missing"):
                import_agent(db, payload(tools=["missing"]), root, agent_id="new_agent")
            self.assertFalse(db.agents)

    @patch("backend.agent_portability._available_tools", return_value=set())
    @patch("backend.agent_portability._skill_tool_ids", return_value={"skill:scheduler:create_schedule"})
    @patch("backend.skills_manager.skills_manager.list_skills", return_value=[{"id": "scheduler", "enabled": True}])
    def test_preflight_accepts_installed_lazy_skill_tool(self, skills, tool_ids, tools):
        result = preflight_import(FakeDB(), payload(
            skills=["scheduler"], tools=["skill:scheduler:create_schedule"]))
        self.assertEqual(result["warning"], {"skills": [], "tools": []})
        self.assertEqual(result["payload"]["agent"]["tools"], ["skill:scheduler:create_schedule"])

    @patch("backend.agent_portability._available_tools", return_value=set())
    @patch("backend.skills_manager.skills_manager.list_skills", return_value=[])
    def test_preflight_warns_and_omits_unavailable_skill(self, skills, tools):
        result = preflight_import(FakeDB(), payload(
            skills=["missing_skill"], tools=["skill:missing_skill:run"]))
        self.assertEqual(result["warning"], {
            "skills": ["missing_skill"], "tools": ["skill:missing_skill:run"]})
        self.assertEqual(result["payload"]["agent"]["skills"], [])
        self.assertEqual(result["payload"]["agent"]["tools"], [])

    @patch("backend.agent_portability._available_tools", return_value=set())
    @patch("backend.skills_manager.skills_manager.list_skills", return_value=[])
    def test_confirmed_import_skips_unavailable_skill(self, skills, tools):
        db = FakeDB()
        with tempfile.TemporaryDirectory() as root:
            imported = import_agent(db, payload(
                skills=["missing_skill"], tools=["skill:missing_skill:run"]),
                root, agent_id="portable", confirm_missing=True)
        self.assertEqual(imported, "portable")
        self.assertEqual(db.skills["portable"], [])
        self.assertEqual(db.tools["portable"], [])


class AgentPortabilityRouteTests(unittest.TestCase):
    def setUp(self):
        from routes.agents import agents_bp
        self.app = Flask(__name__)
        self.app.register_blueprint(agents_bp)
        self.client = self.app.test_client()

    @patch("routes.agents.export_agent")
    @patch("routes.agents.db")
    def test_export_endpoint_downloads_json(self, db, exporter):
        db.get_agent.return_value = {"id": "source"}
        exporter.return_value = payload()
        response = self.client.get("/api/agents/source/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("source.agent.json", response.headers["Content-Disposition"])
        self.assertEqual(response.get_json()["schema"], "evonic.agent")

    @patch("routes.agents.audit.log_agent_crud")
    @patch("routes.agents.import_agent", return_value="created")
    def test_import_endpoint_accepts_wrapped_payload(self, importer, audit):
        response = self.client.post("/api/agents/import", json={
            "payload": payload(), "id": "created", "name": "Created",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["agent_id"], "created")
        importer.assert_called_once()

    @patch("routes.agents.preflight_import", return_value={"payload": payload(), "warning": {"skills": ["missing_skill"], "tools": ["skill:missing_skill:run"]}})
    @patch("routes.agents.import_agent")
    def test_import_endpoint_warns_before_creation(self, importer, preflight):
        response = self.client.post("/api/agents/import", json={"payload": payload()})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["warning"]["skills"], ["missing_skill"])
        importer.assert_not_called()


class AgentPortabilityCliTests(unittest.TestCase):
    @patch("backend.agent_portability.export_agent", return_value=payload())
    @patch("cli.commands._get_db")
    def test_cli_export_writes_file(self, get_db, exporter):
        from cli.commands import agent_export
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "agent.json")
            with redirect_stdout(io.StringIO()):
                agent_export("source", output)
            with open(output, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["schema"], "evonic.agent")

    @patch("backend.agent_portability.import_agent", return_value="stdin_agent")
    @patch("cli.commands._get_db")
    def test_cli_import_reads_stdin(self, get_db, importer):
        from cli.commands import agent_import
        stdin = io.StringIO(json.dumps(payload()))
        with patch("sys.stdin", stdin), redirect_stdout(io.StringIO()):
            agent_import("-", agent_id="stdin_agent", name="Stdin")
        self.assertEqual(importer.call_args.kwargs["agent_id"], "stdin_agent")

    def test_cli_import_reports_invalid_json(self):
        from cli.commands import agent_import
        with patch("sys.stdin", io.StringIO("{")), redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit):
                agent_import("-", agent_id="bad")
        self.assertIn("Invalid JSON", error.getvalue())


if __name__ == "__main__":
    unittest.main()
