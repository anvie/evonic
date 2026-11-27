"""
Learned Skills — skills an agent writes for itself.

Unlike the global skills in skills/ (see skills_manager.py), a learned skill
belongs to one agent and lives in that agent's own directory:

  agents/<agent_id>/skills/<skill_id>/
    skill.json           — manifest (id, name, description, brief, tools_file)
    SYSTEM.md            — instructions (optional when the skill has tools)
    tools.json           — tool definitions, OpenAI function format (optional)
    scripts/<fn>.sh|.py  — one script per tool

Learned skills are always lazy and are loaded with use_skill / unload_skill.
A learned tool is only a script: it runs through the bash / runpy tools, so it
executes wherever those do (sandbox, workplace) and never inside this process.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from typing import Any, Dict, List, Optional

from backend.skills_manager import SKILLS_DIR
from backend.tools._workspace import effective_agent_id, resolve_self_path
from backend.zip_validator import (MAX_ENTRY_COUNT, MAX_UNCOMPRESSED_BYTES,
                                   copytree_ignore, is_junk_path)

# Prompt budget: how many learned skills are advertised, and how long a brief may be.
MAX_LISTED = 30
MAX_BRIEF_CHARS = 200
SCRIPT_TIMEOUT = 300

_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_FN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
# Script extension -> the assigned tool that runs it.
_RUNNERS = {'.sh': 'bash', '.py': 'runpy'}

_reserved_names_cache: Optional[frozenset] = None


def is_enabled(agent: dict) -> bool:
    """Whether *agent* (or, for a sub-agent, its parent) has learned skills turned on."""
    if agent and 'learned_skills_enabled' in agent:
        return bool(agent.get('learned_skills_enabled'))
    eid = effective_agent_id(agent)
    if not eid:
        return False
    from models.db import db
    return bool((db.get_agent(eid) or {}).get('learned_skills_enabled'))


def skills_dir(agent: dict) -> Optional[str]:
    """Absolute path of the agent's learned-skills directory (it may not exist yet)."""
    eid = effective_agent_id(agent)
    if not eid:
        return None
    return resolve_self_path(eid, '/_self/skills', agent)


def _skill_dir(agent: dict, skill_id: str) -> Optional[str]:
    root = skills_dir(agent)
    if not root or not _ID_RE.match(skill_id or ''):
        return None
    path = os.path.join(root, skill_id)
    return path if os.path.isdir(path) else None


def exists(agent: dict, skill_id: str) -> bool:
    """Whether the agent has a learned-skill folder named *skill_id* (valid or not)."""
    return _skill_dir(agent, skill_id) is not None


def is_learned(agent: dict, skill_id: str) -> bool:
    """Whether *skill_id* resolves to the agent's learned skill. A global skill
    with the same id always wins, so this is False for any global skill id."""
    return (not os.path.isdir(os.path.join(SKILLS_DIR, skill_id or ''))
            and exists(agent, skill_id))


def _reserved_tool_names() -> frozenset:
    """Function names a learned tool may not take: core tools and built-ins."""
    global _reserved_names_cache
    if _reserved_names_cache is None:
        from backend.tools import tool_registry
        from backend.tools.registry import TOOLS_DIR
        names = {f[:-3] for f in os.listdir(TOOLS_DIR) if f.endswith('.py')}
        names.update(d.get('name', '') for d in tool_registry.get_builtin_tool_defs())
        _reserved_names_cache = frozenset(names)
    return _reserved_names_cache


def script_path(skill_dir: str, fn_name: str) -> Optional[str]:
    """Path of the script backing tool *fn_name*, or None when there is none."""
    for ext in _RUNNERS:
        path = os.path.join(skill_dir, 'scripts', fn_name + ext)
        if os.path.isfile(path):
            return path
    return None


