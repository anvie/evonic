"""
Tool Registry — discovers and manages tool backends with auto-reload.

In production mode, tools execute real Python backends from backend/tools/.
In eval mode, tools return mock responses from tools/ JSON files.
Built-in tools (like 'remember') are registered separately with agent context.
Skills extend the registry with additional tool definitions and backends.
"""

import os
import sys
import glob
import json
import types
import threading
import importlib
import importlib.util
from typing import Dict, Any, Optional, Callable, List

# Directory containing tool backend Python files
TOOLS_DIR = os.path.join(os.path.dirname(__file__))
# Directory containing tool definition JSON files (for eval mock responses)
TOOL_DEFS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'tools')

# Tools that are always available to all agents — no explicit assignment needed.
# These are regular backend/tools/.py implementations, not built-in factories.
BUILTIN_TOOL_IDS = {"save_artifact", "send_file"}


class ToolRegistry:
    def __init__(self):
        # Cache: tool_name -> { module, mtime, path }
        self._module_cache: Dict[str, dict] = {}
        # Tool definition JSON cache with mtime-based invalidation
        self._json_cache: Optional[List[Dict[str, Any]]] = None
        self._json_mtimes: Optional[tuple] = None
        self._cache_lock = threading.Lock()
        # Built-in tool factories: builtin_id -> callable(agent_context) -> tool_def_and_executor
        # IDs use 'builtin:' namespace prefix (e.g. 'builtin:remember')
        self._builtins: Dict[str, Callable] = {}
        self._builtins['builtin:clear_log_file'] = _builtin_clear_log_factory
        # Register the built-in 'use_skill' and 'unload_skill' tools
        self._builtins['builtin:use_skill'] = _builtin_use_skill_factory
        self._builtins['builtin:unload_skill'] = _builtin_unload_skill_factory
        # Register agent-state tools (active only when agent has enable_agent_state)
        self._builtins['builtin:set_mode'] = _builtin_set_mode_factory
        self._builtins['builtin:update_tasks'] = _builtin_update_tasks_factory
        self._builtins['builtin:save_plan'] = _builtin_save_plan_factory
        # ATG task-graph compiler — exposed only when agent_context['enable_atg']
        # (see get_builtin_tools gate)
        self._builtins['builtin:compile_task_graph'] = _builtin_compile_task_graph_factory
        # CMP session-path navigation — exposed only when agent_context['enable_cmp']
        self._builtins['builtin:switch_path'] = _builtin_switch_path_factory
        self._builtins['builtin:new_path'] = _builtin_new_path_factory
        self._builtins['builtin:read_transcript'] = _builtin_read_transcript_factory
        # State machine gate tool — always available, handlers registered by system/plugins
        self._builtins['builtin:state'] = _builtin_state_factory
        # Long-term memory tools. `recall` covers keyword search, brain-layer
        # synthesis (mode='think'), and graph traversal (mode='graph').
        self._builtins['builtin:remember'] = _builtin_remember_factory
        self._builtins['builtin:recall'] = _builtin_recall_factory
        self._builtins['builtin:forget_memory'] = _builtin_forget_memory_factory
        # Session recall tool
        self._builtins['builtin:recall_sessions'] = _builtin_recall_sessions_factory
        # Tool to clear active fallback flag from agent_state (agent calls this)
        self._builtins['builtin:reset_active_model'] = _builtin_reset_active_model_factory

    def _compute_json_mtimes(self) -> Optional[tuple]:
        """Return a tuple of (path, mtime) for every tools/*.json file.

        Sorted for stable comparison.  Returns None if the tools dir doesn't
        exist, which forces a cache miss on the next get_tool_defs_from_json
        call (which will return [] when the dir is missing anyway).
        """
        defs_dir = os.path.normpath(TOOL_DEFS_DIR)
        if not os.path.isdir(defs_dir):
            return None
        files = sorted(glob.glob(os.path.join(defs_dir, "*.json")))
        return tuple((f, os.path.getmtime(f)) for f in files)

    def invalidate_tool_defs_cache(self) -> None:
        """Force the next get_tool_defs_from_json call to re-read from disk.

        Call this after plugin install / update / remove so the cache picks
        up new or changed JSON definitions immediately.
        """
        with self._cache_lock:
            self._json_cache = None
            self._json_mtimes = None

    def get_tool_defs_from_json(self) -> List[Dict[str, Any]]:
        """Load tool definitions from tools/*.json (for eval & agent config UI).

        Results are cached with mtime-based invalidation.  A cache hit costs
        one os.path.isdir + one glob + N stat calls (for mtimes).
        """
        current_mtimes = self._compute_json_mtimes()

        # Fast path: cache hit, no lock needed for read
        if self._json_cache is not None and current_mtimes is not None:
            if current_mtimes == self._json_mtimes:
                return self._json_cache

        # Cache miss: rebuild under lock
        with self._cache_lock:
            # Double-check: another thread may have rebuilt while we waited
            if self._json_cache is not None and current_mtimes is not None:
                if current_mtimes == self._json_mtimes:
                    return self._json_cache

            tools: List[Dict[str, Any]] = []
            defs_dir = os.path.normpath(TOOL_DEFS_DIR)
            if os.path.isdir(defs_dir):
                for fname in sorted(os.listdir(defs_dir)):
                    if not fname.endswith('.json'):
                        continue
                    with open(os.path.join(defs_dir, fname)) as f:
                        try:
                            tools.append(json.load(f))
                        except json.JSONDecodeError:
                            pass

            # Recompute mtimes after loading (files may have changed while I/O was in flight;
            # using the post-load mtimes means the next call with unchanged files hits cache)
            self._json_mtimes = self._compute_json_mtimes()
            self._json_cache = tools
            return tools

    def get_all_tool_defs(self) -> List[Dict[str, Any]]:
        """Load tool definitions from tools/, enabled skills, and enabled plugins."""
        from backend.skills_manager import skills_manager
        from backend.plugin_manager import plugin_manager
        # get_tool_defs_from_json returns the live cached list — copy it, or
        # extend() below would grow the cache with skill defs on every call.
        all_defs = list(self.get_tool_defs_from_json())
        # Add skill tool definitions
        skill_defs = skills_manager.get_all_skill_tool_defs()
        all_defs.extend(skill_defs)
        # Add plugin tool definitions (id: 'plugin:<plugin_id>:<fn_name>')
        all_defs.extend(plugin_manager.get_all_plugin_tool_defs())
        return all_defs

    def get_mock_executor(self) -> Callable:
        """Return an executor that uses mock responses from JSON tool definitions."""
        # Pre-load all mock responses
        mocks: Dict[str, Any] = {}
        for tool_def in self.get_tool_defs_from_json():
            name = tool_def.get('function', {}).get('name') or tool_def.get('id')
            if name and 'mock_response' in tool_def:
                mocks[name] = tool_def['mock_response']

        def mock_executor(function_name: str, arguments: dict) -> dict:
            if function_name in mocks:
                mock = mocks[function_name]
                if isinstance(mock, dict):
                    return dict(mock)
                return {"result": mock}
            return {"error": f"No mock response defined for tool: {function_name}"}

        return mock_executor

    def get_real_executor(self, agent_context: dict) -> Callable:
        """Return an executor that calls real Python backend tool implementations.

        Each tool backend is a .py file in backend/tools/ with an
        execute(agent: dict, args: dict) -> dict function.
        Files are auto-reloaded when modified.

        The agent_context dict is passed as the first argument to execute() and contains:
        - agent_id, agent_name, agent_model: agent identity
        - user_id: external user who sent the message
        - channel_id: channel the message came from (None for web test chat)
        - session_id: current chat session ID
        - assigned_tool_ids: list of namespaced tool IDs assigned to this agent
        """
        ctx = dict(agent_context)

        # Build function_name -> skill_id / plugin_id mappings from assigned tool IDs
        fn_to_skill: Dict[str, str] = {}
        fn_to_plugin: Dict[str, str] = {}
        for tid in ctx.get('assigned_tool_ids', []):
            if tid.startswith('skill:'):
                parts = tid.split(':', 2)  # skill:skill_id:fn_name
                if len(parts) == 3:
                    fn_to_skill[parts[2]] = parts[1]
            elif tid.startswith('plugin:'):
                parts = tid.split(':', 2)  # plugin:plugin_id:fn_name
                if len(parts) == 3:
                    fn_to_plugin[parts[2]] = parts[1]

        def real_executor(function_name: str, arguments: dict) -> dict:
            # Authorization guard: tool must be in assigned_tool_ids
            _assigned = set(ctx.get('assigned_tool_ids', []))
            if function_name not in _assigned:
                # BUILTIN_TOOL_IDS are always allowed — no explicit assignment needed
                if function_name not in BUILTIN_TOOL_IDS:
                    # Also check namespaced IDs like skill:skill_id:fn_name
                    _namespaced_match = any(
                        tid.endswith(f':{function_name}')
                        for tid in _assigned
                    )
                    if not _namespaced_match:
                        return {
                            "error": (
                                f"Tool '{function_name}' is not assigned to this agent. "
                                "Only explicitly assigned tools can be used."
                            ),
                            "blocked_by": "authorization",
                        }

            # Agent state guard: block write tools when in plan mode or state-blocked
            # Exception: /_self/ paths are always allowed (agent's own config dir).
            from backend.tools._workspace import is_self_path
            _self_path_args = {'write_file', 'str_replace', 'patch'}
            _is_self_target = (
                function_name in _self_path_args
                and any(is_self_path(str(v)) for v in arguments.values())
            )
            ms = ctx.get('agent_state')
            if ms and not _is_self_target:
                blocked = ms.is_blocked(function_name)
                if blocked is True:
                    return {
                        "error": (
                            f"'{function_name}' is blocked in '{ms.mode}' mode. "
                            "Present your plan to the user first, then call set_mode(mode='execute') "
                            "after they approve."
                        ),
                        "blocked_by": "agent_state",
                        "current_mode": ms.mode,
                    }
                elif blocked:
                    return {
                        "error": blocked,
                        "blocked_by": "state",
                    }
            skill_id = fn_to_skill.get(function_name)
            plugin_id = fn_to_plugin.get(function_name)
            module = self._load_tool_module(function_name, skill_id=skill_id,
                                            plugin_id=plugin_id)
            if module is None:
                return {"error": f"No backend implementation for tool: {function_name}"}
            if not hasattr(module, 'execute'):
                return {"error": f"Tool backend '{function_name}' missing execute() function"}
            # Propagate live flags from agent_context (e.g. _skip_safety set after approval)
            ctx['_skip_safety'] = agent_context.get('_skip_safety', False)
            try:
                return module.execute(ctx, arguments)
            except Exception as e:
                return {"error": f"Tool execution error: {str(e)}"}

        return real_executor

    @staticmethod
    def _is_builtin_enabled(builtin_id: str, agent_context: dict) -> bool:
        """Return whether a feature-gated built-in is available to an agent."""
        if builtin_id in ('builtin:save_plan', 'builtin:set_mode', 'builtin:state'):
            return not bool(agent_context.get('always_execute'))
        if builtin_id == 'builtin:compile_task_graph':
            return bool(agent_context.get('enable_atg'))
        if builtin_id in ('builtin:switch_path', 'builtin:new_path',
                          'builtin:read_transcript', 'builtin:forget_memory'):
            return bool(agent_context.get('enable_cmp'))
        return True

    def get_builtin_tool_defs(self, agent_context: Optional[dict] = None) -> List[Dict[str, Any]]:
        """Return UI-facing built-in definitions, optionally scoped to an agent."""
        defs = []
        for builtin_id, factory in self._builtins.items():
            if agent_context is not None and not self._is_builtin_enabled(builtin_id, agent_context):
                continue
            tool_def, _ = factory(agent_context or {})
            fn = tool_def.get('function', {})
            defs.append({
                'id': builtin_id,          # e.g. 'builtin:remember'
                'name': fn.get('name', builtin_id),
                'description': fn.get('description', ''),
                'function': fn,
                '_builtin': True,
            })
        return defs

    def get_builtin_tools(self, agent_context: dict) -> List[Dict[str, Any]]:
        """Get OpenAI function definitions for built-in tools, scoped to agent context."""
        from backend.plugin_manager import should_suppress_builtin
        agent_id = agent_context.get('id', '')
        tools = []
        for builtin_id, factory in self._builtins.items():
            # Feature-gated built-ins must be absent from the agent's tool list.
            if not self._is_builtin_enabled(builtin_id, agent_context):
                continue
            tool_def, _ = factory(agent_context)
            if should_suppress_builtin(agent_id, builtin_id, tool_def):
                continue
            tools.append(tool_def)
        return tools

    def get_builtin_executor(self, agent_context: dict) -> Callable:
        """Return an executor for built-in tools, scoped to agent context.
        Executors are keyed by function name (as the LLM calls them), not the builtin ID.
        """
        executors: Dict[str, Callable] = {}
        for builtin_id, factory in self._builtins.items():
            # Keep executor availability aligned with definition exposure.
            if not self._is_builtin_enabled(builtin_id, agent_context):
                continue
            tool_def, executor = factory(agent_context)
            fn_name = tool_def['function']['name']  # e.g. 'remember'
            executors[fn_name] = executor

        def builtin_executor(function_name: str, arguments: dict) -> dict:
            if function_name in executors:
                try:
                    return executors[function_name](arguments)
                except Exception as e:
                    return {"error": f"Built-in tool error: {str(e)}"}
            return None  # Not a built-in — fall through

        return builtin_executor

    def _load_tool_module(self, tool_name: str, skill_id: str = None, plugin_id: str = None):
        """Load (or reload) a tool's Python module from backend/tools/,
        skills/*/backend/tools/, or plugins/*/backend/tools/.

        Args:
            tool_name: Function name of the tool.
            skill_id: If provided, prefer this skill's backend over others.
            plugin_id: If provided, prefer this plugin's backend over others.
        """
        tool_path = os.path.join(TOOLS_DIR, f"{tool_name}.py")
        skill_backend_dir = None
        plugin_owner = None  # plugin_id owning the resolved backend

        # If skill_id is specified, search that skill first
        if skill_id:
            from backend.skills_manager import skills_manager
            skill_path = skills_manager.find_tool_backend_path(tool_name, skill_id=skill_id)
            if skill_path:
                tool_path = skill_path
                skill_dir = skills_manager.find_tool_skill_dir(tool_name, skill_id=skill_id)
                if skill_dir:
                    skill_backend_dir = os.path.join(skill_dir, 'backend')
            # Fall through to default search if not found in specified skill
        elif plugin_id:
            from backend.plugin_manager import plugin_manager
            p_path, p_owner = plugin_manager.find_plugin_tool_backend(tool_name, plugin_id=plugin_id)
            if p_path:
                tool_path = p_path
                plugin_owner = p_owner
            # Fall through to default search if not found in specified plugin

        if not os.path.isfile(tool_path):
            # Search in skills (no skill_id hint), then plugins (no plugin_id hint)
            from backend.skills_manager import skills_manager
            skill_path = skills_manager.find_tool_backend_path(tool_name)
            if skill_path:
                tool_path = skill_path
                skill_dir = skills_manager.find_tool_skill_dir(tool_name)
                if skill_dir:
                    skill_backend_dir = os.path.join(skill_dir, 'backend')
            else:
                from backend.plugin_manager import plugin_manager
                p_path, p_owner = plugin_manager.find_plugin_tool_backend(tool_name)
                if p_path is None:
                    return None
                tool_path = p_path
                plugin_owner = p_owner

        current_mtime = os.path.getmtime(tool_path)
        if plugin_owner:
            cache_key = f"{tool_name}:plugin:{plugin_owner}"
        elif skill_id:
            cache_key = f"{tool_name}:{skill_id}"
        else:
            cache_key = tool_name
        cached = self._module_cache.get(cache_key)

        if cached and cached['mtime'] == current_mtime and cached['path'] == tool_path:
            return cached['module']

        if plugin_owner:
            module = self._exec_plugin_tool_module(
                plugin_owner, os.path.dirname(tool_path), tool_name, tool_path)
        elif skill_backend_dir:
            # Skill tools: use a unique namespace (like plugin_tools_{plugin_id})
            # so relative imports (from ._utils import ...) resolve against the
            # skill's own backend/tools/ dir, not the main backend/tools/ package.
            pkg_name = f"skill_tools_{skill_id}"
            pkg = sys.modules.get(pkg_name)
            if pkg is None:
                pkg = types.ModuleType(pkg_name)
                pkg.__package__ = pkg_name
                sys.modules[pkg_name] = pkg
            pkg.__path__ = [os.path.dirname(tool_path)]

            mod_name = f"{pkg_name}.{tool_name}"
            spec = importlib.util.spec_from_file_location(mod_name, tool_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(mod_name, None)
                raise
        else:
            # Core tools: use tools.{tool_name} namespace — relative imports
            # resolve against the main backend/tools/ package correctly.
            spec = importlib.util.spec_from_file_location(f"tools.{tool_name}", tool_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

        self._module_cache[cache_key] = {
            'module': module,
            'mtime': current_mtime,
            'path': tool_path
        }
        return module

    def _exec_plugin_tool_module(self, plugin_id: str, tools_dir: str,
                                 tool_name: str, tool_path: str):
        """Execute a plugin tool module inside a per-plugin namespace package.

        Relative imports (from ._lib import x) resolve against the plugin's
        own backend/tools/ dir and never collide across plugins (unlike the
        skills path, whose hardcoded 'tools.<name>' spec shares one parent
        package across all skills).

    'plugin_tools_<id>' is deliberately NOT under the 'plugin_pkg_<id>'
        prefix that _unload_plugin evicts: helper submodules holding
        long-lived singletons (e.g. subprocess managers) survive
        reload_plugin, which runs on every plugin config save. Tradeoff:
        edits to helper modules need an app restart; tool entrypoint .py
        files still hot-reload via mtime.
        """
        pkg_name = f'plugin_tools_{plugin_id}'
        pkg = sys.modules.get(pkg_name)
        if pkg is None:
            pkg = types.ModuleType(pkg_name)
            pkg.__package__ = pkg_name
            sys.modules[pkg_name] = pkg
        pkg.__path__ = [tools_dir]  # keep fresh across reinstalls

        mod_name = f'{pkg_name}.{tool_name}'
        spec = importlib.util.spec_from_file_location(mod_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        return module


def _builtin_clear_log_factory(agent_context: dict):
    tool_def = {
        "type": "function",
        "function": {
            "name": "clear_log_file",
            "description": "Truncates the agent-specific llm.log and sessrecap.log files and adds a reset marker with the current date.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }

    def executor(arguments: dict) -> dict:
        import backend.tools.clear_log_file as clear_tool
        return clear_tool.execute(agent_context, arguments)

    return tool_def, executor


def _builtin_use_skill_factory(agent_context: dict):
    """Factory for the built-in 'use_skill' tool."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": (
                "Load a skill's SYSTEM.md knowledge into your context for lazy-loaded skills. "
                "See Skills section for details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The ID of the skill to load (e.g. 'kanban'). Only lazy-loaded skills are supported."
                    }
                },
                "required": ["id"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        import backend.tools.use_skill as use_skill_tool
        return use_skill_tool.execute(agent_context, arguments)

    return tool_def, executor


def _builtin_unload_skill_factory(agent_context: dict):
    """Factory for the built-in 'unload_skill' tool."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "unload_skill",
            "description": (
                "Unload a previously loaded lazy skill, removing its tools from context. "
                "See Skill Cleanup Rule for details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The ID of the skill to unload (e.g. 'plugin_creator'). Only lazy-loaded skills can be unloaded."
                    }
                },
                "required": ["id"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        import backend.tools.unload_skill as unload_skill_tool
        return unload_skill_tool.execute(agent_context, arguments)

    return tool_def, executor


def _builtin_set_mode_factory(agent_context: dict):
    """Factory for the built-in 'set_mode' tool (mental state mode transitions)."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "set_mode",
            "description": (
                "Switch between plan mode (write tools blocked) and execute mode (write tools available). "
                "See Agent State section for rules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["plan", "execute"],
                        "description": "The mode to switch to."
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why you are transitioning."
                    }
                },
                "required": ["mode"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        ms = agent_context.get('agent_state')
        if ms is None:
            return {"error": "Agent state is not enabled for this agent."}
        return ms.set_mode(
            arguments.get('mode', ''),
            reason=arguments.get('reason'),
            session_id=agent_context.get('session_id'),
            agent_id=agent_context.get('id'),
        )

    return tool_def, executor


def _builtin_update_tasks_factory(agent_context: dict):
    """Factory for the built-in 'update_tasks' tool (mental state task list management)."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "update_tasks",
            "description": (
                "Manage your implementation task list "
                "(set, add, update status, remove). CRITICAL: Each entry must be "
                "ATOMIC — exactly one concrete action or outcome that can be "
                "completed independently. Split multi-action work into separate "
                "entries; never batch several actions into one task.\n"
                "Example: a 3-phase plan needs at least 3 separate tasks:\n"
                "✓ 'Audit existing API endpoints'\n"
                "✓ 'Create sandbox environment'\n"
                "✓ 'Implement database schema'\n"
                "✗ 'Audit API, create env, implement schema' — BAD: 3 actions in 1 task\n"
                "Only one implementation task may be in_progress at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "add", "done", "in_progress", "replace", "remove"],
                        "description": (
                            "'set': replace the entire task list (provide a structured 'tasks' array). "
                            "'add': add one atomic task. "
                            "'done': mark a task complete. 'in_progress': make the "
                            "selected task the sole active task; this returns every "
                            "other active task to pending. 'replace': update task text "
                            "while preserving its ID and status. 'remove': delete a task."
                        )
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "Task ID for done/in_progress/remove actions."
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "One atomic task for the 'add' action: exactly one "
                            "concrete, independently completable action or outcome."
                        )
                    },
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "text": {
                                    "type": "string",
                                    "description": "Exactly one concrete, independently completable action or outcome."
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done"]
                                }
                            },
                            "required": ["text"],
                            "additionalProperties": False,
                            "description": "Structured task item; exactly one concrete, independently completable action or outcome. ID and status preserve existing task state when provided."
                        },
                        "description": (
                            "Atomic task descriptions for the 'set' action. "
                            "Split multi-action work across separate array entries."
                        )
                    }
                },
                "required": ["action"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        ms = agent_context.get('agent_state')
        if ms is None:
            return {"error": "Agent state is not enabled for this agent."}
        return ms.update_tasks(
            action=arguments.get('action', ''),
            task_id=arguments.get('task_id'),
            text=arguments.get('text'),
            tasks=arguments.get('tasks'),
        )

    return tool_def, executor





def _builtin_save_plan_factory(agent_context: dict):
    """Factory for the built-in 'save_plan' tool.

    Writes a markdown plan file to the agent's personal plan/ directory
    (agents/<agent-id>/plan/) and links it to the agent state so the content
    is re-injected on every subsequent LLM call.
    Available in both plan and execute modes (not in ARG_GUARDED_TOOLS).
    """
    import os

    agent_id = agent_context.get('id', '')
    # Resolve plan/ directory under the agent's own directory
    _base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    plan_dir = os.path.join(_base_dir, 'agents', agent_id, 'plan')

    tool_def = {
        "type": "function",
        "function": {
            "name": "save_plan",
            "description": (
                "Save a markdown plan to your plan/ directory and link it to your agent state. "
                "Call before set_mode('execute') or to update mid-execution. "
                "The plan is re-injected into your context on every turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename for the plan, e.g. 'runpy-heuristic-detection.md'. No slashes."
                    },
                    "content": {
                        "type": "string",
                        "description": "Full markdown content of the plan."
                    }
                },
                "required": ["filename", "content"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        ms = agent_context.get('agent_state')
        if ms is None:
            return {"error": "Agent state is not enabled for this agent."}

        # ATG enforcement: flagged agents on complex tasks must compile a task
        # graph first — small models ignore the prompt instruction alone.
        # save_plan unlocks once a compile was attempted (any atg status,
        # including 'failed') or for trivial-classified tasks.
        if (agent_context.get('enable_atg')
                and not getattr(ms, 'auto_trivial', False)
                and not getattr(ms, 'atg', None)):
            return {"error": (
                "This agent plans with Atomic Task Graph. Call "
                "compile_task_graph(goal, context) with the task goal instead "
                "of save_plan — the compiled graph becomes your plan file. "
                "save_plan is only available if graph compilation fails."
            )}

        filename = arguments.get('filename', '').strip()
        content = arguments.get('content', '')

        if not filename:
            return {"error": "'filename' must be a non-empty string."}
        if '/' in filename or '\\' in filename or '..' in filename:
            return {"error": "'filename' must be a bare filename with no slashes (e.g. 'my-plan.md')."}
        if not filename.endswith('.md'):
            filename += '.md'

        os.makedirs(plan_dir, exist_ok=True)
        file_path = os.path.join(plan_dir, filename)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            return {"error": f"Failed to write plan file: {e}"}

        relative_path = f"plan/{filename}"
        ms.set_plan_file(relative_path)
        return {
            "result": "Plan saved. Make sure to present this plan to user first.",
            "plan_file": relative_path
        }

    return tool_def, executor


def _builtin_compile_task_graph_factory(agent_context: dict):
    """Factory for the built-in 'compile_task_graph' tool (ATG).

    Compiles a complex task into a DAG of atomic tool-use nodes via recursive
    LLM decomposition (arXiv 2607.01942), stores it in agent_state.atg, and
    writes a markdown rendering as the linked plan file — so the existing
    save_plan/set_mode approval flow works unchanged. Exposed only when
    agent_context['enable_atg'] (gated in get_builtin_tools).
    """
    import os
    import re as _re

    agent_id = agent_context.get('id', '')
    _base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    plan_dir = os.path.join(_base_dir, 'agents', agent_id, 'plan')

    tool_def = {
        "type": "function",
        "function": {
            "name": "compile_task_graph",
            "description": (
                "Compile a complex multi-step task into an executable task graph "
                "(DAG of atomic tool-use steps with explicit dependencies). "
                "Prefer this over a free-form save_plan for complex tasks: after "
                "exploring, call compile_task_graph with the task goal. The graph "
                "is saved as your plan file — present it to the user and wait for "
                "approval before set_mode('execute'). Independent steps will run "
                "in parallel and failures are repaired locally during execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "The full task goal to compile, in one or two sentences."
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional findings from your exploration that the compiler "
                            "should know (relevant file paths, constraints, decisions)."
                        )
                    }
                },
                "required": ["goal"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        if not agent_context.get('enable_atg'):
            return {"error": "ATG is not enabled for this agent."}
        ms = agent_context.get('agent_state')
        if ms is None:
            return {"error": "Agent state is not enabled for this agent."}
        runtime = agent_context.get('_atg_runtime')
        if not runtime:
            return {"error": "ATG runtime is not available in this context."}

        goal = (arguments.get('goal') or '').strip()
        if not goal:
            return {"error": "'goal' must be a non-empty string."}

        from backend.agent_runtime.atg.compiler import (
            CompilationError, compile_task_graph, render_markdown)
        try:
            dag, history = compile_task_graph(
                goal,
                runtime.get('tools') or [],
                runtime['llm'],
                runtime['llm_lock'],
                log_file=runtime.get('llm_log_path'),
                context_excerpt=(arguments.get('context') or '')[:4000],
            )
        except CompilationError as e:
            # Mark the attempt so save_plan's ATG redirect unlocks as fallback.
            # root_goal kept for the re-arm continuation check.
            ms.atg = {"status": "failed", "error": str(e)[:500], "root_goal": goal}
            return {"error": f"Task graph compilation failed: {e}. "
                             "You can retry with a clearer goal, or fall back to save_plan."}

        waves = dag.waves()
        ms.atg = {
            "status": "compiled",
            "dag": dag.to_dict(),
            "history": history.to_dict(),
            "repair_attempts": 0,
            "stats": {"nodes_total": len(dag.nodes), "waves": len(waves)},
        }

        try:
            from backend.event_stream import event_stream
            event_stream.emit('atg_compiled', {
                'agent_id': agent_id,
                'session_id': agent_context.get('session_id'),
                'nodes': len(dag.nodes), 'waves': len(waves),
                'refinements': max(0, len(history.entries) - 1),
            })
        except Exception:
            pass

        slug = _re.sub(r'[^a-z0-9]+', '-', goal.lower()).strip('-')[:40] or 'task'
        filename = f"atg-{slug}.md"
        os.makedirs(plan_dir, exist_ok=True)
        try:
            with open(os.path.join(plan_dir, filename), 'w', encoding='utf-8') as f:
                f.write(render_markdown(dag, history))
        except Exception as e:
            return {"error": f"Failed to write plan file: {e}"}
        ms.set_plan_file(f"plan/{filename}")

        return {
            "result": (
                f"Task graph compiled: {len(dag.nodes)} nodes in {len(waves)} waves. "
                "Saved as your plan file — present the plan to the user and wait "
                "for approval before set_mode('execute')."
            ),
            "plan_file": f"plan/{filename}",
            "nodes": len(dag.nodes),
            "waves": len(waves),
        }

    return tool_def, executor


def _cmp_emit(agent_context: dict, event: str, payload: dict) -> None:
    """Best-effort CMP event emission with session context."""
    try:
        from backend.event_stream import event_stream
        event_stream.emit(event, {
            'agent_id': agent_context.get('id', ''),
            'session_id': agent_context.get('session_id'),
            **payload,
        })
    except Exception:
        pass


def _cmp_gate(agent_context: dict):
    """Common gate for CMP tools. Returns (ms, None) or (None, error_dict)."""
    if not agent_context.get('enable_cmp'):
        return None, {"error": "CMP is not enabled for this agent."}
    ms = agent_context.get('agent_state')
    if ms is None:
        return None, {"error": "Agent state is not enabled for this agent."}
    return ms, None


def _cmp_finalize_outgoing(agent_context: dict, ms) -> None:
    """Best-effort card finalization for the active path before it is
    suspended (the paper's card-first ordering). Never blocks the switch."""
    try:
        from models.chatlog import chatlog_manager
        from backend.agent_runtime.cmp.compactor import finalize_active_card
        chatlog = chatlog_manager.get(
            agent_context.get('_db_agent_id', agent_context.get('id', '')),
            agent_context.get('session_id'))
        finalize_active_card(chatlog, ms.cmp, ms)
    except Exception:
        pass


def _builtin_switch_path_factory(agent_context: dict):
    """Factory for the built-in 'switch_path' tool (CMP navigation).

    Validated request: the harness checks the target against the session
    graph; unknown ids return an error listing valid ids (grounding the node
    inventory) instead of executing. Exposed only when enable_cmp.
    """
    tool_def = {
        "type": "function",
        "function": {
            "name": "switch_path",
            "description": (
                "Resume another task path from the session map. Use when the "
                "user returns to an earlier task (e.g. 'back to the website'). "
                "Restores that path's plan/task-graph state and marks the "
                "current path preserved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path_id": {
                        "type": "string",
                        "description": "Target path id from the session map, e.g. 'P1'."
                    }
                },
                "required": ["path_id"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        ms, err = _cmp_gate(agent_context)
        if err:
            return err
        if not ms.cmp or not ms.cmp.get("paths"):
            return {"error": "No session paths exist yet. Use new_path(title) to start one."}
        from backend.agent_runtime.cmp import store as cmp_store
        target_id = (arguments.get('path_id') or '').strip()
        old_id = ms.cmp.get("active_id")
        _cmp_finalize_outgoing(agent_context, ms)  # card-first ordering
        try:
            target = cmp_store.switch_to(ms.cmp, ms, target_id)
        except ValueError as e:
            return {"error": str(e)}
        _cmp_emit(agent_context, 'cmp_path_switched',
                  {'from': old_id, 'to': target_id, 'initiator': 'agent'})
        return {
            "result": f"Switched to {target_id} — {target.get('title')}. "
                      "Its plan/task state has been restored.",
            "path": {k: target.get(k) for k in
                     ("id", "title", "goal", "outcome", "key_facts", "artifacts")},
        }

    return tool_def, executor


def _builtin_new_path_factory(agent_context: dict):
    """Factory for the built-in 'new_path' tool (CMP navigation).

    Starts a separate task path in execute or plan mode based on complexity.
    depends_on records that the new task consumes results of existing paths.
    """
    tool_def = {
        "type": "function",
        "function": {
            "name": "new_path",
            "description": (
                "Start a NEW task as its own session path when the user "
                "switches to different work (not a follow-up of the current "
                "task). Use depends_on when the new task builds on results "
                "of existing paths (e.g. an invoice for a project built in P1)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short title for the new task path (<= 60 chars)."
                    },
                    "goal": {
                        "type": "string",
                        "description": "One-sentence goal of the new task."
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Path ids whose results this task uses, e.g. ['P1']."
                    }
                },
                "required": ["title"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        ms, err = _cmp_gate(agent_context)
        if err:
            return err
        title = (arguments.get('title') or '').strip()
        if not title:
            return {"error": "'title' must be a non-empty string."}
        from backend.agent_runtime.cmp import store as cmp_store
        if ms.cmp is None or not ms.cmp.get("paths"):
            # Adopt the ongoing work as P1 before branching off it.
            prev_title = None
            if isinstance(ms.atg, dict):
                prev_title = ((ms.atg.get('dag') or {}).get('root_goal')
                              or ms.atg.get('root_goal'))
            prev_title = (prev_title or ms.plan_file or "Earlier conversation")
            ms.cmp = cmp_store.new_cmp(ms, title=str(prev_title)[:60])
            _cmp_emit(agent_context, 'cmp_path_created',
                      {'path_id': 'P1', 'title': str(prev_title)[:60],
                       'initiator': 'auto-init'})
        goal = (arguments.get('goal') or '').strip()
        from backend.task_classifier import classify_task
        trivial = classify_task(goal or title) == 'trivial'
        _cmp_finalize_outgoing(agent_context, ms)  # card-first ordering
        try:
            record = cmp_store.create_path(
                ms.cmp, ms, title, goal=goal,
                depends_on=arguments.get('depends_on') or [], trivial=trivial)
        except ValueError as e:
            return {"error": str(e)}
        _cmp_emit(agent_context, 'cmp_path_created',
                  {'path_id': record['id'], 'title': record['title'],
                   'depends_on': record['depends_on'], 'initiator': 'agent'})
        mode_note = "execute mode" if trivial else "plan mode"
        return {
            "result": (
                f"Started {record['id']} — {record['title']}. The previous "
                "path is preserved (resumable via switch_path). You are now in "
                f"{mode_note} for this new task."
            ),
            "path_id": record['id'],
        }

    return tool_def, executor


def _builtin_read_transcript_factory(agent_context: dict):
    """Factory for the built-in 'read_transcript' tool (CMP retrieval).

    Fallback for when a path's waypoint card lacks a detail the user asks for:
    returns a compact digest (user turns + agent replies) of an offloaded
    path's own transcript segments, WITHOUT switching to it. Covers facts
    produced in replies that the compactor did not lift into key_facts.
    """
    tool_def = {
        "type": "function",
        "function": {
            "name": "read_transcript",
            "description": (
                "Retrieve a compact digest of an earlier task path's own "
                "conversation when its card on the map lacks a detail you need "
                "(e.g. a specific value discussed there). Reads that path only, "
                "without switching to it. Use for recap/compare questions about "
                "an offloaded or archived path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path_id": {
                        "type": "string",
                        "description": "Path id to read from the session map, e.g. 'A3'."
                    }
                },
                "required": ["path_id"]
            }
        }
    }

    def executor(arguments: dict) -> dict:
        ms, err = _cmp_gate(agent_context)
        if err:
            return err
        if not ms.cmp or not ms.cmp.get("paths"):
            return {"error": "No session paths exist yet."}
        path_id = (arguments.get('path_id') or '').strip().upper()
        path = ms.cmp["paths"].get(path_id)
        if not path:
            valid = ", ".join(sorted(ms.cmp["paths"]))
            return {"error": f"Unknown path id '{path_id}'. Valid ids: {valid}."}
        try:
            from models.chatlog import chatlog_manager
            from backend.agent_runtime.cmp.compactor import collect_path_entries
            chatlog = chatlog_manager.get(
                agent_context.get('_db_agent_id', agent_context.get('id', '')),
                agent_context.get('session_id'))
            entries = collect_path_entries(chatlog, path)
        except Exception as e:
            return {"error": f"Failed to read transcript for {path_id}: {e}"}

        # Compact digest: user turns + agent replies only (drop tool-call noise),
        # each truncated, bounded to a token budget so it stays far cheaper than
        # rehydrating the raw transcript.
        BUDGET_CHARS, PER_MSG = 4000, 500
        lines, used = [], 0
        from backend.agent_runtime.context import (
            attachment_infos_from_metadata, build_attachment_notes,
        )
        for e in entries:
            if e.get('type') not in ('user', 'final', 'intermediate'):
                continue
            content = (e.get('content') or '').strip()
            if e.get('type') == 'user':
                infos = attachment_infos_from_metadata(e.get('metadata') or {})
                if infos:
                    content = content.rstrip() + build_attachment_notes(
                        infos, has_describe_image=False, audio_enabled=False)
            if not content:
                continue
            role = 'User' if e.get('type') == 'user' else 'Agent'
            snippet = f"{role}: {content[:PER_MSG]}"
            if used + len(snippet) > BUDGET_CHARS:
                lines.append("… [transcript truncated]")
                break
            lines.append(snippet)
            used += len(snippet)
        digest = "\n".join(lines) or "(no readable transcript for this path)"
        return {
            "result": f"Transcript digest of {path_id} — {path.get('title')}:\n\n{digest}",
            "path_id": path_id,
        }

    return tool_def, executor


def _builtin_remember_factory(agent_context: dict):
    """Factory for the built-in 'remember' tool — stores a fact in long-term memory."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Store a fact in long-term memory. "
                "Provide `key` for facts with a stable identity (a preference, a "
                "decision, a setting) — remembering the same key again REPLACES "
                "the old value instead of piling up. Reuse an existing key from "
                "'Known memory keys' when one fits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact to remember as a single clear sentence."
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "Stable dot-path identity for this fact, e.g. "
                            "'user.deploy_target', 'preference.language', "
                            "'decision.database'. Same key = the new fact "
                            "supersedes the old one. Omit for one-off episodic "
                            "facts with no natural key."
                        )
                    },
                    "category": {
                        "type": "string",
                        "enum": ["user_info", "preference", "decision",
                                 "context", "instruction", "general"],
                        "description": "Category for this memory (default: general; ignored when `key` is given — derived from the key's first segment)."
                    }
                },
                "required": ["content"]
            }
        }
    }

    def executor(args: dict) -> dict:
        from backend.agent_runtime.memory_manager import store_memory
        agent_id = agent_context.get('id', '')
        session_id = agent_context.get('session_id', '')
        return store_memory(agent_id, session_id,
                            args.get('content', ''),
                            args.get('category', 'general'),
                            key=args.get('key'))

    return tool_def, executor


def _builtin_recall_factory(agent_context: dict):
    """Factory for the built-in 'recall' tool — searches long-term memory.

    One tool, five modes:
      - fts   (default): fast keyword search over remembered facts
      - key             : exact-key point lookup of the current value of a keyed
                          fact ('query' is the key, e.g. 'user.deploy_target')
      - think           : reason over everything known about a topic (synthesis
                          with citations + knowledge gaps)
      - graph           : traverse the entity knowledge graph from an entity
      - links           : the link neighborhood of a KB document (outgoing/incoming
                          references, dangling links, same-tag docs); 'query' is the
                          KB filename
    """
    from backend.agent_runtime.evomem_writer import EDGE_TYPES
    _edge_enum = sorted(EDGE_TYPES)
    tool_def = {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "Search long-term memory. "
                "See system prompt Memory Retrieval Protocol for mode details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search for; the memory key (e.g. 'user.deploy_target') when mode='key'; the entity name when mode='graph'; or the KB filename (e.g. 'evonic.md') when mode='links'."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["fts", "key", "think", "graph", "links"],
                        "description": "Retrieval mode (default: fts). Use 'key' for the current value of a known memory key — fastest and most precise."
                    },
                    "edge_type": {
                        "type": "string",
                        "enum": _edge_enum,
                        "description": "Only for mode='graph': only follow edges of this type."
                    },
                    "hops": {
                        "type": "integer",
                        "description": "Only for mode='graph': how many hops to traverse (default 2)."
                    }
                },
                "required": ["query"]
            }
        }
    }

    def executor(args: dict) -> dict:
        from backend.agent_runtime.memory_manager import (
            search_memories, synthesize_memory, graph_lookup, recall_by_key,
        )
        agent_id = agent_context.get('id', '')
        query = args.get('query', '')
        mode = args.get('mode', 'fts')

        def _augment(result):
            # Surface matching task paths from THIS session's CMP graph so an
            # offloaded/archived path's facts are recallable even when the
            # per-turn detector did not pin it. Applies to EVERY recall mode
            # (the agent mostly uses mode='think'). Additive — never breaks the
            # underlying memory result.
            try:
                if not agent_context.get('enable_cmp'):
                    return result
                ms = agent_context.get('agent_state')
                cmp = getattr(ms, 'cmp', None) if ms is not None else None
                if not (cmp and cmp.get('paths')):
                    return result
                from backend.agent_runtime.cmp.store import search_cmp_paths
                hits = search_cmp_paths(cmp, query, limit=5)
                if hits:
                    result = dict(result or {})
                    result['session_paths'] = hits
                    result['session_paths_hint'] = (
                        "Matches from the current session's task map. For the "
                        "full detail of one, call read_transcript(path_id) or "
                        "switch_path(path_id).")
            except Exception:
                pass
            return result

        if mode == 'key':
            return _augment(recall_by_key(agent_id, query))
        if mode == 'think':
            return _augment(synthesize_memory(agent_id, query))
        if mode == 'graph':
            return _augment(graph_lookup(
                agent_id, query,
                edge_type=args.get('edge_type'),
                hops=int(args.get('hops', 2) or 2),
            ))
        if mode == 'links':
            from backend.tools.kb_graph import execute as kb_graph_execute
            return _augment(kb_graph_execute(agent_context, {'filename': query}))
        return _augment(search_memories(agent_id, query))

    return tool_def, executor


def _builtin_forget_memory_factory(agent_context: dict):
    """Factory for the built-in 'forget_memory' tool — soft-deletes a long-term memory."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": (
                "Soft-delete a long-term memory by ID so it no longer appears in recall results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "The ID of the memory to delete."
                    },
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "The agent whose memory to delete. Defaults to yourself. "
                            "Only super agents can delete another agent's memories."
                        )
                    }
                },
                "required": ["memory_id"]
            }
        }
    }

    def executor(args: dict) -> dict:
        from backend.agent_runtime.memory_manager import forget_memory
        agent_id = agent_context.get('id', '')
        is_super = bool(agent_context.get('is_super', False))
        return forget_memory(
            agent_id=agent_id,
            memory_id=args.get('memory_id'),
            target_agent_id=args.get('agent_id'),
            is_super=is_super,
        )

    return tool_def, executor


def _builtin_state_factory(agent_context: dict):
    """Factory for the built-in 'state' tool (agent state machine gate).

    Allows the LLM to query or transition its workflow state. Handlers are
    registered by system components and plugins via register_state_handler().
    """
    tool_def = {
        "type": "function",
        "function": {
            "name": "state",
            "description": (
                "Query or transition your workflow state. "
                "Call with no args for a summary; with label to request a state transition. "
                "See Agent State section for details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "State transition label in 'namespace:action' format, "
                            "e.g. 'kanban:pick', 'kanban:activate', 'kanban:finish'."
                        )
                    },
                    "data": {
                        "description": "Optional data payload for the transition (any JSON value)."
                    }
                },
                "required": []
            }
        }
    }

    def executor(arguments: dict) -> dict:
        from backend.plugin_manager import dispatch_state, get_state_summary
        from backend.event_stream import event_stream

        ms = agent_context.get('agent_state')
        label = arguments.get('label')
        data = arguments.get('data')

        # No label → return current state summary
        if not label:
            return get_state_summary(ms)

        # With label → dispatch to registered handler
        agent_id = agent_context.get('agent_id', agent_context.get('id', ''))
        session_id = agent_context.get('session_id', '')
        result = dispatch_state(agent_id, session_id, ms, label, data)

        # On success, persist the new state into AgentState
        if result.get('result') == 'success' and ms is not None:
            namespace = result.get('namespace', label.split(':')[0])
            new_state = result.get('state', '')
            if new_state:
                ms.set_state(
                    namespace=namespace,
                    state=new_state,
                    data=result.get('data'),
                    blocked_tools=result.get('blocked_tools'),
                    allowed_tools=result.get('allowed_tools'),
                )
            else:
                # Handler signalled state cleared (e.g. finish/done)
                ms.clear_state(namespace)

            event_stream.emit('state_transition', {
                'agent_id': agent_id,
                'session_id': session_id,
                'namespace': namespace,
                'label': label,
                'new_state': new_state,
                'data': data,
            })

        return result

    return tool_def, executor


