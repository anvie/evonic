"""
Tool Registry — discovers and manages tool backends with auto-reload.

In production mode, tools execute real Python backends from backend/tools/.
In eval mode, tools return mock responses from tools/ JSON files.
Built-in tools (like 'read') are registered separately with agent context.
Skills extend the registry with additional tool definitions and backends.
"""

import os
import sys
import json
import importlib
import importlib.util
from typing import Dict, Any, Optional, Callable, List

# Directory containing tool backend Python files
TOOLS_DIR = os.path.join(os.path.dirname(__file__))
# Directory containing tool definition JSON files (for eval mock responses)
TOOL_DEFS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'tools')


class ToolRegistry:
    def __init__(self):
        # Cache: tool_name -> { module, mtime, path }
        self._module_cache: Dict[str, dict] = {}
        # Built-in tool factories: builtin_id -> callable(agent_context) -> tool_def_and_executor
        # IDs use 'builtin:' namespace prefix (e.g. 'builtin:read')
        self._builtins: Dict[str, Callable] = {}
        # Register the built-in 'read' tool
        self._builtins['builtin:read'] = _builtin_read_factory
        self._builtins['builtin:clear_log_file'] = _builtin_clear_log_factory
        # Register the built-in 'use_skill' and 'unload_skill' tools
        self._builtins['builtin:use_skill'] = _builtin_use_skill_factory
        self._builtins['builtin:unload_skill'] = _builtin_unload_skill_factory
        # Register agent-state tools (active only when agent has enable_agent_state)
        self._builtins['builtin:set_mode'] = _builtin_set_mode_factory
        self._builtins['builtin:update_tasks'] = _builtin_update_tasks_factory
        self._builtins['builtin:save_plan'] = _builtin_save_plan_factory
        # State machine gate tool — always available, handlers registered by system/plugins
        self._builtins['builtin:state'] = _builtin_state_factory
        # Long-term memory tools
        self._builtins['builtin:remember'] = _builtin_remember_factory
        self._builtins['builtin:recall'] = _builtin_recall_factory
        self._builtins['builtin:forget_memory'] = _builtin_forget_memory_factory
        # Session recall tool
        self._builtins['builtin:recall_sessions'] = _builtin_recall_sessions_factory

    def get_tool_defs_from_json(self) -> List[Dict[str, Any]]:
        """Load tool definitions from tools/*.json (for eval & agent config UI)."""
        tools = []
        defs_dir = os.path.normpath(TOOL_DEFS_DIR)
        if not os.path.isdir(defs_dir):
            return tools
        for fname in sorted(os.listdir(defs_dir)):
            if not fname.endswith('.json'):
                continue
            with open(os.path.join(defs_dir, fname)) as f:
                try:
                    tools.append(json.load(f))
                except json.JSONDecodeError:
                    pass
        return tools

    def get_all_tool_defs(self) -> List[Dict[str, Any]]:
        """Load tool definitions from both tools/ and enabled skills."""
        from backend.skills_manager import skills_manager
        all_defs = self.get_tool_defs_from_json()
        # Add skill tool definitions
        skill_defs = skills_manager.get_all_skill_tool_defs()
        all_defs.extend(skill_defs)
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

        # Build function_name -> skill_id mapping from assigned tool IDs
        fn_to_skill: Dict[str, str] = {}
        for tid in ctx.get('assigned_tool_ids', []):
            if tid.startswith('skill:'):
                parts = tid.split(':', 2)  # skill:skill_id:fn_name
                if len(parts) == 3:
                    fn_to_skill[parts[2]] = parts[1]

        def real_executor(function_name: str, arguments: dict) -> dict:
            # Authorization guard: tool must be in assigned_tool_ids
            _assigned = set(ctx.get('assigned_tool_ids', []))
            if function_name not in _assigned:
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
            _self_path_args = {'write_file', 'str_replace', 'patch', 'file_edit', 'file_create'}
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
            module = self._load_tool_module(function_name, skill_id=skill_id)
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

    def get_builtin_tool_defs(self) -> List[Dict[str, Any]]:
        """Return UI-facing tool definitions for all built-in tools (with _builtin metadata)."""
        defs = []
        for builtin_id, factory in self._builtins.items():
            tool_def, _ = factory({'agent_id': ''})
            fn = tool_def.get('function', {})
            defs.append({
                'id': builtin_id,          # e.g. 'builtin:read'
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
            tool_def, executor = factory(agent_context)
            fn_name = tool_def['function']['name']  # e.g. 'read'
            executors[fn_name] = executor

        def builtin_executor(function_name: str, arguments: dict) -> dict:
            if function_name in executors:
                try:
                    return executors[function_name](arguments)
                except Exception as e:
                    return {"error": f"Built-in tool error: {str(e)}"}
            return None  # Not a built-in — fall through

        return builtin_executor

    def _load_tool_module(self, tool_name: str, skill_id: str = None):
        """Load (or reload) a tool's Python module from backend/tools/ or skills/*/backend/tools/.

        Args:
            tool_name: Function name of the tool.
            skill_id: If provided, prefer this skill's backend over others.
        """
        tool_path = os.path.join(TOOLS_DIR, f"{tool_name}.py")
        skill_backend_dir = None

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

        if not os.path.isfile(tool_path):
            # Search in skills (no skill_id hint)
            from backend.skills_manager import skills_manager
            tool_path = skills_manager.find_tool_backend_path(tool_name)
            if tool_path is None:
                return None
            skill_dir = skills_manager.find_tool_skill_dir(tool_name)
            if skill_dir:
                skill_backend_dir = os.path.join(skill_dir, 'backend')

        current_mtime = os.path.getmtime(tool_path)
        cache_key = f"{tool_name}:{skill_id}" if skill_id else tool_name
        cached = self._module_cache.get(cache_key)

        if cached and cached['mtime'] == current_mtime and cached['path'] == tool_path:
            return cached['module']

        # Temporarily add skill backend dir to sys.path for relative imports
        added_path = False
        if skill_backend_dir and skill_backend_dir not in sys.path:
            sys.path.insert(0, skill_backend_dir)
            added_path = True

        try:
            spec = importlib.util.spec_from_file_location(f"tools.{tool_name}", tool_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if added_path:
                sys.path.remove(skill_backend_dir)

        self._module_cache[cache_key] = {
            'module': module,
            'mtime': current_mtime,
            'path': tool_path
        }
        return module


def _builtin_read_factory(agent_context: dict):
    """Factory for the built-in 'read' tool scoped to an agent's KB directory."""
    agent_id = agent_context.get('id', '')
    workplace_id = agent_context.get('workplace_id')
    # KB files always live on the evonic server at agents/{agent_id}/kb/.
    # The agent's workspace path is where bash/runpy tools execute — it
    # has nothing to do with where KB files are stored.
    base_dir = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..', 'agents', agent_id, 'kb'
    ))

    # Tailor description for remote agents who see /_self/kb/ in their system prompt
    _is_remote = bool(workplace_id)
    _desc = (
        "Read a file from this agent's knowledge base (KB). "
        + ("Pass a bare filename (e.g. 'notes.md') or a /_self/ path (e.g. '/_self/kb/notes.md'). "
           if _is_remote else
           "Pass a bare filename only — no paths (e.g. 'notes.md', not '/kb/notes.md'). ")
        + "This tool is ONLY for KB files. "
        "To read any other file (source code, logs, workspace files), use read_file instead."
    )
    _param_desc = (
        "Bare KB filename (e.g. 'notes.md') or /_self/ path (e.g. '/_self/kb/notes.md')."
        if _is_remote else
        "Bare KB filename, e.g. 'notes.md'. No slashes or paths."
    )
    tool_def = {
        "type": "function",
        "function": {
            "name": "read",
            "description": _desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": _param_desc
                    }
                },
                "required": ["filename"]
            }
        }
    }

    def executor(args: dict) -> dict:
        filename = args.get('filename', '')

        # /_self/ path: resolve to the agent's local directory on the evonic server.
        # Remote agents get /_self/kb/ injected into their system prompt, so the
        # LLM naturally passes /_self/kb/notes.md here.  Handle it like the other
        # file tools (read_file, write_file, etc.) do.
        from backend.tools._workspace import is_self_path, resolve_self_path
        _agent_id = agent_context.get('id', '')
        if _agent_id and is_self_path(filename):
            resolved = resolve_self_path(_agent_id, filename)
            if not resolved:
                return {"error": "Access denied — path escapes agent directory."}
            if not os.path.isfile(resolved):
                return {"error": f"File not found: {filename}"}
            try:
                with open(resolved, 'r', encoding='utf-8') as f:
                    content = f.read()
                return {"filename": filename, "content": content}
            except Exception as e:
                return {"error": f"Read error: {str(e)}"}

        # Security: only bare filenames allowed
        if '/' in filename or '\\' in filename or '..' in filename:
            return {"error": "This tool only reads KB files by bare filename (e.g. 'notes.md'). To read workspace or other files use the read_file tool instead."}
        filepath = os.path.join(base_dir, filename)
        filepath = os.path.normpath(filepath)
        # Double-check we're still inside the KB dir
        if not filepath.startswith(base_dir):
            return {"error": "Access denied."}
        if not os.path.isfile(filepath):
            return {"error": f"File not found: {filename}"}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            return {"filename": filename, "content": content}
        except Exception as e:
            return {"error": f"Read error: {str(e)}"}

    return tool_def, executor


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
                "Lazy-load a skill's SYSTEM.md knowledge into the agent context. "
                "Only works for lazy-loaded skills (eager skills' tools are already available). "
                "Use this when you need to understand a skill's capabilities before using it. "
                "Example: use_skill({id: 'kanban'})"
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
                "Unload a previously lazy-loaded skill, removing its tools from the current context. "
                "Only works for lazy-loaded skills — eager skills' tools are always available. "
                "Use this after you are done with a skill to keep the context clean. "
                "Example: unload_skill({id: 'plugin_creator'})"
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
                "Transition the agent's working mode. "
                "Use 'plan' during planning — write tools (write_file, patch, file_edit, file_create) are blocked. "
                "Use 'execute' after the user approves the plan — write tools become available. "
                "Always present your plan to the user and wait for approval before switching to 'execute'."
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
        return ms.set_mode(arguments.get('mode', ''), reason=arguments.get('reason'))

    return tool_def, executor


