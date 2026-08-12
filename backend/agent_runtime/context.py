"""
context.py — builds LLM input: system prompt, tool list, message formatting.

Pure data preparation — no LLM calls, no threading.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
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
from models.boolean import message_wrapper_enabled
from backend.tools import tool_registry
from backend.tools.registry import BUILTIN_TOOL_IDS
from backend.skills_manager import SkillsManager, skills_manager
from backend.agent_runtime.evomem_client import (
    get_evomem_db_mtime,
)
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

    Explorers are the exception: although they are sub-agents, they must NOT
    inherit the parent — they use their own (row-less) id so the system prompt
    falls back to their configured prompt and KB/tool/variable lookups resolve
    to empty. (Guard is a no-op for every non-explorer.)
    """
    if agent.get('is_explorer'):
        return agent['id']
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


def _get_skills_mtime_hash() -> str:
    """Compute a hash over all skill directories' SYSTEM.md and skill.json mtimes.

    Returns a SHA-256 hex digest that changes whenever any skill is added,
    removed, or modified. Uses only stat() calls — no JSON parsing or tool
    def loading, unlike SkillsManager().list_skills().
    """
    skills_dir = os.path.join(_BASE_DIR, 'skills')
    if not os.path.isdir(skills_dir):
        return hashlib.sha256(b'').hexdigest()

    entries = []
    for name in sorted(os.listdir(skills_dir)):
        skill_dir = os.path.join(skills_dir, name)
        if not os.path.isdir(skill_dir):
            continue
        skill_json = os.path.join(skill_dir, 'skill.json')
        if not os.path.isfile(skill_json):
            continue
        system_md = os.path.join(skill_dir, 'SYSTEM.md')
        max_mtime = max(_get_mtime(system_md), _get_mtime(skill_json))
        entries.append(f"{name}:{max_mtime}")

    return hashlib.sha256(','.join(entries).encode()).hexdigest()


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
                f"(Evonet tunnel{status_note}) — {name}"
            )
        else:
            lines.append(
                f"- `/_portal/{vpath}/` → `{real_path}` "
                f"({backend_type}{status_note}) — {name}"
            )

    return lines


def _resolve_workspace(agent: Dict[str, Any]) -> str:
    """Return the effective workspace directory for this agent.

    Resolution order:
    1. Sandbox agents: always /workspace (the Docker container mount)
    2. Agents with workplace: use workplace.config.workspace_path
    3. Fallback: agent.workspace field from DB
    """
    if agent.get('sandbox_enabled'):
        return '/workspace'
    wp_id = agent.get('workplace_id')
    if wp_id:
        try:
            wp = db.get_workplace(wp_id)
            if wp:
                cfg = wp.get('config', {})
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                ws = cfg.get('workspace_path')
                if ws:
                    return ws
        except Exception:
            pass
    return agent.get('workspace') or '/workspace'


# Allowed doc `type` frontmatter values (mirrors Rust validate::VALID_TYPES and
# evomem_writer.DOC_TYPES). Used for write-time validation + KB-graph node colors.
KB_VALID_TYPES = ("note", "session", "group", "person", "place", "venue", "event",
                  "organization", "company", "product", "contact")


