"""Coverage for agent-private learned skills (backend/learned_skills.py)."""
import json
import os
import time

import pytest

from backend import learned_skills
from backend.agent_runtime import context
from backend.tools import bash, runpy, tool_registry, use_skill

AGENT = {"id": "agent-a", "learned_skills_enabled": 1}


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    """Point every agent's learned-skills directory at tmp_path/<agent_id>."""
    monkeypatch.setattr(
        learned_skills, "skills_dir",
        lambda agent: str(tmp_path / learned_skills.effective_agent_id(agent)))
    return tmp_path


def _write_skill(root, agent_id, skill_id, *, system_md="Do the thing.", tools=None,
                 scripts=None, manifest=None):
    skill_dir = root / agent_id / skill_id
    (skill_dir / "scripts").mkdir(parents=True)
    data = {"id": skill_id, "name": skill_id, "description": "desc", "brief": f"Load {skill_id}."}
    if tools is not None:
        data["tools_file"] = "tools.json"
        (skill_dir / "tools.json").write_text(json.dumps(
            [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
             for name in tools]))
    data.update(manifest or {})
    (skill_dir / "skill.json").write_text(json.dumps(data))
    if system_md is not None:
        (skill_dir / "SYSTEM.md").write_text(system_md)
    for name, body in (scripts or {}).items():
        (skill_dir / "scripts" / name).write_text(body)
    return skill_dir


def test_valid_instruction_only_skill_is_listed(skills_root):
    _write_skill(skills_root, "agent-a", "make-pdf")
    assert learned_skills.validate(AGENT, "make-pdf") is None
    assert [s["id"] for s in learned_skills.list_skills(AGENT)] == ["make-pdf"]


@pytest.mark.parametrize("kwargs, expected", [
    ({"manifest": {"id": "other"}}, '"id"'),
    ({"tools": ["create_pdf"]}, "has no script"),
    ({"tools": ["bash"], "scripts": {"bash.sh": "echo"}}, "taken by a core tool"),
    ({"system_md": None}, "needs a SYSTEM.md"),
])
def test_invalid_skills_are_reported_and_hidden(skills_root, kwargs, expected):
    _write_skill(skills_root, "agent-a", "make-pdf", **kwargs)
    assert expected in learned_skills.validate(AGENT, "make-pdf")
    assert learned_skills.list_skills(AGENT) == []
    assert learned_skills.list_skills(AGENT, include_invalid=True)[0]["error"]


def test_bad_id_and_global_collision_are_rejected(skills_root):
    assert "Invalid skill id" in learned_skills.validate(AGENT, "../etc")
    # 'github' is a global skill shipped in skills/
    _write_skill(skills_root, "agent-a", "github")
    assert "collides with a global skill" in learned_skills.validate(AGENT, "github")
    assert not learned_skills.is_learned(AGENT, "github")


def test_use_skill_loads_only_for_owner_with_feature_on(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "echo hi"})
    monkeypatch.setattr(use_skill.skills_manager, "get_skill", lambda skill_id: None)
    monkeypatch.setattr(use_skill.skills_manager, "list_skills", lambda: [])

    result = use_skill.execute(dict(AGENT), {"id": "make-pdf"})
    assert result["status"] == "success"
    assert result["system_md"] == "Do the thing."
    assert result["inject_tools"][0]["function"]["name"] == "create_pdf"

    other = use_skill.execute({"id": "agent-b", "learned_skills_enabled": 1}, {"id": "make-pdf"})
    assert other["status"] == "error"
    off = use_skill.execute({"id": "agent-a", "learned_skills_enabled": 0}, {"id": "make-pdf"})
    assert off["status"] == "error"