def _builtin_recall_sessions_factory(agent_context: dict):
    """Factory for the built-in 'recall_sessions' tool — queries session summaries from DB."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "recall_sessions",
            "description": (
                "Recall session summaries from previous conversations. "
                "Leave query empty for recent sessions; use a keyword to search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword (e.g. 'login bug', 'kanban'). Leave empty to get all sessions."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of sessions to return (default: 20, max: 50)."
                    }
                },
                "required": []
            }
        }
    }

    def executor(args: dict) -> dict:
        from models.db import db
        agent_id = agent_context.get('id', '')
        query = args.get('query', '')
        limit = min(args.get('limit', 20), 50)

        summaries = db.get_agent_summaries(agent_id, query=query, limit=limit)

        if not summaries:
            return {"result": "No session summaries found."}

        # Format as markdown
        lines = [f"## Session Summaries", f"\nFound {len(summaries)} session(s):\n"]
        for s in summaries:
            date = s.get("created_at", "")[:10] if s.get("created_at") else "?"
            channel = s.get("channel_id") or "web"
            msg_count = s.get("message_count", 0)
            session_id = s.get("session_id", "?")
            summary_text = s.get("summary", "")

            # Extract session ID short form (last part after the dash)
            short_id = session_id.split('-')[-1][:8] if '-' in session_id else session_id[:8]

            lines.append(f"### Session {short_id} ({channel}, {date})")
            lines.append(f"- Messages: {msg_count}")
            lines.append("")
            lines.append(summary_text)
            lines.append("")

        return {"result": "\n".join(lines)}

    return tool_def, executor


def _builtin_reset_active_model_factory(agent_context: dict):
    """Factory for the built-in 'reset_active_model' tool.

    Clears the active fallback model flag from agent_state so the agent
    returns to its configured primary/default model on the next turn.
    """
    tool_def = {
        "type": "function",
        "function": {
            "name": "reset_active_model",
            "description": (
                "Clears the active fallback model flag from agent_state. "
                "After calling this, the agent will use its configured "
                "primary/default model on the next turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }

    def executor(arguments: dict) -> dict:
        agent_id = agent_context.get('id', '')
        if not agent_id:
            return {"error": "Agent ID not available in context."}
        from models.chat import agent_chat_manager
        import json
        try:
            _db = agent_chat_manager.get(agent_id)
            _raw = _db.get_agent_state()
            if not _raw:
                return {"result": "No agent state found — nothing to reset."}
            _data = json.loads(_raw)
            if 'active_fallback_model_id' not in _data:
                return {"result": "No active fallback model to reset."}
            fb_id = _data.pop('active_fallback_model_id', None)
            _db.upsert_agent_state(json.dumps(_data))
            return {
                "result": (
                    f"Fallback model ({fb_id}) has been cleared. "
                    "The agent will use its primary model on the next turn."
                )
            }
        except Exception as e:
            return {"error": f"Failed to reset active model: {str(e)}"}

    return tool_def, executor