def validate_kb_frontmatter(content: str) -> str | None:
    """Validate a KB markdown file's frontmatter. Return an error message if
    invalid, else None.

    Requires a leading YAML frontmatter block (`---` … `---`) with non-empty
    `title`, `description`, and `type`, where `type` ∈ KB_VALID_TYPES.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return ("Missing YAML frontmatter. KB files must start with a `---` block "
                "containing title, description, and type.")
    fields = {}
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        key, sep, val = line.partition(":")
        if sep:
            fields[key.strip()] = val.strip().strip("\"'")
    if not closed:
        return "Unterminated frontmatter block (missing closing `---`)."

    missing = [k for k in ("title", "description", "type") if not fields.get(k)]
    if missing:
        return f"Missing required frontmatter field(s): {', '.join(missing)}."
    if fields["type"] not in KB_VALID_TYPES:
        return (f"Invalid type '{fields['type']}'; must be one of: "
                f"{', '.join(KB_VALID_TYPES)}.")
    return None


def kb_frontmatter_warning(filepath: str) -> str | None:
    """Soft-warn helper for KB edits: validate a file's current content and
    return a user-facing warning if its frontmatter is incomplete, else None.

    Never raises — used to attach a non-blocking warning after an edit.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            err = validate_kb_frontmatter(f.read())
    except Exception:
        return None
    if err:
        return (f"⚠ KB frontmatter incomplete: {err} "
                f"Add title, description, and type (note|session|group).")
    return None



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
                        tool_prompt = tool_prompt.replace('/workspace', _resolve_workspace(agent))
                    parts.append(tool_prompt)

    # Scratchpad policy — only injected for agents that can create/run scripts
    # (bash / runpy / write_file). Keeps throwaway operational scripts and temp
    # files out of the project root and workspace by pointing them at a dedicated
    # per-agent scratchpad directory.
    _script_tools = {'bash', 'runpy', 'write_file'}
    if assigned_ids & _script_tools:
        from backend.tools._workspace import scratch_dir
        scratch = scratch_dir(agent.get('agent_id') or aid)
        parts.append(
            "\n## Script & Scratch File Policy (MANDATORY)\n"
            "You have tools that create and run scripts. Write EVERY throwaway "
            "script, temporary file, and intermediate operational output to your "
            f"dedicated scratchpad directory:\n\n    {scratch}/\n\n"
            f"- Create it first if missing: `mkdir -p {scratch}`\n"
            "- Never scatter one-off scripts or temp files in the project root, the "
            "workspace, or arbitrary /tmp paths.\n"
            "- Only durable, intentional deliverables belong outside the scratchpad."
        )

    if message_wrapper_enabled(agent, db):
        parts.append("")
        parts.append("## Message Wrapper Protocol")
        parts.append(
            "After EVERY user message, before your main response, you MUST:"
        )
        parts.append(
            "1. Scan the message for any new preference, instruction, rule, or personal fact."
        )
        parts.append(
            "2. If found: store it immediately via remember() (factual data), "
            "store it as a preference via remember() for non-factual style notes, or update SYSTEM.md (critical rules)."
        )
        parts.append(
            "3. This applies to BOTH explicit and implicit cues. Even casual mentions count."
        )

    # Memory Retrieval Protocol — coach the agent on the retrieval side of
    # long-term memory (the capture side is covered above). Memories are NOT
    # auto-injected; the agent must use recall() to explicitly fetch them.
    parts.append("")
    parts.append("## Memory Retrieval Protocol")
    parts.append(
        "You have long-term memory that persists across conversations. You MUST use "
        "the `recall` tool to look up past facts — nothing is injected automatically."
    )
    parts.append(
        "- `recall(query=\"<key>\", mode=\"key\")` — exact current value of a keyed "
        "fact. Fastest and most precise; prefer it whenever the key is listed below."
    )
    parts.append(
        "- `recall(query=\"...\")` — fast keyword lookup of a specific stored fact "
        "(e.g. a phone number, an address, a name). This is the default (mode='fts')."
    )
    parts.append(
        "- `recall(query=\"...\", mode=\"think\")` — reason over EVERYTHING you know "
        "about a topic; returns a synthesis plus what is still missing. Prefer this for "
        "open questions like \"what do I know about the user's project?\"."
    )
    parts.append(
        "- `recall(query=\"...\", mode=\"graph\")` — follow relationships between people, "
        "organizations, and projects (e.g. where someone works, what they founded, "
        "who they advise); `query` is the entity name."
    )
    parts.append(
        "Look facts up instead of guessing or asking the user for something you may "
        "already know."
    )
    parts.append(
        "- If you are not sure about a thing or what the user wants, check first "
        "using the `recall` tool \u2014 the information might already be there."
    )
    parts.append(
        "- **Recall before filesystem search**: When you need to locate a project "
        "directory, binary, configuration file, or any file path, you MUST use "
        "`recall` first to check if the location is already stored in long-term "
        "memory. Only after confirming the information is not in memory should you "
        "resort to filesystem exploration."
    )

    # Keys-only memory index: show WHAT the agent knows (a few tokens per key)
    # without paying for the contents. Enables precise recall(mode='key') and
    # keeps remember() key naming consistent (the agent reuses listed keys).
    try:
        _mem_keys = db.get_active_dimensions(aid, limit=50)
    except Exception:
        _mem_keys = []
    if _mem_keys:
        parts.append(
            "Known memory keys (current value via `recall(query=\"<key>\", "
            "mode=\"key\")`; reuse a listed key in remember() to update that fact):"
        )
        parts.append("`" + "`, `".join(_mem_keys) + "`")

    # List available skills with SYSTEM.md so the agent knows what it can load
    skills_mgr = skills_manager
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

    # Skill cleanup rule: remind agents to unload unused lazy-loaded skills.
    # This is a platform-level instruction injected into all agents by default,
    # so users don't have to manually add it to SYSTEM.md.
    parts.append("\n## Skill Cleanup Rule")
    parts.append(
        "After completing a task that required loading a lazy-loaded skill, "
        "immediately review loaded skills and unload any that are no longer "
        "needed. Do not keep unused skills in context; they waste tokens by "
        "adding stale tool definitions."
    )
    # Build operations rule: inject for agents that have bash or runpy tools.
    # This ensures long-running compilations don't block the agent.
    if assigned_ids and ('bash' in assigned_ids or 'runpy' in assigned_ids):
        parts.append("\n## Build Operations Rule\n")
        parts.append(
            "Every build operation (cmake, make, ninja, gcc, g++, cargo build, "
            "go build, npm build, or any long-running compilation) MUST be "
            "executed inside a tmux or screen session. Never run these commands "
            "directly in bash — they will block the agent. **Dependency "
            "priority**: (1) `tmux` — `tmux new-session -d -s build \"cd "
            "/path && make 2>&1 | tee build.log\"` then monitor with `tmux "
            "capture-pane -t build -p`. (2) `screen` — fallback if tmux "
            "not available. (3) `nohup` — last resort if neither tmux "
            "nor screen available."
        )
    

    # Inform all agents about /_self/ access to their local config directory
    parts.append("\n## Agent Home Directory")
    parts.append(
        "You can access your own agent directory on the evonic server "
        "using the `/_self/` path prefix with any file tool."
    )
    parts.append(
        f"- `/_self/SYSTEM.md` — your system prompt\n"
        f"- `/_self/kb/` — your knowledge base files\n"
        f"- `/_self/sessions/` — your session data\n"
        f"- `/_self/plan/` — your plan files\n"
        f"- `/_self/artifacts/` — your artifacts directory"
    )
    parts.append(
        "**Important**: `/_self/` paths only work with file tools "
        "(`read_file`, `write_file`, `patch`, `str_replace`) — "
        "NOT with `bash` or `runpy`."
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

    # List available agent variables (names only, never values) so the LLM
    # knows to reference $VAR_NAME in bash/runpy instead of literal secrets.
    agent_vars = db.get_agent_variables(eid)
    if agent_vars:
        parts.append("\n## Environment Variables")
        parts.append(
            "The following variables are automatically available as environment variables "
            "in `bash` and `runpy` tools. Use `$VAR_NAME` in bash or `os.environ['VAR_NAME']` "
            "in Python. NEVER output literal values of secret variables — they are injected automatically."
        )
        for var in agent_vars:
            label = " (secret)" if var.get('is_secret') else ""
            parts.append(f"- `${var['key']}`{label}")

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

    # Check skills hash (covers SYSTEM.md and skill.json for all skill dirs)
    if _get_skills_mtime_hash() != cache_entry.get('skills_hash', ''):
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

    # Check agent variables hash (adding/removing/changing variables must invalidate)
    current_vars = db.get_agent_variables(eid)
    vars_key = str(sorted((v['key'], v.get('is_secret', False)) for v in current_vars))
    if hashlib.sha256(vars_key.encode()).hexdigest() != cache_entry.get('vars_hash', ''):
        return False

    # Check evomem DB mtime (KB graph changes when links are synced)
    if get_evomem_db_mtime(eid) != cache_entry.get('evomem_mtime', 0.0):
        return False

    # Check run_as_user — changing the execution user must invalidate the cache
    if agent.get('run_as_user') != cache_entry.get('run_as_user'):
        return False

    # Check workspace — changing via /cd must invalidate the cache
    if _resolve_workspace(agent) != cache_entry.get('workspace'):
        return False

    if message_wrapper_enabled(agent, db) != cache_entry.get('message_wrapper_enabled'):
        return False

    return True


def trail_history_kwargs(agent_id: str) -> dict:
    """Extra kwargs for chatlog.get_entries_for_llm_trail().

    Normally empty, so trail history keeps its default 50-message sliding
    window. For agent ids listed in the `trail_history_full_agents` setting,
    raise the message limit to `trail_history_limit` (default 1_000_000), i.e.
    TRUE full history — the whole transcript is sent every turn until the model
    hits its context ceiling. Used by the CMP endurance benchmark to run a
    genuine full-history baseline against the bounded windowed/CMP arms.
    """
    try:
        from models.db import db
        full = (db.get_setting('trail_history_full_agents', '') or '')
        if agent_id and agent_id in {a.strip() for a in full.split(',') if a.strip()}:
            return {'limit': int(db.get_setting('trail_history_limit', '1000000') or 1000000)}
    except Exception:
        pass
    return {}


def build_system_prompt(agent: Dict[str, Any], injected_system_vars: Dict[str, str] = None) -> str:
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
        skills_hash = _get_skills_mtime_hash()

        assigned_ids = frozenset(db.get_agent_tools(eid))

        # Compute variables hash for cache invalidation
        current_vars = db.get_agent_variables(eid)
        vars_key = str(sorted((v['key'], v.get('is_secret', False)) for v in current_vars))
        vars_hash = hashlib.sha256(vars_key.encode()).hexdigest()

        _system_prompt_cache[aid] = {
            'static_prompt': static_prompt,
            'sp_mtime': _get_mtime(sp_path),
            'kb_mtime': _get_mtime(kb_dir),
            'evomem_mtime': get_evomem_db_mtime(eid),
            'skills_hash': skills_hash,
            'tools_hash': str(sorted(assigned_ids)),
            'ctx_mtime': _get_mtime(__file__),
            'sandbox_enabled': agent.get('sandbox_enabled', 0),
            'vars_hash': vars_hash,
            'run_as_user': agent.get('run_as_user'),
            'workspace': _resolve_workspace(agent),
            'message_wrapper_enabled': message_wrapper_enabled(agent, db),
        }

    prompt = static_prompt

    # Expand injected system vars (session-scoped, after cache)
    if injected_system_vars is None:
        injected_system_vars = agent.get('injected_system_vars')
    if injected_system_vars:
        for key, value in injected_system_vars.items():
            placeholder = '{{' + key + '}}'
            prompt = prompt.replace(placeholder, str(value))

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

    # Run-as-user awareness: tell agent which user they execute as (no sudo needed).
    run_as_user = agent.get('run_as_user')
    if run_as_user and not agent.get('sandbox_enabled'):
        prompt += f"\n\nYou are run as the **{run_as_user}** user."

    # Bwrap sandbox awareness: agents executing inside a bubblewrap sandbox
    # (bwrap workplace, or global SANDBOX_BACKEND=bwrap) need to know its rules.
    _bwrap_active = False
    _wp_id = agent.get('workplace_id')
    if _wp_id:
        try:
            _wp = db.get_workplace(_wp_id)
            _bwrap_active = bool(_wp and _wp.get('type') == 'bwrap')
        except Exception:
            pass
    elif agent.get('sandbox_enabled'):
        try:
            from config import SANDBOX_BACKEND
            _bwrap_active = SANDBOX_BACKEND == 'bwrap'
        except ImportError:
            pass
    if _bwrap_active:
        prompt += (
            "\n\n## Sandbox Environment\n"
            "You run inside a lightweight Linux sandbox (bubblewrap):\n"
            "- No root/sudo — the OS filesystem is read-only. Install tools into your "
            "home instead (`pip install --user`, `npm --prefix ~/…`, or binaries in `~/bin`); "
            "your home persists.\n"
            "- Work in `/workspace` (visible to file tools) or `~` (`/home/agent`). "
            "Files under `/tmp` are invisible to file tools.\n"
            "- Background processes (servers, tunnels, tmux) keep running between "
            "commands, but die when the platform restarts.\n"
            "- `ping` may be unavailable (no raw sockets) — use `curl` to test connectivity."
        )

    # Dynamic enabled-agent roster for super agents.
    # Injects a lightweight list of enabled agents (id, name, description) so the
    # super agent can quickly identify targets for delegation via send_agent_message.
    # Uses raw SQL to avoid loading full agent records — minimal overhead.
    if agent.get('is_super'):
        try:
            with db._connect() as conn:
                rows = conn.execute(
                    "SELECT id, name, description FROM agents WHERE enabled = 1 ORDER BY name"
                ).fetchall()
            if rows:
                lines = ["\n## Enabled Agents\n",
                         "These agents are available for delegation via `send_agent_message`:\n"]
                for row in rows:
                    agent_id, agent_name, agent_desc = row
                    desc = f" — {agent_desc}" if agent_desc else ""
                    lines.append(f"- **{agent_id}** ({agent_name}){desc}")
                prompt += "\n".join(lines)
        except Exception:
            _logger.warning("Failed to inject agent roster for super agent %s", aid, exc_info=True)

    # Evonet tunnel awareness: inform agents when they operate through a tunnel workplace
    workplace_id = agent.get('workplace_id')
    if workplace_id:
        try:
            workplace = db.get_workplace(workplace_id)
            if workplace and workplace.get('type') == 'tunnel':
                prompt += (
                    "\n\n## Evonet Tunnel Workplace\n\n"
                    "You are operating through an Evonet tunnel (WebSocket) to a remote device. "
                    "Your tools (bash, runpy, file operations) execute on that remote device, "
                    "not on the Evonic server. If the remote device disconnects, your tools "
                    "will be unavailable until the Evonet connector reconnects. "
                    "For more details, see https://evonic.dev/evonet/"
                )
        except Exception:
            _logger.warning("Failed to lookup workplace for agent %s", aid, exc_info=True)

    # CWD awareness: tell non-sandbox agents their actual working directory.
    # Sandbox agents already know they run at /workspace from the Sandbox
    # Environment section. Tunnel/remote agents need this since their tool
    # descriptions no longer show a generic placeholder.
    if not agent.get('sandbox_enabled'):
        workspace = _resolve_workspace(agent)
        if workspace:
            prompt += f"\n\nYour current working directory is `{workspace}`.\n"

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
        ("/detach", "Stop waiting on the running long-running process and attach an on-exit monitor to it, so we can keep chatting and you report back once it finishes (persistent, survives restarts; the monitor removes itself)"),
        ("/jobs", "List background jobs for this session and any monitors attached to them"),
        ("/dump", "Dump current session as JSONL file for download"),
        ("/model", "Show or switch LLM model"),
    ]
    slash_commands.append(("/plan", "Switch to plan mode"))
    slash_commands.append(("/unfocus", "Force-clear focus mode — use when agent is stuck in focus after a failed task"))
    # /cd and /cwd are available to super agents and agents with remote/tunnel workplaces
    has_remote_workplace = False
    if not is_super:
        workplace_id = agent.get('workplace_id')
        if workplace_id:
            try:
                workplace = db.get_workplace(workplace_id)
                if workplace and workplace.get('type') in ('remote', 'tunnel'):
                    has_remote_workplace = True
            except Exception:
                pass
    if is_super or has_remote_workplace:
        slash_commands.append(("/cwd", "Show current workspace directory"))
        slash_commands.append(("/cd", "Change workspace directory"))
    if is_super:
        slash_commands.append(("/restart", "Restart the service (super agent only)"))
        slash_commands.append(("/shutdown", "Shut down the Evonic server completely (super agent only)"))
    # /autopilot is not yet implemented, omit from listing

    # Filter slash commands based on per-agent hidden/disabled settings.
    # Super agents are exempt — they always see all commands.
    if not is_super:
        all_cmd_names = {name for name, _desc in slash_commands}

        def _expand(raw_value: str) -> set:
            if not raw_value or not raw_value.strip():
                return set()
            raw = raw_value.strip()
            if raw == '*':
                return set(all_cmd_names)
            if raw.startswith('!'):
                allowed = {c.strip() for c in raw[1:].split(',') if c.strip()}
                return set(all_cmd_names) - allowed
            return {c.strip() for c in raw.split(',') if c.strip()}

        hidden = _expand(agent.get('hidden_slash_commands', ''))
        disabled = _expand(agent.get('disabled_slash_commands', ''))
        remove_set = hidden | disabled
        if remove_set:
            slash_commands = [(n, d) for n, d in slash_commands if n not in remove_set]

    if slash_commands:
        prompt += "\n\n## Slash Commands\n\n**Available commands:**\n"
        for name, desc in slash_commands:
            prompt += f"- `{name}` — {desc}\n"

    # Inject artifacts directory path for agents with artifacts enabled
    if agent.get('artifacts_enabled', True):
        if agent.get('sandbox_enabled'):
            artifacts_path = os.path.join('/workspace/shared/agents', aid, 'artifacts')
            artifacts_note = (
                f"Directory: `{artifacts_path}` (also `/_self/artifacts/` via file tools only). "
                "Save with `save_artifact(content=\"...\")` or `save_artifact(source_path=\"...\")`; "
                "files appear in the Artifacts tab. "
                f"Public URL: `/api/agents/{aid}/artifacts/<filename>`. "
                f"Embed images with `<img src=\"/api/agents/{aid}/artifacts/filename.webp\" alt=\"...\">`. "
                "Deliver files to the user with `send_file` (or `save_artifact` for the Artifacts tab). "
                "Never give local filesystem paths (e.g. `/home/...`, `sandbox:...`) as chat links — "
                "the user cannot open them. "
                f"`bash`/`runpy` must use `{artifacts_path}`, not `/_self/`."
            )
        else:
            artifacts_note = (
                f"Public URL: `/api/agents/{aid}/artifacts/<filename>`. "
                "Save with `save_artifact(content=\"...\")` or `save_artifact(source_path=\"...\")`; "
                "files are also available at `/_self/artifacts/` via file tools. "
                f"Embed images with `<img src=\"/api/agents/{aid}/artifacts/filename.webp\" alt=\"...\">`. "
                "Deliver files to the user with `send_file` (or `save_artifact` for the Artifacts tab). "
                "Never give local filesystem paths (e.g. `/home/...`, `sandbox:...`) as chat links — "
                "the user cannot open them. "
                "`bash`/`runpy` cannot use `/_self/`."
            )
        prompt += "\n\n## Artifacts Directory\n" + artifacts_note

    return prompt


