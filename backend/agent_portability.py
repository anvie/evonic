"""Portable, versioned agent export and import support."""

import os
import re
import shutil
import copy
from typing import Any, Dict, List, Optional

SCHEMA = "evonic.agent"
VERSION = 1
ID_RE = re.compile(r"^[a-z0-9_]+$")
SUBAGENT_ID_RE = re.compile(r"_sub_\d+$")
DEPENDENCY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
VARIABLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_TEXT_LENGTH = 102400
MAX_KB_FILE_LENGTH = 1024 * 1024
CONFIG_KEYS = frozenset({
    "enabled", "vision_enabled", "summarize_threshold", "summarize_tail",
    "summarize_prompt", "message_buffer_seconds", "inject_agent_id",
    "inject_datetime", "send_intermediate_responses", "outbound_buffer_seconds",
    "enable_agent_state", "sandbox_enabled", "attachments_enabled",
    "attachment_max_size_mb", "artifacts_enabled", "safety_checker_enabled",
    "disable_parallel_tool_execution", "disable_turn_prefetch",
    "agent_messaging_enabled", "tool_compression_enabled", "message_wrapper_enabled",
    "fallback_model_id", "model_id", "audio_enabled", "video_enabled", "run_as_user",
    "bash_exec_enabled", "vision_model_id", "inter_agent_clear_context",
    "builtin_tools_enabled", "messaging_acl", "messaging_acl_mode", "memory_engine",
    "kb_organizer_mode", "enable_atg", "enable_cmp", "always_execute",
})


class AgentPortabilityError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AgentPortabilityError(message)


def _safe_kb_path(path: Any) -> str:
    _require(isinstance(path, str) and path.strip(), "Knowledge-base path must be a non-empty string.")
    _require("\\" not in path and not os.path.isabs(path), "Knowledge-base path must be relative and use forward slashes.")
    normalized = os.path.normpath(path).replace(os.sep, "/")
    _require(normalized not in (".", "..") and not normalized.startswith("../"), "Knowledge-base path cannot escape the knowledge-base directory.")
    return normalized


def _agent_dir(base_dir: str, agent_id: str) -> str:
    return os.path.join(base_dir, "agents", agent_id)


def _kb_dir(base_dir: str, agent_id: str) -> str:
    return os.path.join(_agent_dir(base_dir, agent_id), "kb")


def _read_kb(base_dir: str, agent_id: str) -> List[Dict[str, str]]:
    root = _kb_dir(base_dir, agent_id)
    files = []
    if not os.path.isdir(root):
        return files
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for dirname in dirnames:
            if os.path.islink(os.path.join(dirpath, dirname)):
                raise AgentPortabilityError("Knowledge-base directories cannot be symbolic links.")
        for name in sorted(name for name in names if not name.startswith(".")):
            absolute = os.path.join(dirpath, name)
            relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            if os.path.islink(absolute):
                raise AgentPortabilityError(f"Knowledge-base file '{relative}' cannot be a symbolic link.")
            try:
                with open(absolute, "r", encoding="utf-8") as handle:
                    files.append({"path": relative, "content": handle.read()})
            except UnicodeDecodeError:
                raise AgentPortabilityError(f"Knowledge-base file '{relative}' is not UTF-8 text.")
    return files


