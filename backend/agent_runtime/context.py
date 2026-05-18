"""
context.py — builds LLM input: system prompt, tool list, message formatting.

Pure data preparation — no LLM calls, no threading.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

import tiktoken

_logger = logging.getLogger(__name__)

_TIKTOKEN_ENCODING = None


def _token_count(text: str) -> int:
    """Count tokens using tiktoken cl100k_base encoding."""
    global _TIKTOKEN_ENCODING
    if _TIKTOKEN_ENCODING is None:
        _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    return len(_TIKTOKEN_ENCODING.encode(text))

from models.db import db
from backend.tools import tool_registry
from backend.skills_manager import SkillsManager
from config import AGENT_MAX_TOOL_RESULT_CHARS as MAX_TOOL_RESULT_CHARS

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENTS_DIR = os.path.join(_BASE_DIR, 'agents')

# Per-agent cache for the static portion of build_system_prompt.
# Entries are invalidated when tracked file/dir mtimes change.
# Structure: { agent_id: { "static_prompt": str, "sp_mtime": float, "kb_mtime": float,
#                           "skills_mtimes": dict, "tools_hash": str, "ctx_mtime": float,
#                           "sandbox_enabled": int } }
_system_prompt_cache: Dict[str, Dict[str, Any]] = {}


def _effective_id(agent: Dict[str, Any]) -> str:
    """Return the agent ID to use for DB/disk resource lookups.

    Sub-agents don't exist in the agents table or agents/ directory.
    They inherit the parent's SYSTEM.md, KB files, tool assignments,
    and skill assignments.
    """
    if agent.get('is_subagent'):
        return agent.get('parent_id', agent['id'])
    return agent['id']


def _system_prompt_path(agent_id: str) -> str:
    return os.path.join(_AGENTS_DIR, agent_id, 'SYSTEM.md')


def _get_mtime(path: str) -> float:
    """Return mtime of a file or dir, or 0 if it doesn't exist."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _build_portal_info(agent_id: str) -> list:
    """Build per-agent portal virtual path listing for system prompt injection."""
    try:
        from models.db import db
        portals = db.get_agent_portals(agent_id)
    except Exception:
        _logger.warning("Failed to load portal info for agent %s", agent_id, exc_info=True)
        return []

    if not portals:
        return []

    lines = []
    for p in portals:
        vpath = p.get("virtual_path", "")
        backend_type = p.get("backend_type", "?")
        real_path = p.get("real_path", "")
        name = p.get("name", vpath)
        status = p.get("status", "disconnected")
        status_note = " (⚠ disconnected)" if status != "connected" else ""

        if backend_type == "local":
            lines.append(
                f"- `/_portal/{vpath}/` → `{real_path}` "
                f"(local filesystem{status_note}) — {name}"
            )
        elif backend_type == "ssh":
            lines.append(
                f"- `/_portal/{vpath}/` → `{real_path}` "
                f"(SSH remote{status_note}) — {name}"
            )
        elif backend_type == "evonet":
            lines.append(
                f"- `/_portal/{vpath}/` → `{real_path}` "
                f"(Evonet cloud{status_note}) — {name}"
            )
        else:
            lines.append(
                f"- `/_portal/{vpath}/` → `{real_path}` "
                f"({backend_type}{status_note}) — {name}"
            )

    return lines