def build_tools(agent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the OpenAI function tool list for this agent."""
    tools = []

    # Explorer sub-agents (the Explore tool's explorers AND the KB organizer) are
    # isolated workers: they get ONLY their configured tools — the direxplorer
    # read-only set (Grep/Read/Glob) plus any EXTRAS the user added in the explorer
    # skill settings. They must NOT receive built-ins (remember/recall/...),
    # universal, messaging, or parent tools. Resolve FIRST and return; their tools
    # may come from a LAZY skill, hence resolving directly via _explorer.tool_defs.
    if agent.get('is_explorer'):
        from backend.agent_runtime import explorer as _explorer
        seen_fn_names = set()
        for tool_def in _explorer.tool_defs(agent):
            fn_name = tool_def.get('function', {}).get('name', '')
            if fn_name and fn_name not in seen_fn_names:
                seen_fn_names.add(fn_name)
                tools.append(tool_def)
        return compact_tool_definitions(tools)

    # Resolve explicit assignments before adding messaging definitions so the LLM
    # sees only messaging tools the agent can actually execute. Sub-agents inherit
    # their parent's assignments.
    eid = _effective_id(agent)
    assigned_ids = set(db.get_agent_tools(eid))

    # Built-in tools (use_skill, set_mode, remember, recall, etc.)
    # Can be disabled per-agent via builtin_tools_enabled advanced setting.
    agent_context = {
        'id': agent['id'],
        'is_super': bool(agent.get('is_super')),
        'workplace_id': agent.get('workplace_id'),
        'send_file_allowed_path_regex': agent.get('send_file_allowed_path_regex', ''),
        'enable_atg': bool(agent.get('enable_atg')) and bool(agent.get('enable_agent_state')),
        'enable_cmp': bool(agent.get('enable_cmp')) and bool(agent.get('enable_agent_state')),
        'always_execute': bool(agent.get('always_execute')),
    }
    if agent.get('builtin_tools_enabled', True):
        tools.extend(tool_registry.get_builtin_tools(agent_context))

    # Universal tools — always available to all agents without explicit assignment.
    # These are regular backend/tools/.py implementations (save_artifact, send_file)
    # that should behave like built-in tools but don't use the built-in factory pattern.
    seen_fn_names = {t['function']['name'] for t in tools if t.get('function', {}).get('name')}
    for tool_def in tool_registry.get_all_tool_defs():
        fn_name = tool_def.get('function', {}).get('name', '')
        tool_id = tool_def.get('id', '')
        if not fn_name or tool_id.startswith('skill:'):
            continue
        if fn_name not in BUILTIN_TOOL_IDS:
            continue
        if fn_name == 'save_artifact' and not agent.get('artifacts_enabled', True):
            continue
        if fn_name in seen_fn_names:
            continue
        seen_fn_names.add(fn_name)
        tools.append({
            "type": "function",
            "function": tool_def['function']
        })

    # Super agent gets its own administrative built-in tools
    if agent.get('is_super'):
        from backend.tools.super_agent_tools import get_super_agent_tool_defs
        tools.extend(get_super_agent_tool_defs())

    # Agent messaging tools are gated by messaging enablement and assignment.
    # This keeps definitions advertised to the LLM aligned with runtime
    # authorization, while super agents retain access to the full messaging set.
    if agent.get('is_super') or agent.get('agent_messaging_enabled') != 0:
        from backend.tools.agent_messaging import get_agent_messaging_tool_defs
        seen_fn_names = {t['function']['name'] for t in tools if t.get('function', {}).get('name')}
        for tool_def in get_agent_messaging_tool_defs():
            fn_name = tool_def.get('function', {}).get('name', '')
            if not fn_name or fn_name in seen_fn_names:
                continue
            # send_agent_message is the core inter-agent communication tool: an
            # enabled agent receives it without a separate tool assignment. The
            # remaining messaging tools retain assignment-based exposure.
            if (not agent.get('is_super')
                    and fn_name != 'send_agent_message'
                    and fn_name not in assigned_ids):
                continue
            seen_fn_names.add(fn_name)
            tools.append(tool_def)

    # Add assigned tools from the registry (including skill tools).

    # Auto-assign describe_image for vision-enabled agents.
    # Mirrors the auto-assignment in runtime.py and prefetch.py so that
    # build_tools includes the tool definition (not just the hint).
    if agent.get('vision_enabled', 1):
        assigned_ids.add('describe_image')

    # Auto-assign transcribe_audio for audio-enabled agents.
    if agent.get('audio_enabled'):
        assigned_ids.add('transcribe_audio')

    # Auto-assign monitor wherever bash is available — it is the opt-in way to
    # be notified about background processes, which only bash can start.
    if 'bash' in assigned_ids:
        assigned_ids.add('monitor')

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

    # Auto-inject eagerly loaded skill tools for assigned skills
    # This ensures that when an agent has a skill assigned in agent_skills and that skill
    # is eagerly loaded (no lazy_tools=true), the tools are available without manual
    # tool assignment in agent_tools.
    assigned_skill_ids = set(db.get_agent_skills(eid))
    if assigned_skill_ids:
        for skill in skills_manager.list_skills():
            skill_id = skill.get('id', '')
            if skill_id not in assigned_skill_ids:
                continue
            # Skip lazy-loaded skills — their tools are injected via use_skill
            if skill.get('lazy_tools', False):
                continue
            # Skip super_only skills for non-super agents
            if skill.get('super_only', False) and not agent.get('is_super'):
                continue
            defs = skills_manager.get_skill_tool_defs(skill_id)
            for tool_def in defs:
                fn_name = tool_def.get('function', {}).get('name', '')
                if not fn_name:
                    continue
                # Avoid duplicates
                if any(t['function']['name'] == fn_name for t in tools):
                    continue
                tools.append({
                    "type": "function",
                    "function": tool_def['function']
                })

    # ── Patch /workspace and Docker/container references for non-sandbox agents ──
    # Tool JSON definitions contain /workspace paths and Docker/container
    # language in function/parameter descriptions. Non-sandbox agents
    # (workplace/remote) aren't running in Docker, so sanitize these.
    if not agent.get('sandbox_enabled'):
        # Ordered replacements — most specific first to avoid partial matches
        workspace = _resolve_workspace(agent)
        replacements = [
            ('in an isolated Docker container', 'in an isolated execution environment'),
            ('in a sandboxed Docker container', 'in a sandboxed execution environment'),
            ('The container is shared', 'The environment is shared'),
            ('The container persists', 'The environment persists'),
            ('tears down the container', 'tears down the environment'),
            ('tear down the container', 'tear down the environment'),
            ('destroys the shared runpy container', 'destroys the shared runpy environment'),
            ('local/Docker execution', 'local execution'),
            ('/workspace', workspace),
        ]
        for tool in tools:
            func = copy.deepcopy(tool.get('function', {}))
            tool['function'] = func
            # Patch function-level description
            if 'description' in func:
                desc = func['description']
                for old, new in replacements:
                    desc = desc.replace(old, new)
                func['description'] = desc
            # Patch parameter descriptions
            for param_def in func.get('parameters', {}).get('properties', {}).values():
                if isinstance(param_def, dict) and 'description' in param_def:
                    desc = param_def['description']
                    for old, new in replacements:
                        desc = desc.replace(old, new)
                    param_def['description'] = desc

    compact_tool_definitions(tools)
    return tools


def compact_tool_definitions(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove schema fields that carry no constraint or model-facing information.

    The input is updated in place to preserve ``build_tools`` compatibility. Only
    empty descriptions and empty ``required`` arrays are removed; names, types,
    properties, enums, non-empty constraints, and tool availability are unchanged.
    """
    def _compact(value: Any) -> None:
        if isinstance(value, dict):
            if value.get('description') == '':
                del value['description']
            if value.get('required') == []:
                del value['required']
            for nested in value.values():
                _compact(nested)
        elif isinstance(value, list):
            for nested in value:
                _compact(nested)

    for tool in tools:
        _compact(tool)
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

    # If user_id provided, also return summary (actual LLM context extra)
    if user_id:
        session_id = db.get_or_create_session(agent_id, user_id)

        summary_record = db.get_summary(session_id, agent_id=agent_id)
        sum_tokens = 0
        if summary_record:
            summary_text = f"## Prior conversation summary\n{summary_record['summary']}"
            result["summary"] = summary_text
            sum_tokens = _token_count(summary_text)
            result["tokens"]["summary"] = sum_tokens

        # Recalculate total to include summary
        result["tokens"]["total"] = sp_tokens + tool_tokens + sum_tokens

    return result


def command_hint_from_content(content: str) -> str:
    """Extract a command hint from a serialized tool result JSON string.

    Used by build_message_entry() to route tool output through RTK compression.
    Falls back to "unknown" if the content format is unrecognizable.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return "unknown"

    if not isinstance(data, dict):
        return "unknown"

    # read_file: has file_path
    if "file_path" in data:
        return "read_file"

    # bash/runpy/exec tools: have exit_code + stdout/stderr
    if "exit_code" in data and ("stdout" in data or "stderr" in data):
        return "bash"

    # catch-all for any other structured dict
    return "unknown"


def build_attachment_note(attachment_info: dict,
                          has_describe_image: bool = True,
                          audio_enabled: bool = False) -> str:
    """Render authoritative attachment metadata for model-visible context.

    The database attachment ID is intentionally explicit so the model can call
    ``read_attachment(attachment_id=N)`` without inferring an ID from a session
    name or storage path.
    """
    file_path = attachment_info.get('file_path', '')
    if file_path and not os.path.isabs(file_path):
        file_path = os.path.abspath(os.path.join(_BASE_DIR, file_path))

    filename = attachment_info.get('filename', '')
    mime_type = attachment_info.get('mime_type') or 'application/octet-stream'
    size_bytes = int(attachment_info.get('size_bytes', 0) or 0)
    attachment_id = attachment_info.get('attachment_id')
    if size_bytes >= 1048576:
        size_str = f"{size_bytes / 1048576:.1f} MB"
    elif size_bytes >= 1024:
        size_str = f"{size_bytes / 1024:.1f} KB"
    else:
        size_str = f"{size_bytes} B"

    note = f"\n\n[Attachment: {filename} ({mime_type}, {size_str})]"
    if attachment_id is not None:
        note += f"\nAttachment ID: {attachment_id}"
    note += f"\nFile path: {file_path}"

    if mime_type.startswith('image/') and has_describe_image:
        note += "\nUse the `describe_image` tool to view and analyze this image."
    if mime_type.startswith('audio/') and audio_enabled:
        note += "\nUse the `transcribe_audio` tool to listen to this audio."
    return note


def build_attachment_notes(attachment_infos: list,
                           has_describe_image: bool = True,
                           audio_enabled: bool = False) -> str:
    """Render notes for multiple attachments, numbered when more than one."""
    notes = []
    count = len(attachment_infos)
    for index, info in enumerate(attachment_infos, 1):
        note = build_attachment_note(
            info,
            has_describe_image=has_describe_image,
            audio_enabled=audio_enabled,
        )
        if count > 1:
            note = note.replace('[Attachment:', f'[Attachment #{index}:', 1)
        notes.append(note)
    return ''.join(notes)


def attachment_infos_from_metadata(metadata: dict) -> list:
    """Return valid plural attachment metadata with a legacy singular fallback."""
    if not isinstance(metadata, dict):
        return []
    infos = metadata.get('attachment_infos')
    if not isinstance(infos, list):
        infos = []
    infos = [info for info in infos if isinstance(info, dict)]
    legacy = metadata.get('attachment_info')
    return infos or ([legacy] if isinstance(legacy, dict) else [])


def build_session_attachment_manifest(session_id: str, agent_id: str,
                                      exclude_ids=None) -> str:
    """Build a compact, authoritative metadata-only index of live session files."""
    excluded = {str(value) for value in (exclude_ids or ())}
    lines = []
    try:
        records = reversed(db.list_session_attachments(session_id, agent_id))
    except Exception:
        _logger.warning("Failed to load attachment manifest for session %s", session_id,
                        exc_info=True)
        return ''
    for record in records:
        if not isinstance(record, dict):
            continue
        attachment_id = record.get('id')
        if attachment_id is None or str(attachment_id) in excluded:
            continue
        file_path = record.get('file_path') or ''
        if file_path and not os.path.isabs(file_path):
            file_path = os.path.abspath(os.path.join(_BASE_DIR, file_path))
        if not file_path or not os.path.isfile(file_path):
            continue
        filename = re.sub(r'[\r\n|]+', '_', str(
            record.get('filename') or record.get('original_filename') or ''))
        mime_type = re.sub(r'[\r\n|]+', '_', str(
            record.get('mime_type') or 'application/octet-stream'))
        size_bytes = int(record.get('size_bytes') or 0)
        lines.append(
            f"- id={attachment_id} | filename={filename} | mime={mime_type} | "
            f"size={size_bytes} B | path={file_path}"
        )
    if not lines:
        return ''
    return (
        "## Session Attachments\n"
        "Persistent metadata for files uploaded in this session (no binary content):\n" +
        '\n'.join(lines)
    )


def sync_session_attachment_manifest(messages: list, session_id: str,
                                     agent_id: str) -> None:
    """Replace any cached manifest with a fresh one, excluding visible attachment notes."""
    messages[:] = [
        msg for msg in messages
        if not (msg.get('role') == 'system' and
                str(msg.get('content') or '').startswith('## Session Attachments\n'))
    ]
    visible_ids = set()
    pattern = re.compile(r'^Attachment ID:\s*(\d+)\s*$', re.MULTILINE)
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, str):
            visible_ids.update(pattern.findall(content))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    visible_ids.update(pattern.findall(part['text']))
    manifest = build_session_attachment_manifest(session_id, agent_id, visible_ids)
    if manifest:
        messages.insert(1, {'role': 'system', 'content': manifest})


def append_attachment_note(msg: dict,
                           attachment_info: dict,
                           has_describe_image: bool = True,
                           audio_enabled: bool = False) -> dict:
    """Append structured attachment metadata to a model message in-place."""
    note = build_attachment_note(
        attachment_info,
        has_describe_image=has_describe_image,
        audio_enabled=audio_enabled,
    )
    content = msg.get('content', '') or ''
    msg['content'] = content.rstrip() + note
    return msg


def build_message_entry(msg: dict, agent: dict, has_describe_image: bool = True) -> dict:
    """Convert a DB message row into an LLM message dict."""
    entry = {"role": msg['role']}
    _msg_meta = msg.get('metadata') if isinstance(msg.get('metadata'), dict) else {}
    msg_image = _msg_meta.get('image_url') if _msg_meta else None
    msg_video = _msg_meta.get('video_url') if _msg_meta else None
    # Images are NEVER auto-fed to the main LLM — always use the describe_image tool instead.
    # Audio is likewise never auto-fed — agents listen via the transcribe_audio tool.
    has_video = msg_video and agent.get('video_enabled')

    # Build attachment context note from authoritative structured metadata.
    # Prefer the plural attachment_infos list; fall back to the legacy singular
    # attachment_info when no valid plural entries are present.
    attachment_infos = _msg_meta.get('attachment_infos') if _msg_meta else None
    attachment_info = _msg_meta.get('attachment_info') if _msg_meta else None
    if not isinstance(attachment_infos, list):
        attachment_infos = []
    attachment_infos = [info for info in attachment_infos if isinstance(info, dict)]
    attachment_note = None
    if attachment_infos:
        attachment_note = build_attachment_notes(
            attachment_infos,
            has_describe_image=has_describe_image,
            audio_enabled=bool(agent.get('audio_enabled')),
        )
    elif attachment_info and isinstance(attachment_info, dict):
        attachment_note = build_attachment_note(
            attachment_info,
            has_describe_image=has_describe_image,
            audio_enabled=bool(agent.get('audio_enabled')),
        )

    if has_video:
        parts = []
        text_content = msg.get('content', '')
        if attachment_note:
            text_content = text_content.rstrip() + attachment_note
        if text_content and text_content not in ('[Image]', '[Audio]', '[Video]'):
            parts.append({"type": "text", "text": text_content})
        parts.append({"type": "video_url", "video_url": {"url": msg_video}})
        if not parts or parts[0].get('type') != 'text':
            parts.insert(0, {"type": "text", "text": "What is in this media?"})
        entry['content'] = parts
    elif msg.get('content'):
        content = msg['content']
        if attachment_note:
            content = content.rstrip() + attachment_note
        # Safety net: try RTK compression before falling back to blunt truncation.
        # Covers legacy DB entries and code paths that reach here outside llm_loop.
        if msg.get('role') == 'tool' and len(content) > MAX_TOOL_RESULT_CHARS:
            try:
                from backend.token_compressor.compressor_registry import get_registry
                reg = get_registry()
                hint = command_hint_from_content(content)
                # Assume exit_code=0 — we don't have it when reading from DB
                compressed = reg.compress(hint, 0, content)
                # Only use compressed result if it differs (filter actually matched)
                if compressed != content:
                    content = compressed
            except Exception:
                # Fail-open: fall through to old truncation behavior
                pass

            # Still apply blunt truncation if RTK didn't shrink enough
            if len(content) > MAX_TOOL_RESULT_CHARS:
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


def build_user_identity_context(channel_id: str, external_user_id: str):
    """Look up the channel user's display name and build an identity context block.

    Returns a string for insertion into the LLM conversation context, or None
    when the channel has no display name on file for this user.
    """
    if not channel_id or not external_user_id:
        return None

    try:
        display_name = db.get_user_display_name(channel_id, external_user_id)
    except Exception:
        _logger.warning(
            "Failed to look up display name for channel=%s user=%s",
            channel_id, external_user_id, exc_info=True,
        )
        return None

    if not display_name or display_name == 'unknown':
        return None

    return (
        "## Current User\n"
        f"You are currently speaking with: **{display_name}** "
        f"(channel user ID: `{external_user_id}`).\n"
        "This identity is provided by the chat channel and is authoritative "
        "for this session. If you have previously remembered a different name "
        "for this user — disregard it. Always address this user as "
        f"**{display_name}** throughout this conversation."
    )