def _load(agent: dict, skill_id: str):
    """Read and check one learned skill. Returns (manifest, tool_defs, error)."""
    if not _ID_RE.match(skill_id or ''):
        return None, [], (f"Invalid skill id '{skill_id}': use lowercase letters, "
                          f"digits, '-' and '_'.")
    skill_dir = _skill_dir(agent, skill_id)
    if not skill_dir:
        return None, [], f"Learned skill '{skill_id}' not found."
    if os.path.isdir(os.path.join(SKILLS_DIR, skill_id)):
        return None, [], (f"Learned skill id '{skill_id}' collides with a global skill. "
                          f"Rename the folder and the id in skill.json.")
    try:
        with open(os.path.join(skill_dir, 'skill.json'), encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return None, [], f"skill.json is missing or not valid JSON: {e}"
    if not isinstance(manifest, dict) or manifest.get('id') != skill_id:
        return None, [], f"skill.json must be an object whose \"id\" is \"{skill_id}\"."

    tool_defs = []
    tools_file = manifest.get('tools_file', '')
    if tools_file:
        try:
            with open(os.path.join(skill_dir, os.path.basename(tools_file)), encoding='utf-8') as f:
                tool_defs = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return None, [], f"{tools_file} is missing or not valid JSON: {e}"
        if not isinstance(tool_defs, list):
            return None, [], f"{tools_file} must be a JSON array of function definitions."
        for td in tool_defs:
            fn = td.get('function', {}).get('name', '') if isinstance(td, dict) else ''
            if not _FN_RE.match(fn or ''):
                return None, [], f"{tools_file}: every entry needs a valid function.name."
            if fn in _reserved_tool_names():
                return None, [], f"Tool name '{fn}' is taken by a core tool. Pick another name."
            if not script_path(skill_dir, fn):
                return None, [], f"Tool '{fn}' has no script: add scripts/{fn}.sh or scripts/{fn}.py."

    if not tool_defs and not os.path.isfile(os.path.join(skill_dir, 'SYSTEM.md')):
        return None, [], "A learned skill needs a SYSTEM.md, tools, or both."
    return manifest, tool_defs, None


def validate(agent: dict, skill_id: str) -> Optional[str]:
    """Return why learned skill *skill_id* is unusable, or None when it is fine."""
    return _load(agent, skill_id)[2]


def list_skills(agent: dict, include_invalid: bool = False) -> List[Dict[str, Any]]:
    """The agent's learned skills, sorted by id.

    Invalid skills are left out unless *include_invalid* is set, in which case
    they are returned with an ``error`` key.
    """
    root = skills_dir(agent)
    if not root or not os.path.isdir(root):
        return []
    skills = []
    for skill_id in sorted(os.listdir(root)):
        skill_dir = os.path.join(root, skill_id)
        if not os.path.isdir(skill_dir):
            continue
        manifest, tool_defs, error = _load(agent, skill_id)
        if error and not include_invalid:
            continue
        manifest = manifest or {}
        entry = {
            'id': skill_id,
            'name': manifest.get('name', skill_id),
            'description': manifest.get('description', ''),
            'brief': manifest.get('brief', ''),
            'tools': [td['function']['name'] for td in tool_defs],
            'enabled': manifest.get('enabled', True) is not False,
            'updated_at': _skill_mtime(skill_dir),
        }
        if error:
            entry['error'] = error
        skills.append(entry)
    return skills


def is_skill_enabled(agent: dict, skill_id: str) -> bool:
    """Whether learned skill *skill_id* may be listed and loaded. A skill is on unless switched off."""
    manifest = _load(agent, skill_id)[0] or {}
    return manifest.get('enabled', True) is not False


def set_skill_enabled(agent: dict, skill_id: str, enabled: bool) -> bool:
    """Switch a learned skill on or off by writing ``enabled`` into its skill.json."""
    skill_dir = _skill_dir(agent, skill_id)
    if not skill_dir:
        return False
    path = os.path.join(skill_dir, 'skill.json')
    try:
        with open(path, encoding='utf-8') as f:
            manifest = json.load(f)
        if enabled:
            manifest.pop('enabled', None)
        else:
            manifest['enabled'] = False
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    return True


def get_skill(agent: dict, skill_id: str) -> Optional[Dict[str, Any]]:
    """Manifest, SYSTEM.md text and tool definitions of one valid learned skill."""
    manifest, tool_defs, error = _load(agent, skill_id)
    if error:
        return None
    system_md = ''
    system_md_path = os.path.join(_skill_dir(agent, skill_id), 'SYSTEM.md')
    if os.path.isfile(system_md_path):
        with open(system_md_path, encoding='utf-8') as f:
            system_md = f.read()
    return {**manifest, 'system_md': system_md, 'tool_defs': tool_defs}


def delete_skill(agent: dict, skill_id: str) -> bool:
    skill_dir = _skill_dir(agent, skill_id)
    if not skill_dir:
        return False
    shutil.rmtree(skill_dir)
    return True


def export_zip(agent: dict, skill_id: str, out_path: str) -> bool:
    """Write learned skill *skill_id* to *out_path* as a zip holding one ``<skill_id>/`` folder."""
    skill_dir = _skill_dir(agent, skill_id)
    if not skill_dir:
        return False
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, files in os.walk(skill_dir):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, skill_dir)
                if not is_junk_path(rel):
                    zf.write(full, os.path.join(skill_id, rel))
    return True