def _build_static_prompt(agent: Dict[str, Any]) -> str:
    """Build the static portion of the system prompt (no datetime, no onboarding).

    This is cached per-agent and invalidated only when underlying files/dirs change.
    """
    parts = []
    aid = agent['id']
    eid = _effective_id(agent)  # parent's ID for sub-agents

    # Optionally inject agent ID at the top
    if agent.get('inject_agent_id'):
        parts.append(f"Your agent ID is: {aid}")

    # Read system prompt from file; fall back to DB value for backward compat
    sp_path = _system_prompt_path(eid)
    if os.path.isfile(sp_path):
        try:
            with open(sp_path, 'r', encoding='utf-8') as f:
                sp = f.read().strip()
            if sp:
                parts.append(sp)
        except Exception:
            pass
    elif agent.get('system_prompt'):
        parts.append(agent['system_prompt'])

    # Language preference injection
    _agent_lang = db.get_setting('agent_language')
    if _agent_lang:
        _lang_instructions = {
            'english': 'Always respond in English.',
            'indonesian': 'Always respond in Bahasa Indonesia.',
            'adaptive': 'Respond in the same language the user uses. If the user mixes languages, you may mix too.',
        }
        _lang_text = _lang_instructions.get(_agent_lang, '')
        if _lang_text:
            parts.append(f"\n## Language\n{_lang_text}")

    # Inject system_prompt from assigned tool definitions
    assigned_ids = set(db.get_agent_tools(eid))
    if assigned_ids:
        seen_fn_names = set()
        for tool_def in tool_registry.get_all_tool_defs():
            tool_id = tool_def.get('id', '')
            fn_name = tool_def.get('function', {}).get('name', '')
            if tool_id in assigned_ids or fn_name in assigned_ids:
                if fn_name in seen_fn_names:
                    continue
                seen_fn_names.add(fn_name)
                tool_prompt = tool_def.get('system_prompt', '').strip()
                if tool_prompt:
                    if not agent.get('sandbox_enabled'):
                        tool_prompt = tool_prompt.replace('/workspace/shared/agents/', '')
                        tool_prompt = tool_prompt.replace('/workspace', 'the agents working directory')
                    parts.append(tool_prompt)

    # List available KB files so the agent knows what it can read
    kb_dir = os.path.join(_AGENTS_DIR, eid, 'kb')
    if os.path.isdir(kb_dir):
        files = [f for f in sorted(os.listdir(kb_dir))
                 if os.path.isfile(os.path.join(kb_dir, f))]
        if files:
            parts.append("\n## Available Knowledge Files")
            parts.append("You can read these files using the `read` tool:")
            for f in files:
                size = os.path.getsize(os.path.join(kb_dir, f))
                parts.append(f"- {f} ({size / 1024:.1f} KB)")
            parts.append("")
            parts.append("### KB Usage")
            parts.append("- **Save**: Use `write_file` with path `/_self/kb/filename` to store a new KB file.")
            parts.append("- **Read**: Use the `read` tool with the bare filename (no path) to read a KB file.")
            parts.append("- **KB vs Remember**: Use `read` for reference documents, guides, and long-form content. Use `remember` for short, searchable facts you want to recall across conversations.")
            parts.append("- **Best practices**: Store structured reference material in KB (specs, API docs, conventions). Keep each file focused on one topic. Update KB files when information changes.")

            # Inject notes.md instructions only if notes.md exists in KB
            if 'notes.md' in files:
                parts.append("")
                parts.append("### Notes.md - User Preferences & Instructions")
                parts.append(
                    "You have a `notes.md` file in your KB. This file is your primary location "
                    "for storing your user's personal preferences, tastes, language preferences, "
                    "and communication style instructions."
                )
                parts.append("")
                parts.append("**Use notes.md for:**")
                parts.append("- User's preferred language (e.g., 'User prefers Bahasa Indonesia')")
                parts.append("- Communication style preferences (e.g., 'User likes concise answers', 'User dislikes emoji')")
                parts.append("- Personal instructions (e.g., 'Call the user Pak')")
                parts.append("- Tastes and preferences (e.g., 'User prefers bullet points over paragraphs')")
                parts.append("")
                parts.append("**Do NOT put in notes.md -- use `remember` instead:**")
                parts.append("- Factual/memorization data: addresses, phone numbers, email, birthday")
                parts.append("- Secret/sensitive data: passwords, tokens, PINs, secret codes, bank accounts")
                parts.append("")
                parts.append("**Usage rules:**")
                parts.append("- Read this file: `read(\"notes.md\")`")
                parts.append("- Update via `write_file` with path `/_self/kb/notes.md`")
                parts.append("- Update immediately when the user communicates a new preference")
                parts.append("- Prioritize notes.md over `remember` for non-factual preference information")

    # List available skills with SYSTEM.md so the agent knows what it can load
    skills_mgr = SkillsManager()
    _allowed_skills = None if agent.get('is_super') else set(db.get_agent_skills(eid))
    skills_with_system_md = []
    skill_briefs = []
    for skill in skills_mgr.list_skills():
        if not skills_mgr.is_skill_enabled(skill.get('id', '')):
            continue
        # Hide super_only skills from regular agents
        if skill.get('super_only', False) and not agent.get('is_super'):
            continue
        # Hide skills not in this agent's allowlist (regular agents only)
        if _allowed_skills is not None and skill['id'] not in _allowed_skills:
            continue
        # Only list lazy skills — eager skills' tools are already in the tool list
        if not skill.get('lazy_tools', False):
            continue
        skill_dir = skill.get('_dir', os.path.join(_BASE_DIR, 'skills', skill['id']))
        system_md_path = os.path.join(skill_dir, 'SYSTEM.md')
        if os.path.isfile(system_md_path):
            skills_with_system_md.append(skill['id'])
            # brief is for agents; fall back to description if no brief defined
            brief = skill.get('brief', '').strip() or skill.get('description', '').strip()
            if brief:
                skill_briefs.append(brief)

    if skills_with_system_md:
        parts.append("\n## Skills")
        parts.append("You have these skills that can be loaded using `use_skill` tool:")
        for skill_id in skills_with_system_md:
            parts.append(f"- `{skill_id}`")
        # Inject skill briefs — short usage hints defined in skill.json
        if skill_briefs:
            for brief in skill_briefs:
                parts.append(f"\n{brief}")

    # Inform all agents about /_self/ access to their local config directory
    parts.append("\n## Agent Home Directory")
    parts.append(
        "You can access your own agent directory on the evonic server "
        "using the `/_self/` path prefix with any file tool."
    )
    parts.append(
        f"- `/_self/SYSTEM.md` — your system prompt\n"
        f"- `/_self/kb/` — your knowledge base files\n"
        f"- `/_self/sessions/` — your session data"
    )

    # Inform agents about portal virtual paths configured for them
    _portal_lines = _build_portal_info(eid)
    if _portal_lines:
        parts.append("\n## Portals — Virtual Path Mappings")
        parts.append(
            "Your administrator has configured the following virtual path mappings "
            "for file I/O (read_file, write_file, patch, str_replace). "
            "Use `/_portal/<name>/...` to access files on these locations. "
            "Portals do NOT work with bash or runpy."
        )
        parts.extend(_portal_lines)

    # Sandbox awareness: inform the agent when it runs inside a Docker container
    if agent.get('sandbox_enabled'):
        parts.append("\n## Sandbox Environment\n")
        parts.append(
            "You are running inside a **sandboxed Docker container** for safety isolation. "
            "Important implications:\n\n"
            "- **Tools** (`bash`, `runpy`, `read_file`, `write_file`, `patch`, `str_replace`) "
            "execute **inside this container**, not on the host.\n"
            "- **Evonic server processes** (including its web server, database, and agent runtime) "
            "run on the **host** outside this sandbox. You **cannot** restart, stop, or modify "
            "the evonic service from within the sandbox.\n"
            "- **File paths** like `/workspace/` refer to the sandbox's mounted workspace, "
            "not the host filesystem. Host-level paths and system directories are not accessible.\n"
            "- **Network**: The container has network access (e.g., API calls via `http.get/post`) "
            "but cannot reach host-local services bound to `localhost`.\n"
            "- **Session persistence**: The container persists across calls within the same session "
            "— installed packages and written files survive between tool invocations."
        )

    return "\n".join(parts) if parts else "You are a helpful assistant."