def test_use_skill_explains_a_broken_learned_skill(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"])
    monkeypatch.setattr(use_skill.skills_manager, "get_skill", lambda skill_id: None)
    result = use_skill.execute(dict(AGENT), {"id": "make-pdf"})
    assert result["status"] == "error"
    assert "has no script" in result["message"]
    assert "/_self/skills/make-pdf/" in result["message"]


def test_sub_agent_uses_parent_learned_skills(skills_root):
    _write_skill(skills_root, "agent-a", "make-pdf")
    sub = {"id": "agent-a_sub1", "is_subagent": True, "parent_id": "agent-a",
           "learned_skills_enabled": 1}
    assert [s["id"] for s in learned_skills.list_skills(sub)] == ["make-pdf"]


def test_run_tool_passes_arguments_as_env_to_bash(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "pandoc \"$ARG_SOURCE\""})
    calls = []
    monkeypatch.setattr(bash, "execute", lambda agent, args: calls.append(args) or {"exit_code": 0})
    agent = {**AGENT, "assigned_tool_ids": ["bash"]}

    result = learned_skills.run_tool(agent, "make-pdf", "create_pdf", {"source": "a.md", "pages": 2})

    assert result == {"exit_code": 0}
    assert calls[0]["script"] == "pandoc \"$ARG_SOURCE\""
    assert calls[0]["env"]["ARG_SOURCE"] == "a.md"
    assert calls[0]["env"]["ARG_PAGES"] == "2"
    assert json.loads(calls[0]["env"]["SKILL_ARGS"]) == {"source": "a.md", "pages": 2}


def test_run_tool_routes_python_scripts_to_runpy(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.py": "print(1)"})
    calls = []
    monkeypatch.setattr(runpy, "execute", lambda agent, args: calls.append(args) or {"exit_code": 0})
    learned_skills.run_tool({**AGENT, "assigned_tool_ids": ["runpy"]}, "make-pdf", "create_pdf", {})
    assert calls[0]["code"] == "print(1)"


def test_run_tool_refused_without_the_runner_tool(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "echo hi"})
    monkeypatch.setattr(bash, "execute", lambda agent, args: pytest.fail("must not run"))
    result = learned_skills.run_tool({**AGENT, "assigned_tool_ids": ["read_file"]},
                                     "make-pdf", "create_pdf", {})
    assert "not assigned" in result["error"]


def test_executor_routes_learned_tool_to_script(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "echo hi"})
    calls = []
    monkeypatch.setattr(bash, "execute", lambda agent, args: calls.append(args) or {"exit_code": 0})
    monkeypatch.setattr(tool_registry, "_load_tool_module",
                        lambda *a, **k: pytest.fail("learned tool must not load a module"))
    executor = tool_registry.get_real_executor({
        "id": "agent-a", "assigned_tool_ids": ["bash", "skill:make-pdf:create_pdf"]})

    assert executor("create_pdf", {"source": "a.md"}) == {"exit_code": 0}
    assert calls[0]["env"]["ARG_SOURCE"] == "a.md"


def test_executor_routes_a_learned_tool_loaded_mid_turn(skills_root, monkeypatch):
    """use_skill adds the tool id after the executor was built."""
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "echo hi"})
    monkeypatch.setattr(bash, "execute", lambda agent, args: {"exit_code": 0})
    agent_context = {"id": "agent-a", "assigned_tool_ids": ["bash"]}
    executor = tool_registry.get_real_executor(agent_context)
    agent_context["assigned_tool_ids"].append("skill:make-pdf:create_pdf")

    assert executor("create_pdf", {}) == {"exit_code": 0}


def test_export_then_import_into_another_agent(skills_root, tmp_path):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "echo hi"})
    zip_path = str(tmp_path / "make-pdf.zip")
    assert learned_skills.export_zip(AGENT, "make-pdf", zip_path)

    other = {"id": "agent-b"}
    assert learned_skills.import_zip(other, zip_path) == {"id": "make-pdf"}
    assert learned_skills.validate(other, "make-pdf") is None
    assert os.path.isfile(skills_root / "agent-b" / "make-pdf" / "scripts" / "create_pdf.sh")

    again = learned_skills.import_zip(other, zip_path)
    assert again["exists"] is True
    assert learned_skills.import_zip(other, zip_path, force=True) == {"id": "make-pdf"}