def export_agent(db, agent_id: str, base_dir: str) -> Dict[str, Any]:
    agent = db.get_agent(agent_id)
    if not agent:
        raise AgentPortabilityError(f"Agent '{agent_id}' was not found.")
    configuration = {key: agent[key] for key in CONFIG_KEYS if key in agent and agent[key] is not None}
    variables, omitted = [], []
    for variable in db.get_agent_variables(agent_id):
        if variable.get("is_secret"):
            omitted.append(variable["key"])
        else:
            variables.append({"key": variable["key"], "value": variable.get("value", "")})
    system_prompt = agent.get("system_prompt", "")
    prompt_path = os.path.join(_agent_dir(base_dir, agent_id), "SYSTEM.md")
    if os.path.islink(prompt_path):
        raise AgentPortabilityError("SYSTEM.md cannot be a symbolic link.")
    if os.path.isfile(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as handle:
            system_prompt = handle.read()
    payload = {
        "schema": SCHEMA,
        "version": VERSION,
        "metadata": {"omitted_secret_variable_keys": sorted(omitted)},
        "agent": {
            "name": agent.get("name", agent_id),
            "description": agent.get("description", ""),
            "system_prompt": system_prompt,
            "configuration": configuration,
            "tools": sorted(db.get_agent_tools(agent_id)),
            "skills": sorted(db.get_agent_skills(agent_id)),
            "variables": variables,
            "knowledge_base": _read_kb(base_dir, agent_id),
        },
    }
    return validate_payload(payload)


def validate_payload(payload: Any) -> Dict[str, Any]:
    _require(isinstance(payload, dict), "Import data must be a JSON object.")
    _require(set(payload) == {"schema", "version", "metadata", "agent"}, "Import data must contain only schema, version, metadata, and agent.")
    _require(payload["schema"] == SCHEMA, f"Unsupported schema. Expected '{SCHEMA}'.")
    _require(payload["version"] == VERSION, f"Unsupported schema version: {payload['version']}.")
    metadata, agent = payload["metadata"], payload["agent"]
    _require(isinstance(metadata, dict) and set(metadata) == {"omitted_secret_variable_keys"}, "metadata must contain omitted_secret_variable_keys.")
    omitted = metadata["omitted_secret_variable_keys"]
    _require(isinstance(omitted, list) and all(isinstance(key, str) and key for key in omitted), "metadata.omitted_secret_variable_keys must be a list of variable keys.")
    _require(all(VARIABLE_KEY_RE.fullmatch(key) for key in omitted), "metadata.omitted_secret_variable_keys contains an invalid variable key.")
    _require(len(omitted) == len(set(omitted)), "metadata.omitted_secret_variable_keys contains duplicate keys.")
    _require(isinstance(agent, dict) and set(agent) == {"name", "description", "system_prompt", "configuration", "tools", "skills", "variables", "knowledge_base"}, "agent has unsupported or missing fields.")
    for key in ("name", "description", "system_prompt"):
        _require(isinstance(agent[key], str), f"agent.{key} must be a string.")
    _require(len(agent["name"]) <= 200 and len(agent["description"]) <= 2000 and len(agent["system_prompt"]) <= MAX_TEXT_LENGTH, "Agent text exceeds the supported length.")
    _require(isinstance(agent["configuration"], dict) and set(agent["configuration"]).issubset(CONFIG_KEYS), "agent.configuration contains unsupported keys.")
    _require(all(value is None or isinstance(value, (str, int, float, bool)) for value in agent["configuration"].values()), "agent.configuration values must be JSON scalars.")
    for field in ("tools", "skills"):
        _require(isinstance(agent[field], list) and all(isinstance(value, str) and value for value in agent[field]), f"agent.{field} must be a list of identifiers.")
        _require(all(DEPENDENCY_ID_RE.fullmatch(value) for value in agent[field]), f"agent.{field} contains an invalid identifier.")
        _require(len(agent[field]) == len(set(agent[field])), f"agent.{field} contains duplicate identifiers.")
    _require(isinstance(agent["variables"], list), "agent.variables must be a list.")
    variable_keys = set()
    for variable in agent["variables"]:
        _require(isinstance(variable, dict) and set(variable) == {"key", "value"}, "Each variable must contain only key and value.")
        _require(isinstance(variable["key"], str) and variable["key"] and isinstance(variable["value"], str), "Variable key and value must be strings.")
        _require(VARIABLE_KEY_RE.fullmatch(variable["key"]), f"Invalid variable key: {variable['key']}.")
        _require(variable["key"] not in variable_keys, f"Duplicate variable key: {variable['key']}.")
        variable_keys.add(variable["key"])
    _require(isinstance(agent["knowledge_base"], list), "agent.knowledge_base must be a list.")
    paths = set()
    for item in agent["knowledge_base"]:
        _require(isinstance(item, dict) and set(item) == {"path", "content"}, "Each knowledge-base file must contain only path and content.")
        path = _safe_kb_path(item["path"])
        _require(isinstance(item["content"], str) and len(item["content"]) <= MAX_KB_FILE_LENGTH, f"Knowledge-base file '{path}' has invalid content.")
        _require(path not in paths, f"Duplicate knowledge-base path: {path}.")
        item["path"] = path
        paths.add(path)
    return payload


def _available_tools(db) -> set:
    from backend.tools import tool_registry
    ids = {tool["id"] for tool in db.get_tools()}
    for definition in tool_registry.get_all_tool_defs() + tool_registry.get_builtin_tool_defs():
        identifier = definition.get("id") or definition.get("function", {}).get("name")
        if identifier:
            ids.add(identifier)
    return ids


def _skill_tool_ids(skill_id: str) -> set:
    from backend.skills_manager import skills_manager
    return {
        f"skill:{skill_id}:{definition.get('function', {}).get('name', '')}"
        for definition in skills_manager.get_skill_tool_defs(skill_id)
        if definition.get('function', {}).get('name')
    }


def preflight_import(db, payload: Any) -> Dict[str, Any]:
    payload = validate_payload(payload)
    from backend.skills_manager import skills_manager
    skills = {skill.get("id"): skill for skill in skills_manager.list_skills()}
    unavailable_skills = {s for s in payload["agent"]["skills"]
                          if s not in skills or not skills[s].get("enabled", False)}
    available_tools = _available_tools(db)
    effective_tools, skipped_tools = [], set()
    for tool_id in payload["agent"]["tools"]:
        match = re.fullmatch(r"skill:([^:]+):([^:]+)", tool_id)
        if not match:
            if tool_id not in available_tools:
                raise AgentPortabilityError(f"Assigned tools are unavailable: {tool_id}.")
            effective_tools.append(tool_id)
            continue
        skill_id = match.group(1)
        if skill_id in unavailable_skills:
            skipped_tools.add(tool_id)
        elif skill_id not in payload["agent"]["skills"]:
            raise AgentPortabilityError(f"Tool '{tool_id}' does not belong to an assigned skill.")
        elif tool_id not in _skill_tool_ids(skill_id):
            raise AgentPortabilityError(f"Assigned skill tool is unavailable: {tool_id}.")
        else:
            effective_tools.append(tool_id)
    effective = copy.deepcopy(payload)
    effective["agent"]["skills"] = [s for s in payload["agent"]["skills"] if s not in unavailable_skills]
    effective["agent"]["tools"] = effective_tools
    return {"payload": effective, "warning": {"skills": sorted(unavailable_skills), "tools": sorted(skipped_tools)}}


def _validate_dependencies(db, agent: Dict[str, Any]) -> Dict[str, Any]:
    result = preflight_import(db, {"schema": SCHEMA, "version": VERSION,
        "metadata": {"omitted_secret_variable_keys": []}, "agent": agent})
    warning = result["warning"]
    if warning["skills"] or warning["tools"]:
        raise AgentPortabilityError("Unavailable dependencies require confirmation: " +
            ", ".join(warning["skills"] + warning["tools"]) + "." )
    return result["payload"]["agent"]


def _default_agent_id(db, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "imported_agent"
    base = base[:64].rstrip("_") or "imported_agent"
    if SUBAGENT_ID_RE.search(base):
        base += "_agent"
    candidate, suffix = base, 2
    while db.get_agent(candidate):
        tail = f"_{suffix}"
        candidate = base[:64 - len(tail)].rstrip("_") + tail
        suffix += 1
    return candidate


def import_agent(db, payload: Any, base_dir: str, agent_id: Optional[str] = None, name: Optional[str] = None, confirm_missing: bool = False) -> str:
    payload = validate_payload(payload)
    if confirm_missing:
        payload = preflight_import(db, payload)["payload"]
    agent = payload["agent"]
    target_name = name if name is not None else agent["name"]
    _require(isinstance(target_name, str) and target_name.strip() and len(target_name) <= 200, "Agent name must be a non-empty string up to 200 characters.")
    explicit_id = agent_id is not None and str(agent_id).strip() != ""
    target_id = str(agent_id).strip() if explicit_id else _default_agent_id(db, target_name)
    _require(ID_RE.fullmatch(target_id), "Agent ID must use lowercase letters, numbers, and underscores.")
    _require(not SUBAGENT_ID_RE.search(target_id), "Agent ID cannot use the reserved sub-agent suffix pattern.")
    if explicit_id:
        _require(not db.get_agent(target_id), f"Agent ID '{target_id}' already exists.")
    if not confirm_missing:
        _validate_dependencies(db, agent)
    agent_root, workspace = _agent_dir(base_dir, target_id), os.path.join(base_dir, "shared", "agents", target_id)
    _require(not os.path.exists(agent_root), f"Agent directory already exists: {agent_root}.")
    _require(not os.path.exists(workspace), f"Agent workspace already exists: {workspace}.")
    created = False
    try:
        config = dict(agent["configuration"])
        config.update({"id": target_id, "name": target_name.strip(), "description": agent["description"], "system_prompt": agent["system_prompt"], "is_super": False, "workspace": workspace, "workplace_id": None})
        db.create_agent(config)
        created = True
        db.update_agent(target_id, config)
        db.set_agent_tools(target_id, agent["tools"])
        db.set_agent_skills(target_id, agent["skills"])
        db.set_agent_variables_bulk(target_id, agent["variables"])
        os.makedirs(_kb_dir(base_dir, target_id), exist_ok=False)
        with open(os.path.join(agent_root, "SYSTEM.md"), "w", encoding="utf-8") as handle:
            handle.write(agent["system_prompt"])
        for item in agent["knowledge_base"]:
            path = os.path.join(_kb_dir(base_dir, target_id), _safe_kb_path(item["path"]))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(item["content"])
        os.makedirs(workspace, exist_ok=False)
        return target_id
    except Exception:
        if created:
            db.delete_agent(target_id)
        shutil.rmtree(agent_root, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)
        raise