def _builtin_update_tasks_factory(agent_context: dict):
    """Factory for the built-in 'update_tasks' tool (mental state task list management)."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "update_tasks",
            "description": (
                "Manage the task list that tracks implementation progress. "
                "Use 'set' to define the full plan as a task list. "
                "Use 'add' to append a single task. "
                "Use 'done' or 'in_progress' to update task status. "
                "Use 'remove' to delete a task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "add", "done", "in_progress", "remove"],
                        "description": (
                            "'set': replace entire task list (provide 'tasks' array). "
                            "'add': add one task (provide 'text'). "
                            "'done'/'in_progress': update status (provide 'task_id'). "
                            "'remove': delete a task (provide 'task_id')."
                        )
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "Task ID for done/in_progress/remove actions."
                    },
                    "text": {
                        "type": "string",
                        "description": "Task description for the 'add' action."
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task descriptions for the 'set' action."
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


def _extract_tasks_from_markdown(content: str) -> list:
    """Extract task items from markdown content for auto-populating AgentState.tasks."""
    import re
    tasks = []
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r'^(?:[-*]|\d+[.)]) \s*(?:\[.\]\s*)?(.+)$', line)
        if m:
            text = m.group(1).strip()
            if text and not text.startswith('#'):
                tasks.append(text)
    return tasks


def _builtin_save_plan_factory(agent_context: dict):
    """Factory for the built-in 'save_plan' tool.

    Writes a markdown plan file to the plan/ directory and links it to the
    agent state so the content is re-injected on every subsequent LLM call.
    Available in both plan and execute modes (not in GUARDED_TOOLS).
    """
    import os

    # Resolve plan/ directory relative to project root
    plan_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'plan')
    )

    tool_def = {
        "type": "function",
        "function": {
            "name": "save_plan",
            "description": (
                "Save a markdown plan file to the plan/ directory and link it to your agent state. "
                "The plan content will be re-injected into your context on every turn, "
                "so you never lose your objective even after conversation summarization. "
                "You MUST call this before set_mode('execute'). "
                "To update the plan mid-execution, call save_plan again with the same filename."
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
        extracted = _extract_tasks_from_markdown(content)
        if extracted:
            ms.update_tasks("set", tasks=extracted)
        return {
            "result": "Plan saved. Make sure to present this plan to user first.",
            "plan_file": relative_path
        }

    return tool_def, executor


def _builtin_remember_factory(agent_context: dict):
    """Factory for the built-in 'remember' tool — stores a fact in long-term memory."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Store a fact in your long-term memory so it persists across future conversations. "
                "Use this when the user shares important information worth retaining "
                "(name, preferences, decisions, context, or persistent instructions). "
                "Example: remember(content='User prefers responses in English', category='preference')"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact to remember as a single clear sentence."
                    },
                    "category": {
                        "type": "string",
                        "enum": ["user_info", "preference", "decision",
                                 "context", "instruction", "general"],
                        "description": "Category for this memory (default: general)."
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
                            args.get('category', 'general'))

    return tool_def, executor