def test_import_of_invalid_skill_keeps_the_existing_one(skills_root, tmp_path):
    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"])  # no script: invalid
    zip_path = str(tmp_path / "bad.zip")
    learned_skills.export_zip(AGENT, "make-pdf", zip_path)
    _write_skill(skills_root, "agent-b", "make-pdf", system_md="original")

    result = learned_skills.import_zip({"id": "agent-b"}, zip_path, force=True)

    assert "has no script" in result["error"]
    assert (skills_root / "agent-b" / "make-pdf" / "SYSTEM.md").read_text() == "original"


def _static_prompt(monkeypatch, agent):
    monkeypatch.setattr(context.skills_manager, "list_skills", lambda: [])
    monkeypatch.setattr(context.db, "get_agent_skills", lambda agent_id: [])
    monkeypatch.setattr(context.db, "get_agent_tools", lambda agent_id: ["write_file"])
    monkeypatch.setattr(context.db, "get_setting", lambda key, default=None: None)
    monkeypatch.setattr(context.db, "get_active_dimensions", lambda agent_id, limit: [])
    monkeypatch.setattr(context.db, "get_agent_variables", lambda agent_id: [])
    monkeypatch.setattr(context, "_build_portal_info", lambda agent_id: [])
    return context._build_static_prompt(agent)


def test_prompt_lists_learned_skills_only_when_enabled(skills_root, monkeypatch):
    _write_skill(skills_root, "agent-a", "make-pdf")
    on = _static_prompt(monkeypatch, {"id": "agent-a", "system_prompt": "", "learned_skills_enabled": 1})
    assert "## Learned Skills" in on
    assert "- `make-pdf`: Load make-pdf." in on
    assert "/_self/skills/<id>/" in on

    off = _static_prompt(monkeypatch, {"id": "agent-a", "system_prompt": "", "learned_skills_enabled": 0})
    assert "Learned Skills" not in off


def test_prompt_cache_key_changes_when_a_skill_changes(skills_root):
    agent = {"id": "agent-a", "learned_skills_enabled": 1}
    empty = context._learned_skills_hash(agent)
    skill_dir = _write_skill(skills_root, "agent-a", "make-pdf")
    created = context._learned_skills_hash(agent)
    assert created != empty

    future = time.time() + 5
    os.utime(skill_dir / "SYSTEM.md", (future, future))
    assert context._learned_skills_hash(agent) != created
    assert context._learned_skills_hash({"id": "agent-a", "learned_skills_enabled": 0}) == ""


def test_api_lists_exports_imports_and_deletes(skills_root, monkeypatch):
    import io

    from app import app
    from models.db import db

    _write_skill(skills_root, "agent-a", "make-pdf", tools=["create_pdf"],
                 scripts={"create_pdf.sh": "echo hi"})
    monkeypatch.setattr(db, "get_agent", lambda agent_id: {"id": agent_id})
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True

    listed = client.get("/api/agents/agent-a/learned-skills").get_json()["skills"]
    assert [(s["id"], s["tools"]) for s in listed] == [("make-pdf", ["create_pdf"])]
    detail = client.get("/api/agents/agent-a/learned-skills/make-pdf").get_json()["skill"]
    assert detail["system_md"] == "Do the thing."

    exported = client.get("/api/agents/agent-a/learned-skills/make-pdf/export")
    assert exported.status_code == 200 and exported.mimetype == "application/zip"

    def upload(**extra):
        return client.post("/api/agents/agent-b/learned-skills/import", data={
            "file": (io.BytesIO(exported.data), "make-pdf.zip"), **extra})

    assert upload().get_json() == {"id": "make-pdf"}
    assert upload().status_code == 409
    assert upload(force="1").status_code == 200

    assert client.delete("/api/agents/agent-b/learned-skills/make-pdf").get_json() == {"success": True}
    assert client.get("/api/agents/agent-b/learned-skills").get_json()["skills"] == []
    assert client.delete("/api/agents/agent-b/learned-skills/make-pdf").status_code == 404