def _cache_key_valid(agent: Dict[str, Any], cache_entry: Dict[str, Any]) -> bool:
    """Check if the cached static prompt is still valid by comparing mtimes."""
    aid = agent['id']
    eid = _effective_id(agent)

    # Check SYSTEM.md mtime
    sp_path = _system_prompt_path(eid)
    if _get_mtime(sp_path) != cache_entry['sp_mtime']:
        return False

    # Check KB dir mtime
    kb_dir = os.path.join(_AGENTS_DIR, eid, 'kb')
    if _get_mtime(kb_dir) != cache_entry['kb_mtime']:
        return False

    # Check skills mtimes (SYSTEM.md and skill.json)
    cached_skills_mtimes = cache_entry.get('skills_mtimes', {})
    skills_mgr = SkillsManager()
    for skill in skills_mgr.list_skills():
        sid = skill.get('id', '')
        skill_dir = skill.get('_dir', os.path.join(_BASE_DIR, 'skills', sid))
        system_md_mtime = _get_mtime(os.path.join(skill_dir, 'SYSTEM.md'))
        skill_json_mtime = _get_mtime(os.path.join(skill_dir, 'skill.json'))
        current_mtime = max(system_md_mtime, skill_json_mtime)
        if current_mtime != cached_skills_mtimes.get(sid, 0.0):
            return False

    # Check tools hash (assigned tool IDs)
    assigned_ids = frozenset(db.get_agent_tools(eid))
    if str(sorted(assigned_ids)) != cache_entry['tools_hash']:
        return False

    # Check context.py mtime (for injected sections like slash commands)
    if _get_mtime(__file__) != cache_entry.get('ctx_mtime', 0.0):
        return False

    # Check sandbox_enabled — toggling the sandbox setting must invalidate the cache
    if agent.get('sandbox_enabled', 0) != cache_entry.get('sandbox_enabled', 0):
        return False

    return True


