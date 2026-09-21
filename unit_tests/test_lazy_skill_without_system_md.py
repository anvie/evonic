"""Regression coverage for tool-only lazy skills without SYSTEM.md."""

from backend.agent_runtime import context
from backend.tools import use_skill


def test_static_prompt_lists_allowed_lazy_skill_without_system_md(monkeypatch):
    """An enabled, assigned tool-only lazy skill remains discoverable."""
    tool_only_skill = {
        "id": "image-generator",
        "lazy_tools": True,
        "brief": "Load image-generator to create images.",
    }
    eager_skill = {"id": "eager-skill", "lazy_tools": False}
    disabled_skill = {"id": "disabled-skill", "lazy_tools": True}
    unassigned_skill = {"id": "unassigned-skill", "lazy_tools": True}

    monkeypatch.setattr(context.skills_manager, "list_skills", lambda: [
        tool_only_skill, eager_skill, disabled_skill, unassigned_skill,
    ])
    monkeypatch.setattr(
        context.skills_manager,
        "is_skill_enabled",
        lambda skill_id: skill_id != "disabled-skill",
    )
    monkeypatch.setattr(context.db, "get_agent_skills", lambda agent_id: ["image-generator"])
    monkeypatch.setattr(context.db, "get_agent_tools", lambda agent_id: [])
    monkeypatch.setattr(context.db, "get_setting", lambda key, default=None: None)
    monkeypatch.setattr(context.db, "get_active_dimensions", lambda agent_id, limit: [])
    monkeypatch.setattr(context.db, "get_agent_variables", lambda agent_id: [])
    monkeypatch.setattr(context, "_build_portal_info", lambda agent_id: [])

    prompt = context._build_static_prompt({"id": "test-agent", "system_prompt": ""})

    assert "- `image-generator`" in prompt
    assert "Load image-generator to create images." in prompt
    assert "eager-skill" not in prompt
    assert "disabled-skill" not in prompt
    assert "unassigned-skill" not in prompt


def test_use_skill_injects_tools_without_system_md(monkeypatch, tmp_path):
    """A tool-only lazy skill succeeds and returns its tool definitions."""
    skill_dir = tmp_path / "image-generator"
    skill_dir.mkdir()
    (skill_dir / "skill.json").write_text('{"id":"image-generator","lazy_tools":true}')
    tool_defs = [{"type": "function", "function": {"name": "generate_image"}}]

    monkeypatch.setattr(
        use_skill.skills_manager,
        "get_skill",
        lambda skill_id: {"id": skill_id, "_dir": str(skill_dir)},
    )
    monkeypatch.setattr(use_skill.skills_manager, "is_skill_enabled", lambda skill_id: True)
    monkeypatch.setattr(use_skill.skills_manager, "get_skill_tool_defs", lambda skill_id: tool_defs)

    result = use_skill.execute({"id": "super", "is_super": True}, {"id": "image-generator"})

    assert result["status"] == "success"
    assert result["inject_tools"] == tool_defs
    assert "system_md" not in result
    assert "Tool definitions have been injected" in result["message"]