def import_zip(agent: dict, zip_path: str, force: bool = False) -> Dict[str, Any]:
    """Install a learned skill exported by export_zip() into *agent*.

    Returns ``{'id': ...}``, or ``{'error': ...}`` (with ``'exists': True`` when
    the agent already has that skill and *force* is not set).
    """
    root = skills_dir(agent)
    if not root:
        return {'error': 'Agent not found.'}
    if not zipfile.is_zipfile(zip_path):
        return {'error': 'Not a valid zip file.'}
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRY_COUNT or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
                return {'error': 'Zip is too large.'}
            tmp_real = os.path.realpath(tmp_dir)
            for info in infos:
                dest = os.path.realpath(os.path.join(tmp_dir, info.filename))
                if info.filename.startswith('/') or '..' in info.filename \
                        or not dest.startswith(tmp_real + os.sep):
                    return {'error': f'Unsafe path in zip: {info.filename}'}
            zf.extractall(tmp_dir)

        # skill.json sits at the root or one directory deep
        src = tmp_dir
        if not os.path.isfile(os.path.join(src, 'skill.json')):
            subdirs = [d for d in os.listdir(tmp_dir)
                       if os.path.isfile(os.path.join(tmp_dir, d, 'skill.json'))]
            if len(subdirs) != 1:
                return {'error': 'No skill.json found in zip (must be at root or one directory deep).'}
            src = os.path.join(tmp_dir, subdirs[0])
        try:
            with open(os.path.join(src, 'skill.json'), encoding='utf-8') as f:
                skill_id = json.load(f).get('id', '')
        except (OSError, json.JSONDecodeError, AttributeError):
            return {'error': 'skill.json is not valid JSON.'}
        if not _ID_RE.match(skill_id or ''):
            return {'error': f"Invalid skill id '{skill_id}'."}

        dest = os.path.join(root, skill_id)
        backup = os.path.join(tmp_dir, '.previous')
        if os.path.exists(dest):
            if not force:
                return {'error': f"This agent already has a learned skill '{skill_id}'.", 'exists': True}
            shutil.move(dest, backup)
        os.makedirs(root, exist_ok=True)
        shutil.copytree(src, dest, ignore=copytree_ignore)
        error = validate(agent, skill_id)
        if error:
            shutil.rmtree(dest)
            if os.path.exists(backup):
                shutil.move(backup, dest)
            return {'error': error}
    return {'id': skill_id}


def _skill_mtime(skill_dir: str) -> float:
    mtimes = [os.stat(skill_dir).st_mtime]
    for name in ('skill.json', 'SYSTEM.md', 'tools.json'):
        try:
            mtimes.append(os.stat(os.path.join(skill_dir, name)).st_mtime)
        except OSError:
            pass
    return max(mtimes)


def mtime_hash(agent: dict) -> str:
    """Hash that changes whenever a learned skill is added, removed or edited.

    Uses only stat() calls, like context._get_skills_mtime_hash().
    """
    root = skills_dir(agent)
    entries = []
    if root and os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            skill_dir = os.path.join(root, name)
            if os.path.isdir(skill_dir):
                entries.append(f"{name}:{_skill_mtime(skill_dir)}")
    return hashlib.sha256(','.join(entries).encode()).hexdigest()


def run_tool(agent: dict, skill_id: str, fn_name: str, args: dict) -> dict:
    """Run a learned tool's script through the agent's own bash / runpy tool.

    Arguments reach the script as environment variables: ``ARG_<NAME>`` for
    each argument, and ``SKILL_ARGS`` holding all of them as JSON.
    """
    skill_dir = _skill_dir(agent, skill_id)
    path = script_path(skill_dir, fn_name) if skill_dir else None
    if not path:
        return {'error': f"Learned skill '{skill_id}' has no script for tool '{fn_name}'."}
    runner = _RUNNERS[os.path.splitext(path)[1]]
    # A learned tool must never do more than the agent could already do by hand.
    if runner not in set((agent or {}).get('assigned_tool_ids') or []):
        return {'error': f"Tool '{fn_name}' runs a script through `{runner}`, "
                         f"which is not assigned to this agent."}
    try:
        with open(path, encoding='utf-8') as f:
            script = f.read()
    except OSError as e:
        return {'error': f"Cannot read script for tool '{fn_name}': {e}"}

    args = args or {}
    env = {'SKILL_ARGS': json.dumps(args)}
    for key, value in args.items():
        name = 'ARG_' + re.sub(r'[^A-Za-z0-9_]', '_', str(key)).upper()
        env[name] = value if isinstance(value, str) else json.dumps(value)

    if runner == 'bash':
        from backend.tools import bash
        return bash.execute(agent, {'script': script, 'env': env, 'timeout': SCRIPT_TIMEOUT})
    from backend.tools import runpy
    return runpy.execute(agent, {'code': script, 'env': env, 'timeout': SCRIPT_TIMEOUT})
