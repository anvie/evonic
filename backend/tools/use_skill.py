"""
Builtin tool: use_skill

Lazy-load a skill's SYSTEM.md knowledge into the agent context.
For skills with lazy_tools=true, also returns their tool definitions so the
runtime can inject them into the LLM tool list mid-turn.

Usage: use_skill({id: "hello_world"})
"""

import os
import json
from backend.skills_manager import skills_manager


def _enabled_skill_ids() -> list:
    """Return list of enabled skill IDs."""
    return [s['id'] for s in skills_manager.list_skills() if skills_manager.is_skill_enabled(s['id'])]


def execute(agent: dict, args: dict) -> dict:
    """
    Load and return the SYSTEM.md content of a skill.

    Args:
        agent: Agent context dict. Must contain 'is_super' for super_only skills.
        args: Must contain 'id' — the ID of the skill to load.

    Returns:
        dict with 'id', 'system_md' (content), and status.
        For lazy_tools skills, also includes 'inject_tools' with tool definitions.
    """
    skill_id = args.get("id", "").strip()

    if not skill_id:
        return {
            "status": "error",
            "id": skill_id,
            "message": "id is required. Example: use_skill({id: 'hello_world'})"
        }

    # Check if the skill exists
    skill = skills_manager.get_skill(skill_id)
    if not skill:
        available = _enabled_skill_ids()
        return {
            "status": "error",
            "id": skill_id,
            "message": f"Skill '{skill_id}' not found.",
            "available_skills": available
        }

    # Check if the skill is enabled (DB-authoritative whitelist)
    if not skills_manager.is_skill_enabled(skill_id):
        available = _enabled_skill_ids()
        return {
            "status": "error",
            "id": skill_id,
            "message": f"Skill '{skill_id}' is disabled.",
            "available_skills": available
        }

    # Per-agent allowlist check (only the designated super agent is exempt)
    _super_id = None
    try:
        from models.db import db
        _super_id = db.get_setting('super_agent_id')
    except Exception:
        pass
    if not (agent.get('is_super') and agent.get('id') == _super_id):
        from models.db import db
        # Sub-agents inherit parent's skill assignments
        _eid = agent.get('parent_id', agent.get('id', '')) if agent.get('is_subagent') else agent.get('id', '')
        allowed = db.get_agent_skills(_eid)
        if skill_id not in allowed:
            available = _enabled_skill_ids()
            allowed_available = [s for s in available if s in allowed]
            return {
                "status": "error",
                "id": skill_id,
                "message": f"Skill '{skill_id}' is not in your allowed skills list.",
                "available_skills": allowed_available
            }

    # Enforce super_only restriction
    skill_dir = skill.get('_dir', os.path.join(
        os.path.dirname(__file__), '..', '..', 'skills', skill_id
    ))
    manifest_path = os.path.join(os.path.normpath(skill_dir), 'skill.json')
    manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    if manifest.get('super_only', False) and not agent.get('is_super'):
        return {
            "status": "error",
            "id": skill_id,
            "message": f"Skill '{skill_id}' is restricted to super agents only."
        }

    # Eager skills have their tools loaded at startup — use_skill is only for lazy skills
    if not manifest.get('lazy_tools', False):
        return {
            "status": "error",
            "id": skill_id,
            "message": (
                f"Skill '{skill_id}' is eagerly loaded — its tools are already available. "
                f"You don't need to call use_skill for it. Just use the tools directly."
            )
        }

    # Build path to SYSTEM.md
    skill_dir_norm = os.path.normpath(skill_dir)
    system_md_path = os.path.join(skill_dir_norm, "SYSTEM.md")

    if not os.path.isfile(system_md_path):
        return {
            "status": "error",
            "id": skill_id,
            "message": f"No SYSTEM.md found in skill '{skill_id}' at {system_md_path}"
        }

    try:
        with open(system_md_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {
            "status": "error",
            "id": skill_id,
            "message": f"Failed to read SYSTEM.md: {str(e)}"
        }

    result = {
        "status": "success",
        "id": skill_id,
        "system_md": content,
        "message": f"Loaded skill knowledge for '{skill_id}'. This content is now in your context — use it to guide your actions."
    }

    # For lazy_tools skills, include tool definitions for runtime injection
    if manifest.get('lazy_tools', False):
        tool_defs = skills_manager.get_skill_tool_defs(skill_id)
        if tool_defs:
            result['inject_tools'] = tool_defs
            result['message'] += f" Tool definitions have been injected ({len(tool_defs)} tool(s) now available)."

    return result
