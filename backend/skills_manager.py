"""
Skills Manager — discovers, installs, and manages skill packages.

A skill is a directory under skills/ containing:
- skill.json: manifest with id, name, version, description, tools_file, enabled
- setup.py: install(context) and uninstall(context) functions
- backend/tools/*.py: tool backend implementations
- <tools_file>.json: tool function definitions
"""

import os
import re
import json
import shutil
import zipfile
import tempfile
import importlib.util
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(BASE_DIR, 'skills')
CONFIG_PATH = os.path.join(SKILLS_DIR, 'config.json')


def _load_global_config() -> Dict[str, Any]:
    """Load the global skills config (disabled_skills list)."""
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


class SkillsManager:
    def __init__(self):
        os.makedirs(SKILLS_DIR, exist_ok=True)
        self._skill_name_cache: Dict[str, str] = {}

    def is_skill_enabled(self, skill_id: str) -> bool:
        """Check if a skill is enabled. DB is authoritative; absent = disabled."""
        from models.db import db
        return db.get_setting(f'skill_enabled:{skill_id}') == '1'

    def list_skills(self) -> List[Dict[str, Any]]:
        """List all installed skills with metadata."""
        skills = []
        if not os.path.isdir(SKILLS_DIR):
            return skills
        for name in sorted(os.listdir(SKILLS_DIR)):
            skill_dir = os.path.join(SKILLS_DIR, name)
            manifest_path = os.path.join(skill_dir, 'skill.json')
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, encoding='utf-8') as f:
                    manifest = json.load(f)
                manifest['_dir'] = skill_dir
                manifest['tool_count'] = len(self._load_tool_defs(skill_dir, manifest))
                manifest['enabled'] = self.is_skill_enabled(name)
                skills.append(manifest)
            except (json.JSONDecodeError, KeyError):
                continue
        return skills

    def get_skill(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """Get a single skill's metadata, tool list, variables, and config."""
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        manifest_path = os.path.join(skill_dir, 'skill.json')
        if not os.path.isfile(manifest_path):
            return None
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        manifest['enabled'] = self.is_skill_enabled(skill_id)
        tool_defs = self._load_tool_defs(skill_dir, manifest)
        manifest['_dir'] = skill_dir
        manifest['tools'] = [
            {'name': t.get('function', {}).get('name', ''), 'description': t.get('function', {}).get('description', '')}
            for t in tool_defs
        ]
        manifest['tool_count'] = len(tool_defs)
        manifest['variables'] = manifest.get('variables', [])
        manifest['config'] = self.get_skill_config(skill_id)
        return manifest

    def get_skill_name(self, skill_id: str) -> str:
        """Read only the manifest name from skill.json — no tool defs, no DB queries."""
        cached = self._skill_name_cache.get(skill_id)
        if cached is not None:
            return cached
        manifest_path = os.path.join(SKILLS_DIR, skill_id, 'skill.json')
        if not os.path.isfile(manifest_path):
            self._skill_name_cache[skill_id] = skill_id
            return skill_id  # fallback to ID
        try:
            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)
            name = manifest.get('name', skill_id)
            self._skill_name_cache[skill_id] = name
            return name
        except (json.JSONDecodeError, IOError):
            self._skill_name_cache[skill_id] = skill_id
            return skill_id

    def get_skill_tool_defs(self, skill_id: str) -> List[Dict[str, Any]]:
        """Load tool definitions for a specific skill."""
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        manifest_path = os.path.join(skill_dir, 'skill.json')
        if not os.path.isfile(manifest_path):
            return []
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        if not self.is_skill_enabled(skill_id):
            return []
        return self._load_tool_defs(skill_dir, manifest)

    def get_all_skill_tool_defs(self) -> List[Dict[str, Any]]:
        """Load tool definitions from ALL enabled skills.

        Skills with lazy_tools=true are excluded here — their tool defs are
        only injected into the LLM context after the agent calls use_skill().
        """
        all_defs = []
        for skill in self.list_skills():
            skill_id = skill.get('id', '')
            if not self.is_skill_enabled(skill_id):
                continue
            # Lazy-loaded skills are excluded from upfront context injection
            if skill.get('lazy_tools', False):
                continue
            skill_dir = skill.get('_dir', os.path.join(SKILLS_DIR, skill_id))
            defs = self._load_tool_defs(skill_dir, skill)
            # Tag each def with its skill origin and namespaced ID
            for d in defs:
                d['_skill_id'] = skill_id
                d['_skill_dir'] = skill_dir
                fn_name = d.get('function', {}).get('name', '')
                d['id'] = f"skill:{skill_id}:{fn_name}"
            all_defs.extend(defs)
        return all_defs

    def install_skill(self, zip_path: str, force: bool = False) -> Dict[str, Any]:
        """Install a skill from a zip file. Returns skill manifest or error."""
        if not zipfile.is_zipfile(zip_path):
            return {'error': 'Not a valid zip file'}

        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Security: check for path traversal in entry names AND extraction destinations
                tmp_dir_real = os.path.realpath(tmp_dir)
                for entry in zf.namelist():
                    # Check entry name for obvious traversal attempts
                    if entry.startswith('/') or '..' in entry:
                        return {'error': f'Unsafe path in zip: {entry}'}
                    
                    # Validate the actual extraction destination
                    extract_path = os.path.join(tmp_dir, entry)
                    extract_path_real = os.path.realpath(extract_path)
                    
                    if not extract_path_real.startswith(tmp_dir_real + os.sep):
                        return {'error': f'Path traversal detected in zip: {entry}'}
                
                zf.extractall(tmp_dir)

            # Find skill.json — at root or one level deep
            manifest_path = self._find_manifest(tmp_dir)
            if not manifest_path:
                return {'error': 'No skill.json found in zip (must be at root or one directory deep)'}

            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)

            skill_id = manifest.get('id', '')
            if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
                return {'error': f'Invalid skill id: {skill_id}. Must be alphanumeric with dashes/underscores.'}

            # The skill content directory is where skill.json lives
            skill_src = os.path.dirname(manifest_path)

            # Verify tools_file exists
            tools_file = manifest.get('tools_file', '')
            if tools_file and not os.path.isfile(os.path.join(skill_src, tools_file)):
                return {'error': f'Tools file not found in package: {tools_file}'}

            # Check for duplicate ID
            skill_dest = os.path.join(SKILLS_DIR, skill_id)
            if os.path.exists(skill_dest) and not force:
                return {'error': f'Skill "{skill_id}" is already installed. Uninstall it first or use force to overwrite.'}

            # Preserve config.json across reinstalls
            saved_config = None
            config_path = os.path.join(skill_dest, 'config.json')
            if os.path.isfile(config_path):
                with open(config_path, encoding='utf-8') as f:
                    saved_config = f.read()
            if os.path.exists(skill_dest):
                shutil.rmtree(skill_dest)
            shutil.copytree(skill_src, skill_dest)
            if saved_config is not None:
                with open(os.path.join(skill_dest, 'config.json'), 'w', encoding='utf-8') as f:
                    f.write(saved_config)

            # Run setup.install() if available
            setup_result = self._run_setup(skill_dest, skill_id, 'install')

            manifest['_setup_result'] = setup_result
            return manifest

    def install_skill_from_dir(self, source_dir: str, force: bool = False) -> Dict[str, Any]:
        """Install a skill from a directory path (for CLI usage)."""
        manifest_path = os.path.join(source_dir, 'skill.json')
        if not os.path.isfile(manifest_path):
            return {'error': f'No skill.json found in {source_dir}'}

        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)

        skill_id = manifest.get('id', '')
        if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
            return {'error': f'Invalid skill id: {skill_id}'}

        # Copy to skills directory (skip if source is already in skills/)
        skill_dest = os.path.join(SKILLS_DIR, skill_id)
        source_norm = os.path.normpath(os.path.abspath(source_dir))
        dest_norm = os.path.normpath(os.path.abspath(skill_dest))

        if source_norm != dest_norm:
            if os.path.exists(skill_dest) and not force:
                return {'error': f'Skill "{skill_id}" is already installed. Uninstall it first or use force to overwrite.'}
            saved_config = None
            config_path = os.path.join(skill_dest, 'config.json')
            if os.path.isfile(config_path):
                with open(config_path, encoding='utf-8') as f:
                    saved_config = f.read()
            if os.path.exists(skill_dest):
                shutil.rmtree(skill_dest)
            shutil.copytree(source_dir, skill_dest)
            if saved_config is not None:
                with open(os.path.join(skill_dest, 'config.json'), 'w', encoding='utf-8') as f:
                    f.write(saved_config)

        # Run setup.install()
        setup_result = self._run_setup(skill_dest, skill_id, 'install')
        manifest['_setup_result'] = setup_result
        return manifest

    def uninstall_skill(self, skill_id: str) -> Dict[str, Any]:
        """Uninstall a skill: run setup.uninstall() then delete directory."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
            return {'error': 'Invalid skill id'}

        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        if not os.path.isdir(skill_dir):
            return {'error': f'Skill not found: {skill_id}'}

        # Run setup.uninstall() first
        setup_result = self._run_setup(skill_dir, skill_id, 'uninstall')

        # Delete the skill directory
        shutil.rmtree(skill_dir)
        return {'success': True, 'setup_result': setup_result}

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable a skill. State is stored in DB, not in skill.json."""
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        manifest_path = os.path.join(skill_dir, 'skill.json')
        if not os.path.isfile(manifest_path):
            return {'error': f'Skill not found: {skill_id}'}

        from models.db import db
        db.set_setting(f'skill_enabled:{skill_id}', '1' if enabled else '0')

        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        manifest['enabled'] = enabled
        return manifest

    def get_skill_variables(self, skill_id: str) -> List[Dict[str, Any]]:
        """Read the variables schema from skill.json."""
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        manifest_path = os.path.join(skill_dir, 'skill.json')
        if not os.path.isfile(manifest_path):
            return []
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        return manifest.get('variables', [])

    def get_skill_config(self, skill_id: str) -> Dict[str, Any]:
        """Load config from DB merged with defaults from variables schema."""
        variables = self.get_skill_variables(skill_id)
        # Build defaults from schema
        config = {}
        for v in variables:
            config[v['name']] = v.get('default', '')
        # Override with user-set values from DB
        from models.db import db
        for v in variables:
            key = f'skill_config:{skill_id}:{v["name"]}'
            stored = db.get_setting(key)
            if stored is not None:
                var_type = v.get('type', 'string')
                if var_type == 'boolean':
                    config[v['name']] = stored in ('1', 'true', 'True')
                elif var_type == 'number':
                    try:
                        config[v['name']] = float(stored) if '.' in stored else int(stored)
                    except ValueError:
                        pass
                else:
                    config[v['name']] = stored
        return config

    def set_skill_config(self, skill_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and save config values to DB."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
            return {'error': 'Invalid skill id'}
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        if not os.path.isdir(skill_dir):
            return {'error': f'Skill not found: {skill_id}'}

        variables = self.get_skill_variables(skill_id)
        var_map = {v['name']: v for v in variables}

        # Validate and coerce types
        clean = {}
        for name, val in values.items():
            if name not in var_map:
                continue
            var_def = var_map[name]
            var_type = var_def.get('type', 'string')
            try:
                if var_type == 'number':
                    clean[name] = float(val) if '.' in str(val) else int(val)
                elif var_type == 'boolean':
                    clean[name] = val if isinstance(val, bool) else str(val).lower() in ('true', '1', 'yes')
                else:
                    clean[name] = str(val)
            except (ValueError, TypeError):
                return {'error': f'Invalid value for {var_def.get("label", name)}: expected {var_type}'}

        # Save to DB
        from models.db import db
        for name, val in clean.items():
            key = f'skill_config:{skill_id}:{name}'
            db.set_setting(key, str(val))
        return {'success': True, 'config': self.get_skill_config(skill_id)}

    def find_tool_backend_path(self, tool_name: str, skill_id: str = None) -> Optional[str]:
        """Find the backend .py file for a tool across skills.

        Args:
            tool_name: Function name of the tool.
            skill_id: If provided, only search this skill's directory.
        """
        global_config = _load_global_config()
        disabled = global_config.get('disabled_skills', [])
        for skill in self.list_skills():
            if skill.get('id', '') in disabled:
                continue
            if skill_id and skill['id'] != skill_id:
                continue
            skill_dir = skill.get('_dir', os.path.join(SKILLS_DIR, skill['id']))
            tool_path = os.path.join(skill_dir, 'backend', 'tools', f'{tool_name}.py')
            if os.path.isfile(tool_path):
                return tool_path
        return None

    def find_tool_skill_dir(self, tool_name: str, skill_id: str = None) -> Optional[str]:
        """Find the skill directory that provides a given tool backend.

        Args:
            tool_name: Function name of the tool.
            skill_id: If provided, only search this skill's directory.
        """
        global_config = _load_global_config()
        disabled = global_config.get('disabled_skills', [])
        for skill in self.list_skills():
            if skill.get('id', '') in disabled:
                continue
            if skill_id and skill['id'] != skill_id:
                continue
            skill_dir = skill.get('_dir', os.path.join(SKILLS_DIR, skill['id']))
            tool_path = os.path.join(skill_dir, 'backend', 'tools', f'{tool_name}.py')
            if os.path.isfile(tool_path):
                return skill_dir
        return None

    def update_skill_tool_field(self, skill_id: str, fn_name: str, field: str, value) -> Dict[str, Any]:
        """Update a single field on a specific tool entry in a skill's tool-defs JSON file."""
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        manifest_path = os.path.join(skill_dir, 'skill.json')
        if not os.path.isfile(manifest_path):
            return {'error': f"Skill '{skill_id}' not found"}
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        tools_file = manifest.get('tools_file', '')
        if not tools_file:
            return {'error': f"Skill '{skill_id}' has no tools_file"}
        tools_path = os.path.join(skill_dir, tools_file)
        if not os.path.isfile(tools_path):
            return {'error': f"Tools file not found: {tools_file}"}
        with open(tools_path, encoding='utf-8') as f:
            defs = json.load(f)
        updated = False
        for entry in defs:
            if entry.get('function', {}).get('name') == fn_name:
                entry[field] = value
                updated = True
                break
        if not updated:
            return {'error': f"Tool '{fn_name}' not found in skill '{skill_id}'"}
        with open(tools_path, 'w', encoding='utf-8') as f:
            json.dump(defs, f, indent=2, ensure_ascii=False)
        return {'success': True}

    def _load_tool_defs(self, skill_dir: str, manifest: dict) -> List[Dict[str, Any]]:
        """Load tool definitions from a skill's tools file."""
        tools_file = manifest.get('tools_file', '')
        if not tools_file:
            return []
        tools_path = os.path.join(skill_dir, tools_file)
        if not os.path.isfile(tools_path):
            return []
        try:
            with open(tools_path, encoding='utf-8') as f:
                data = json.load(f)
            # The tools file is an array of OpenAI function defs
            if isinstance(data, list):
                return data
            return []
        except (json.JSONDecodeError, IOError):
            return []

    def _find_manifest(self, directory: str) -> Optional[str]:
        """Find skill.json at root or one level deep in extracted zip."""
        # Check root
        root_manifest = os.path.join(directory, 'skill.json')
        if os.path.isfile(root_manifest):
            return root_manifest
        # Check one level deep
        for name in os.listdir(directory):
            sub = os.path.join(directory, name)
            if os.path.isdir(sub):
                sub_manifest = os.path.join(sub, 'skill.json')
                if os.path.isfile(sub_manifest):
                    return sub_manifest
        return None

    def _run_setup(self, skill_dir: str, skill_id: str, func_name: str) -> dict:
        """Run setup.install() or setup.uninstall() from the skill's setup.py."""
        setup_path = os.path.join(skill_dir, 'setup.py')
        if not os.path.isfile(setup_path):
            return {'skipped': True, 'reason': 'No setup.py found'}

        context = {
            'skill_dir': skill_dir,
            'app_dir': BASE_DIR,
            'skill_id': skill_id,
        }

        try:
            spec = importlib.util.spec_from_file_location(f'skill_setup_{skill_id}', setup_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            func = getattr(module, func_name, None)
            if func is None:
                return {'skipped': True, 'reason': f'No {func_name}() function in setup.py'}

            return func(context)
        except Exception as e:
            return {'error': f'setup.{func_name}() failed: {str(e)}'}


skills_manager = SkillsManager()