def build_system_prompt(agent: Dict[str, Any]) -> str:
    """Build the system prompt including tool injections and KB file listing.

    The static portion (SYSTEM.md, KB files, skills) is cached per-agent and
    invalidated only when underlying files/dirs change (mtime check).
    Dynamic portions (onboarding, datetime) are always re-evaluated.
    """
    aid = agent['id']
    eid = _effective_id(agent)

    # Check cache
    cache_entry = _system_prompt_cache.get(aid)
    if cache_entry is not None and _cache_key_valid(agent, cache_entry):
        static_prompt = cache_entry['static_prompt']
    else:
        # Cache miss or invalid — rebuild static portion
        static_prompt = _build_static_prompt(agent)

        # Build mtime snapshot for cache validation
        sp_path = _system_prompt_path(eid)
        kb_dir = os.path.join(_AGENTS_DIR, eid, 'kb')
        skills_mtimes = {}
        skills_mgr = SkillsManager()
        for skill in skills_mgr.list_skills():
            sid = skill.get('id', '')
            skill_dir = skill.get('_dir', os.path.join(_BASE_DIR, 'skills', sid))
            system_md_mtime = _get_mtime(os.path.join(skill_dir, 'SYSTEM.md'))
            skill_json_mtime = _get_mtime(os.path.join(skill_dir, 'skill.json'))
            skills_mtimes[sid] = max(system_md_mtime, skill_json_mtime)

        assigned_ids = frozenset(db.get_agent_tools(eid))

        _system_prompt_cache[aid] = {
            'static_prompt': static_prompt,
            'sp_mtime': _get_mtime(sp_path),
            'kb_mtime': _get_mtime(kb_dir),
            'skills_mtimes': skills_mtimes,
            'tools_hash': str(sorted(assigned_ids)),
            'ctx_mtime': _get_mtime(__file__),
            'sandbox_enabled': agent.get('sandbox_enabled', 0),
        }

    prompt = static_prompt

    # Onboarding injection for super agent (one-time, until owner name is known).
    # Once set_owner_name is called, defaults/super_agent_system_prompt.md is copied
    # to SYSTEM.md and owner_name is stored — the injection below is then replaced
    # by a simple personalization line.
    if agent.get('is_super'):
        _owner_name = db.get_setting('owner_name')
        if not _owner_name:
            prompt += (
                "\n\n## IMPORTANT: First-Time Onboarding\n"
                "This is your first conversation. You MUST:\n"
                f"1. Introduce yourself — your name is **{agent.get('name', 'Agent')}**\n"
                "2. Ask for the platform owner's name\n"
                "3. Once you learn their name, call the `set_owner_name` tool with their name\n"
                "4. Then greet them warmly and offer help\n\n"
                "Do not do anything else before you know the owner's name."
            )
        else:
            prompt += f"\n\nYour owner's name is: **{_owner_name}**"

    if agent.get('inject_datetime'):
        gmt7 = timezone(timedelta(hours=7))
        now = datetime.now(gmt7)
        has_template_vars = any(v in prompt for v in ('{{time}}', '{{date}}', '{{day}}'))
        # Replace inline template vars (backward compat for existing SYSTEM.md files)
        prompt = prompt.replace('{{time}}', now.strftime('%H:%M:%S'))
        prompt = prompt.replace('{{date}}', now.strftime('%Y-%m-%d'))
        prompt = prompt.replace('{{day}}', now.strftime('%A'))
        # Auto-append datetime block if no inline template vars were present
        if not has_template_vars:
            prompt += (f"\n\nCurrent date/time: {now.strftime('%A')}, "
                       f"{now.strftime('%Y-%m-%d')}, {now.strftime('%H:%M:%S')} (WIB/UTC+7)")

    # Always append the empty-response recovery instruction
    prompt += (
        "\n\n## Response Recovery Rule\n"
        "If you are asked \"[SYSTEM] Please continue and give your response.\", it means "
        "your previous turn produced no visible reply. Continue your work or provide your "
        "response now. If you genuinely have nothing to say (e.g. the message was "
        "internal/system noise that requires no reply), respond with exactly: `[No response needed]`"
    )

    # Dynamically inject slash commands based on agent permissions
    is_super = bool(agent.get('is_super'))
    slash_commands = [
        ("/clear", "Clear chat history for this session"),
        ("/help", "Show available commands"),
        ("/summary", "Force regenerate session summary"),
        ("/stop", "Stop the agent's current processing loop"),
    ]
    slash_commands.append(("/plan", "Switch to plan mode"))
    slash_commands.append(("/unfocus", "Force-clear focus mode — use when agent is stuck in focus after a failed task"))
    if is_super:
        slash_commands.append(("/restart", "Restart the service (super agent only)"))
        slash_commands.append(("/cwd", "Show current workspace directory"))
        slash_commands.append(("/cd", "Change workspace directory"))
    # /autopilot is not yet implemented, omit from listing

    if slash_commands:
        prompt += "\n\n## Slash Commands\n\n**Available commands:**\n"
        for name, desc in slash_commands:
            prompt += f"- `{name}` — {desc}\n"

    # Inject artifacts directory path for agents with artifacts enabled
    if agent.get('artifacts_enabled', True):
        if agent.get('sandbox_enabled'):
            artifacts_path = os.path.join('/workspace/shared/agents', aid, 'artifacts')
            artifacts_note = (
                f"Your artifacts directory is: `{artifacts_path}`\n"
                "Files you save here will appear in the Artifacts tab on your agent detail page.\n"
                "Use `save_artifact` tool for text files, or write files directly to this path "
                "using `write_file` or bash/runpy for binary files (PDFs, images)."
            )
        else:
            artifacts_note = (
                "Your artifacts directory is available via the `save_artifact` tool. "
                "You can also save files directly to your artifacts directory "
                "using `write_file` or bash/runpy.\n"
                "Files you save there will appear in the Artifacts tab on your agent detail page."
            )
        prompt += "\n\n## Artifacts Directory\n" + artifacts_note

    return prompt