def _builtin_recall_factory(agent_context: dict):
    """Factory for the built-in 'recall' tool — searches long-term memory."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "Search your long-term memory for facts from past conversations. "
                "Use this when you need to recall something about the user or context "
                "that may not be in the current conversation history. "
                "Example: recall(query='user phone number')"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search for in memory."
                    }
                },
                "required": ["query"]
            }
        }
    }

    def executor(args: dict) -> dict:
        from backend.agent_runtime.memory_manager import search_memories
        agent_id = agent_context.get('id', '')
        return search_memories(agent_id, args.get('query', ''))

    return tool_def, executor


def _builtin_forget_memory_factory(agent_context: dict):
    """Factory for the built-in 'forget_memory' tool — soft-deletes a long-term memory."""
    tool_def = {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": (
                "Delete a specific memory from your long-term memory by its ID. "
                "The memory is soft-deleted (marked as expired) so it will no longer "
                "appear in recall results. Use this when a memory is no longer relevant "
                "or was stored incorrectly. "
                "Example: forget_memory(memory_id=42)"
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
                "Call with no arguments to see all current states and registered namespaces. "
                "Call with a label (and optional data) to request a state transition — "
                "the handler will validate the transition and return instructions on what to do next. "
                "Labels use 'namespace:action' format, e.g. 'kanban:pick', 'kanban:finish'."
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
                "Recall session summaries from previous conversations with this agent. "
                "Use without query to get all recent sessions. "
                "Use query to search for specific topics by keyword. "
                "Example: recall_sessions() or recall_sessions(query='login bug')"
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