def build_tools(agent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the OpenAI function tool list for this agent."""
    tools = []

    # Always include built-in tools (read, etc.)
    # Pass workplace_id so built-in factories can tailor descriptions for remote agents
    # (e.g. read() tool mentions /_self/kb/ when workplace_id is set).
    agent_context = {
        'id': agent['id'],
        'is_super': bool(agent.get('is_super')),
        'workplace_id': agent.get('workplace_id'),
    }
    tools.extend(tool_registry.get_builtin_tools(agent_context))

    # Super agent gets its own administrative built-in tools
    if agent.get('is_super'):
        from backend.tools.super_agent_tools import get_super_agent_tool_defs
        tools.extend(get_super_agent_tool_defs())

    # Super agent gets ALL skill tools automatically — no per-skill assignment needed
    if agent.get('is_super'):
        seen_fn_names = {t['function']['name'] for t in tools if t.get('function', {}).get('name')}
        for tool_def in tool_registry.get_all_tool_defs():
            tool_id = tool_def.get('id', '')
            fn_name = tool_def.get('function', {}).get('name', '')
            if not tool_id.startswith('skill:') or not fn_name:
                continue
            if fn_name in seen_fn_names:
                continue
            seen_fn_names.add(fn_name)
            tools.append({
                "type": "function",
                "function": tool_def['function']
            })

    # Agent messaging tools — available to super agent and agents with messaging enabled
    if agent.get('is_super') or agent.get('agent_messaging_enabled') != 0:
        from backend.tools.agent_messaging import get_agent_messaging_tool_defs
        tools.extend(get_agent_messaging_tool_defs())

    # Add assigned tools from the registry (including skill tools)
    # Sub-agents inherit parent's tool assignments
    eid = _effective_id(agent)
    assigned_ids = set(db.get_agent_tools(eid))
    if assigned_ids:
        seen_fn_names = {t['function']['name'] for t in tools if t.get('function', {}).get('name')}
        for tool_def in tool_registry.get_all_tool_defs():
            tool_id = tool_def.get('id', '')
            fn_name = tool_def.get('function', {}).get('name', '')
            # Match by namespaced id OR bare function name (backward compat)
            if tool_id in assigned_ids or fn_name in assigned_ids:
                # One function name per agent — skip duplicates
                if fn_name in seen_fn_names:
                    continue
                seen_fn_names.add(fn_name)
                tools.append({
                    "type": "function",
                    "function": tool_def['function']
                })

    # ── Patch /workspace references for non-sandbox (workplace) agents ──
    # Tool JSON definitions contain /workspace paths in function descriptions
    # and parameter descriptions. Workplace agents are misled into trying to
    # use paths that don't exist on their system.
    if not agent.get('sandbox_enabled'):
        for tool in tools:
            func = tool.get('function', {})
            # Patch function-level description
            if 'description' in func and '/workspace' in func['description']:
                func['description'] = func['description'].replace('/workspace', 'the agents working directory')
            # Patch parameter descriptions
            for param_name, param_def in func.get('parameters', {}).get('properties', {}).items():
                if isinstance(param_def, dict) and 'description' in param_def:
                    if '/workspace' in param_def['description']:
                        param_def['description'] = param_def['description'].replace('/workspace', 'the agents working directory')
    return tools


def get_compiled_context(agent_id: str, user_id: str = None) -> dict:
    """Return the compiled system prompt, tool definitions, token estimates,
    and optionally the actual LLM context (memories + prior summary)."""
    agent = db.get_agent(agent_id)
    if not agent:
        return {"system_prompt": "", "tools": [], "tokens": {"system_prompt": 0, "tool_definitions": 0, "total": 0}}

    system_prompt = build_system_prompt(agent)
    tools = build_tools(agent)

    # Token estimates using tiktoken cl100k_base
    sp_tokens = _token_count(system_prompt)
    tool_tokens = _token_count(json.dumps(tools))

    result = {
        "system_prompt": system_prompt,
        "tools": tools,
        "tokens": {
            "system_prompt": sp_tokens,
            "tool_definitions": tool_tokens,
            "total": sp_tokens + tool_tokens,
        }
    }

    # If user_id provided, also return memories and summary (actual LLM context extras)
    if user_id:
        from backend.agent_runtime.memory_manager import get_memories_for_context
        session_id = db.get_or_create_session(agent_id, user_id)
        fake_messages = [{"role": "system", "content": system_prompt}]
        memory_text = get_memories_for_context(agent_id, fake_messages)
        mem_tokens = 0
        if memory_text:
            result["memories"] = memory_text
            mem_tokens = _token_count(memory_text)
            result["tokens"]["memories"] = mem_tokens

        summary_record = db.get_summary(session_id, agent_id=agent_id)
        sum_tokens = 0
        if summary_record:
            summary_text = f"## Prior conversation summary\n{summary_record['summary']}"
            result["summary"] = summary_text
            sum_tokens = _token_count(summary_text)
            result["tokens"]["summary"] = sum_tokens

        # Recalculate total to include memories and summary
        result["tokens"]["total"] = sp_tokens + tool_tokens + mem_tokens + sum_tokens

    return result


def build_message_entry(msg: dict, agent: dict) -> dict:
    """Convert a DB message row into an LLM message dict."""
    entry = {"role": msg['role']}
    msg_image = None
    if msg.get('metadata') and isinstance(msg['metadata'], dict):
        msg_image = msg['metadata'].get('image_url')
    if msg_image and agent.get('vision_enabled'):
        parts = []
        if msg.get('content') and msg['content'] != '[Image]':
            parts.append({"type": "text", "text": msg['content']})
        parts.append({"type": "image_url", "image_url": {"url": msg_image}})
        if not parts[0].get('text') if parts else True:
            parts.insert(0, {"type": "text", "text": "What is in this image?"})
        entry['content'] = parts
    elif msg.get('content'):
        content = msg['content']
        # Safety net: re-truncate legacy DB entries that were stored untruncated
        if msg.get('role') == 'tool' and len(content) > MAX_TOOL_RESULT_CHARS:
            remaining = len(content) - MAX_TOOL_RESULT_CHARS
            content = (content[:MAX_TOOL_RESULT_CHARS] +
                       f"\n...[truncated — {remaining} chars omitted]")
        entry['content'] = content
    if msg.get('tool_calls'):
        entry['tool_calls'] = msg['tool_calls']
    if msg.get('tool_call_id'):
        entry['tool_call_id'] = msg['tool_call_id']
    # Restore reasoning_content so it is passed back to APIs that require it
    if msg.get('role') == 'assistant' and msg.get('metadata') and isinstance(msg['metadata'], dict):
        rc = msg['metadata'].get('reasoning_content')
        if rc:
            entry['reasoning_content'] = rc
    return entry
