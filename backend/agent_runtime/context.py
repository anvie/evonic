"""
context.py — builds LLM input: system prompt, tool list, message formatting.

Pure data preparation — no LLM calls, no threading.
"""
from __future__ import annotations

import base64
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
from backend.tools import tool_registry
from backend.skills_manager import SkillsManager, skills_manager
from backend.agent_runtime.evomem_client import (
    get_kb_graph_metadata,
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

# Module-level tool alias cache: { agent_eid: { alias: canonical_name } }
# Populated by build_tools() and consumed by _build_static_prompt(),
# _cache_key_valid(), and runtime.py context injection.
_tool_alias_cache: Dict[str, Dict[str, str]] = {}


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


def _extract_kb_frontmatter(filepath: str) -> dict:
    """Parse YAML front matter in a KB file and return description + tags.

    Returns a dict: {description: str|None, tags: [str]}.
    """
    result = {"description": None, "tags": []}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line != "---":
                return result
            for line in f:
                line_stripped = line.strip()
                if line_stripped == "---":
                    break
                if line_stripped.startswith("description:"):
                    val = line_stripped[len("description:"):].strip().strip("\"'")
                    result["description"] = val if val else None
                elif line_stripped.startswith("tags:"):
                    tag_val = line_stripped[len("tags:"):].strip()
                    if tag_val.startswith("[") and tag_val.endswith("]"):
                        inner = tag_val[1:-1].strip()
                        if inner:
                            result["tags"] = [t.strip().strip("\"'") for t in inner.split(",") if t.strip()]
    except Exception:
        pass
    return result


def _format_size(size_bytes: int) -> str:
    """Format file size in human-readable KB or MB."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / 1024:.1f} KB"


def _compute_staleness_flag(
    source_updated_at: str | None,
    target_slug: str,
    target_updated_at: dict,
) -> str:
    """Compute staleness flag for an outgoing link target.

    Returns a string like " ⚠ (updated 3 days ago, target may have changed)"
    or empty string if the target is not newer than the source.
    """
    if not source_updated_at:
        return ""
    target_ts = target_updated_at.get(target_slug)
    if not target_ts:
        return ""

    try:
        src_dt = datetime.fromisoformat(source_updated_at)
        tgt_dt = datetime.fromisoformat(target_ts)
    except (ValueError, TypeError):
        return ""

    if tgt_dt <= src_dt:
        return ""

    # Compute "N days ago" relative to target's update time
    now = datetime.now(timezone.utc)
    if tgt_dt.tzinfo is None:
        tgt_dt = tgt_dt.replace(tzinfo=timezone.utc)
    delta = now - tgt_dt
    days = delta.days
    if days == 0:
        age = "today"
    elif days == 1:
        age = "1 day ago"
    else:
        age = f"{days} days ago"

    return f" ⚠ (updated {age}, target may have changed)"


def _build_kb_listing(effective_id: str) -> list:
    """Build the KB listing.

    When _kb_index.md exists, shows its content as the primary listing,
    followed by auto-generated graph metadata. Otherwise falls back to
    a flat graph-aware listing.

    Returns a list of prompt lines, or empty list if no KB dir or no files.
    """
    kb_dir = os.path.join(_AGENTS_DIR, effective_id, 'kb')
    if not os.path.isdir(kb_dir):
        return []

    files = sorted(
        f for f in os.listdir(kb_dir)
        if os.path.isfile(os.path.join(kb_dir, f))
    )
    if not files:
        return []

    lines = []
    lines.append("\n## Available Knowledge Files")

    # --- Try to use _kb_index.md as canonical index ---
    index_path = os.path.join(kb_dir, '_kb_index.md')
    if os.path.isfile(index_path):
        # Read _kb_index.md content, strip YAML frontmatter
        index_body = ""
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                raw = f.read()
            # Strip YAML frontmatter (--- ... ---)
            if raw.startswith('---'):
                second = raw.find('---', 3)
                if second != -1:
                    index_body = raw[second + 3:].strip()
                else:
                    index_body = raw
            else:
                index_body = raw
        except Exception:
            index_body = ""

        if index_body:
            lines.append(
                "Your KB index (read with read(\"_kb_index.md\")):\n"
            )
            lines.append(index_body)
            lines.append("")

        # --- Auto-generated graph metadata (compact) ---
        graph = get_kb_graph_metadata(effective_id)
        graph_pages = graph["pages"] if graph else {}
        target_updated_at = graph["target_updated_at"] if graph else {}

        # Filter out _kb_index.md from graph metadata
        regular_files = [f for f in files if f != '_kb_index.md']
        if regular_files:
            lines.append("### Graph metadata (auto-generated):")
            if graph_pages:
                for f in regular_files:
                    gdata = graph_pages.get(f, {})
                    incoming = gdata.get('incoming_slugs', [])
                    outgoing = gdata.get('outgoing_slugs', [])
                    tags = gdata.get('tags', [])
                    tag_str = f" [tags: {', '.join(tags)}]" if tags else ""

                    inc_str = f"↑{len(incoming)} incoming" if incoming else "↑0 incoming"
                    out_str = f"→{len(outgoing)} outgoing" if outgoing else "→0 outgoing"

                    source_ts = gdata.get('updated_at')
                    if outgoing:
                        out_parts = []
                        for tgt in outgoing:
                            flag = _compute_staleness_flag(source_ts, tgt, target_updated_at)
                            out_parts.append(tgt + flag)
                        out_str = f"→ {', '.join(out_parts)}"

                    lines.append(f"- {f}: {inc_str}, {out_str}{tag_str}")
            else:
                for f in regular_files:
                    lines.append(f"- {f}: no graph data available yet")
            lines.append("")
    else:
        # --- Fallback: graph-aware listing (no _kb_index.md) ---
        lines.append(
            "You can read these files using the `read` tool. "
            "Use [[kb/filename]] to link between KB docs."
        )
        lines.append("")

        # Disk metadata
        file_info: dict = {}
        for f in files:
            fp = os.path.join(kb_dir, f)
            size = os.path.getsize(fp)
            fm = _extract_kb_frontmatter(fp)
            file_info[f] = {
                "size": size,
                "description": fm["description"],
                "tags": fm["tags"],
            }

        # Graph metadata from evomem
        graph = get_kb_graph_metadata(effective_id)
        graph_pages = graph["pages"] if graph else {}
        target_updated_at = graph["target_updated_at"] if graph else {}

        for slug, gdata in graph_pages.items():
            if slug in file_info:
                if gdata.get("tags"):
                    file_info[slug]["tags"] = gdata["tags"]

        for f in files:
            info = file_info[f]
            gdata = graph_pages.get(f, {})

            size_str = _format_size(info["size"])
            tags = info.get("tags") or gdata.get("tags") or []
            tag_str = f" [tags: {', '.join(tags)}]" if tags else ""
            desc = info["description"]
            if desc and len(desc) > 120:
                desc = desc[:117] + "..."
            desc_str = f" — {desc}" if desc else ""

            lines.append(f"- {f} ({size_str}){tag_str}{desc_str}")

            incoming = gdata.get("incoming_slugs", [])
            if incoming:
                lines.append(f"    ↑ referenced by: {', '.join(incoming)}")
            else:
                lines.append("    ↑ referenced by: <none>")

            outgoing = gdata.get("outgoing_slugs", [])
            if outgoing:
                source_ts = gdata.get("updated_at")
                out_parts = []
                for tgt in outgoing:
                    flag = _compute_staleness_flag(source_ts, tgt, target_updated_at)
                    out_parts.append(tgt + flag)
                lines.append(f"    → references: {', '.join(out_parts)}")
            else:
                lines.append("    → references: <none>")

            lines.append("")

    # --- KB Usage section (common to both paths) ---
    lines.append("### KB Usage")
    lines.append(
        "- **Save**: Use `write_file` with path `/_self/kb/filename` to "
        "store a new KB file."
    )
    lines.append(
        "- **Read**: Use the `read` tool with the bare filename (no path) "
        "to read a KB file."
    )
    lines.append(
        "- **KB vs Remember**: Use `read` for reference documents, guides, "
        "and long-form content. Use `remember` for short, searchable facts "
        "you want to recall across conversations."
    )
    lines.append(
        "- **Frontmatter**: KB files MUST include YAML frontmatter "
        "(delimited by `---` lines) with a `description` field. This "
        "description appears as a snippet in the \"Available Knowledge Files\" "
        "listing, helping agents decide whether to read the full file."
    )
    lines.append(
        "- **Best practices**: Store structured reference material in KB "
        "(specs, API docs, conventions). Keep each file focused on one topic. "
        "Update KB files when information changes. Always include frontmatter "
        "with a `description` when creating a new KB file."
    )
    lines.append(
        "- **Wiki-links**: Use `[[kb/filename]]` (without `.md` extension) "
        "to link between KB documents. "
        "Update `_kb_index.md` when adding new KB files."
    )

    # --- KB Coaching ---
    lines.append("")
    lines.append("### KB Coaching")
    lines.append(
        "When creating new KB files, add `[[kb/...]]` wiki-links to related "
        "documents so the knowledge graph stays connected. Use the `kb_graph` "
        "tool to explore existing link neighborhoods. Keep `_kb_index.md` "
        "updated when you add or remove KB documents."
    )

    # Inject notes.md instructions only if notes.md exists in KB
    if 'notes.md' in files:
        lines.append("")
        lines.append("### Notes.md - User Preferences & Instructions")
        lines.append(
            "You have a `notes.md` file in your KB. This file is your primary "
            "location for storing your user's personal preferences, tastes, "
            "language preferences, and communication style instructions."
        )
        lines.append("")
        lines.append("**Use notes.md for:**")
        lines.append(
            "- User's preferred language (e.g., 'User prefers Bahasa Indonesia')"
        )
        lines.append(
            "- Communication style preferences (e.g., 'User likes concise "
            "answers', 'User dislikes emoji')"
        )
        lines.append("- Personal instructions (e.g., 'Call the user Pak')")
        lines.append(
            "- Tastes and preferences (e.g., 'User prefers bullet points "
            "over paragraphs')"
        )
        lines.append("")
        lines.append("**Do NOT put in notes.md -- use `remember` instead:**")
        lines.append(
            "- Factual/memorization data: addresses, phone numbers, email, birthday"
        )
        lines.append(
            "- Secret/sensitive data: passwords, tokens, PINs, secret codes, "
            "bank accounts"
        )
        lines.append("")
        lines.append("**Usage rules:**")
        lines.append('- Read this file: `read(\"notes.md\")`')
        lines.append(
            "- Update via `write_file` with path `/_self/kb/notes.md`"
        )
        lines.append(
            "- Update immediately when the user communicates a new preference"
        )
        lines.append(
            "- Prioritize notes.md over `remember` for non-factual preference "
            "information"
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
        # Load tool aliases for heading rename — check cache first, fall back to DB
        _aliases = _tool_alias_cache.get(eid)
        if _aliases is None:
            _aliases = db.get_agent_tool_aliases(eid)
            _tool_alias_cache[eid] = _aliases
        # Build reverse map: canonical_name -> alias
        _canonical_to_alias = {v: k for k, v in _aliases.items()}

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
                    # Rename headings for aliased tools (e.g. "## read — Usage Rules" -> "## baca_berkas — Usage Rules")
                    if fn_name in _canonical_to_alias:
                        alias_name = _canonical_to_alias[fn_name]
                        tool_prompt = re.sub(
                            rf'^## {re.escape(fn_name)} ',
                            f'## {alias_name} ',
                            tool_prompt,
                            flags=re.MULTILINE,
                        )
                    parts.append(tool_prompt)

    # List available KB files with graph-aware metadata (links, tags, staleness)
    _kb_listing_lines = _build_kb_listing(eid)
    if _kb_listing_lines:
        parts.extend(_kb_listing_lines)

    # Message Wrapper Protocol
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
        "update notes.md (tastes/preferences/style), or update SYSTEM.md (critical rules)."
    )
    parts.append(
        "3. This applies to BOTH explicit and implicit cues. Even casual mentions count."
    )

    # Memory Retrieval Protocol — coach the agent on the retrieval side of
    # long-term memory (the capture side is covered above). Relevant facts are
    # auto-injected each turn, but the agent should reach for these tools when it
    # needs more than what was injected.
    parts.append("")
    parts.append("## Memory Retrieval Protocol")
    parts.append(
        "You have long-term memory that persists across conversations. Relevant facts "
        "are injected automatically each turn under a \"## Memory\" heading, so you "
        "usually do not need to fetch them. When you need MORE than what was injected:"
    )
    parts.append(
        "- `recall(query=\"...\")` — fast keyword lookup of a specific stored fact "
        "(e.g. a phone number, an address, a name)."
    )
    parts.append(
        "- `think(query=\"...\")` — reason over EVERYTHING you know about a topic; "
        "returns a synthesis plus what is still missing. Prefer this over `recall` for "
        "open questions like \"what do I know about the user's project?\"."
    )
    parts.append(
        "- `graph_query(entity=\"...\")` — follow relationships between people, "
        "organizations, and projects (e.g. where someone works, what they founded, "
        "who they advise)."
    )
    parts.append(
        "Look facts up instead of guessing or asking the user for something you may "
        "already know."
    )

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

    # Check tool aliases hash (alias changes must invalidate cache)
    tool_aliases = _tool_alias_cache.get(eid, db.get_agent_tool_aliases(eid))
    aliases_key = json.dumps(tool_aliases, sort_keys=True)
    if hashlib.sha256(aliases_key.encode()).hexdigest() != cache_entry.get('tools_alias_hash', ''):
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
        skills_hash = _get_skills_mtime_hash()

        assigned_ids = frozenset(db.get_agent_tools(eid))

        # Compute variables hash for cache invalidation
        current_vars = db.get_agent_variables(eid)
        vars_key = str(sorted((v['key'], v.get('is_secret', False)) for v in current_vars))
        vars_hash = hashlib.sha256(vars_key.encode()).hexdigest()

        # Compute tool aliases hash for cache invalidation
        _tool_aliases = _tool_alias_cache.get(eid, db.get_agent_tool_aliases(eid))
        _tool_alias_cache[eid] = _tool_aliases
        _aliases_key = json.dumps(_tool_aliases, sort_keys=True)
        _tools_alias_hash = hashlib.sha256(_aliases_key.encode()).hexdigest()

        _system_prompt_cache[aid] = {
            'static_prompt': static_prompt,
            'sp_mtime': _get_mtime(sp_path),
            'kb_mtime': _get_mtime(kb_dir),
            'evomem_mtime': get_evomem_db_mtime(eid),
            'skills_hash': skills_hash,
            'tools_hash': str(sorted(assigned_ids)),
            'tools_alias_hash': _tools_alias_hash,
            'ctx_mtime': _get_mtime(__file__),
            'sandbox_enabled': agent.get('sandbox_enabled', 0),
            'vars_hash': vars_hash,
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
        ("/detach", "Move the running long-running process (build/download) to the background so we can keep chatting — tracking is persistent (survives restarts) and you'll be notified to report the result when it finishes; the watcher is removed automatically, no cleanup needed"),
        ("/jobs", "List background jobs for this session"),
    ]
    slash_commands.append(("/plan", "Switch to plan mode"))
    slash_commands.append(("/unfocus", "Force-clear focus mode — use when agent is stuck in focus after a failed task"))
    if is_super:
        slash_commands.append(("/restart", "Restart the service (super agent only)"))
        slash_commands.append(("/cwd", "Show current workspace directory"))
        slash_commands.append(("/cd", "Change workspace directory"))
        slash_commands.append(("/shutdown", "Shut down the Evonic server completely (super agent only)"))
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
                "Use `save_artifact(source_path=\"...\")` for files already on disk (binaries, images, PDFs) "
                "or `save_artifact(content=\"...\")` for text generated in your response.\n"
                "You can also access it via `/_self/artifacts/` with any file tool.\n\n"
                f"**Artifact public URL**: `/api/agents/{aid}/artifacts/<filename>`\n"
                "This URL serves the file directly in the browser (no download prompt for images).\n"
                "To display an image inline in chat, save it via `save_artifact(source_path=\"...\")` "
                f"then embed in your markdown response: `<img src=\"/api/agents/{aid}/artifacts/filename.webp\" alt=\"...\">`\n\n"
                "**Important**: `/_self/` paths only work with file tools (`read_file`, `write_file`, `patch`, `str_replace`) "
                "— NOT with `bash` or `runpy`. When saving from bash/runpy, use the full workspace path "
                f"`{artifacts_path}` or the `save_artifact` tool."
            )
        else:
            artifacts_note = (
                f"Your artifacts are served at: `/api/agents/{aid}/artifacts/<filename>`\n"
                "Use the `save_artifact` tool to save files. "
                "Use `save_artifact(source_path=\"...\")` for files already on disk (binaries, images) "
                "or `save_artifact(content=\"...\")` for text generated in your response. "
                "You can also access the directory via `/_self/artifacts/` with file tools.\n\n"
                "To display an image inline in chat: save it via `save_artifact(source_path=\"...\")` "
                f"then embed in your markdown response: `<img src=\"/api/agents/{aid}/artifacts/filename.webp\" alt=\"...\">`\n\n"
                "**Important**: `/_self/` paths only work with file tools (`read_file`, `write_file`, `patch`, `str_replace`) "
                "— NOT with `bash` or `runpy`."
            )
        prompt += "\n\n## Artifacts Directory\n" + artifacts_note

    return prompt


def build_tools(agent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the OpenAI function tool list for this agent."""
    tools = []

    # Built-in tools (read, use_skill, set_mode, remember, recall, etc.)
    # Can be disabled per-agent via builtin_tools_enabled advanced setting.
    # Pass workplace_id so built-in factories can tailor descriptions for remote agents
    # (e.g. read() tool mentions /_self/kb/ when workplace_id is set).
    agent_context = {
        'id': agent['id'],
        'is_super': bool(agent.get('is_super')),
        'workplace_id': agent.get('workplace_id'),
    }
    if agent.get('builtin_tools_enabled', True):
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

    # Auto-inject eagerly loaded skill tools for assigned skills
    # This ensures that when an agent has a skill assigned in agent_skills and that skill
    # is eagerly loaded (no lazy_tools=true), the tools are available without manual
    # tool assignment in agent_tools.
    if not agent.get('is_super'):
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
                if skill.get('super_only', False):
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
        replacements = [
            ('in an isolated Docker container', 'in an isolated execution environment'),
            ('in a sandboxed Docker container', 'in a sandboxed execution environment'),
            ('The container is shared', 'The environment is shared'),
            ('The container persists', 'The environment persists'),
            ('tears down the container', 'tears down the environment'),
            ('tear down the container', 'tear down the environment'),
            ('destroys the shared runpy container', 'destroys the shared runpy environment'),
            ('local/Docker execution', 'local execution'),
            ('/workspace', 'the agents working directory'),
        ]
        for tool in tools:
            func = tool.get('function', {})
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

    # Strip empty description strings from all tool definitions.
    # OpenAI function calling spec treats description as optional;
    # removing empty strings saves tokens without losing information.
    for tool in tools:
        func = tool.get('function', {})
        if isinstance(func.get('description'), str) and func['description'] == '':
            del func['description']
        for param_def in func.get('parameters', {}).get('properties', {}).values():
            if isinstance(param_def, dict) and isinstance(param_def.get('description'), str) and param_def['description'] == '':
                del param_def['description']

    # --- Tool name aliasing: inject aliases from DB into tool schemas ---
    eid = _effective_id(agent)
    aliases = db.get_agent_tool_aliases(eid)
    _tool_alias_cache[eid] = aliases

    if aliases:
        # Build reverse map: canonical_name -> alias
        canonical_to_alias = {v: k for k, v in aliases.items()}
        for tool in tools:
            fn_name = tool.get('function', {}).get('name', '')
            if fn_name in canonical_to_alias:
                tool['function']['name'] = canonical_to_alias[fn_name]

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


def build_message_entry(msg: dict, agent: dict) -> dict:
    """Convert a DB message row into an LLM message dict."""
    entry = {"role": msg['role']}
    _msg_meta = msg.get('metadata') if isinstance(msg.get('metadata'), dict) else {}
    msg_image = _msg_meta.get('image_url') if _msg_meta else None
    msg_audio = _msg_meta.get('audio_url') if _msg_meta else None
    msg_video = _msg_meta.get('video_url') if _msg_meta else None
    # Images are NEVER auto-fed to the main LLM — always use the describe_image tool instead.
    has_audio = msg_audio and agent.get('audio_enabled')
    has_video = msg_video and agent.get('video_enabled')
    has_image_attachment = msg_image is not None  # track for attachment note enhancement

    # Build attachment context note if attachment_info is present in metadata
    attachment_info = _msg_meta.get('attachment_info') if _msg_meta else None
    attachment_note = None
    if attachment_info and isinstance(attachment_info, dict):
        file_path = attachment_info.get('file_path', '')
        filename = attachment_info.get('filename', '')
        mime_type = attachment_info.get('mime_type', 'application/octet-stream')
        size_bytes = int(attachment_info.get('size_bytes', 0) or 0)
        if size_bytes >= 1048576:
            size_str = f"{size_bytes / 1048576:.1f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"
        is_image = mime_type and mime_type.startswith("image/")
        if is_image and has_image_attachment:
            attachment_note = (
                f"\n\n[Attachment: {filename} ({mime_type}, {size_str})]"
                f"\nFile path: {file_path}"
                "\nUse the `describe_image` tool to view and analyze this image."
            )
        else:
            attachment_note = (
                f"\n\n[Attachment: {filename} ({mime_type}, {size_str})]"
                f"\nFile path: {file_path}"
            )

    if has_audio or has_video:
        parts = []
        text_content = msg.get('content', '')
        if attachment_note:
            text_content = text_content.rstrip() + attachment_note
        if text_content and text_content not in ('[Image]', '[Audio]', '[Video]'):
            parts.append({"type": "text", "text": text_content})
        # NOTE: Images are never auto-fed — use describe_image tool instead.
        if has_audio:
            if msg_audio.startswith("data:"):
                try:
                    header, b64data = msg_audio.split(",", 1)
                    fmt = header.split(":")[1].split(";")[0].split("/")[1]
                except (ValueError, IndexError):
                    fmt, b64data = "wav", msg_audio

                # Catch any OGG audio not converted at the channel level.
                # Some code paths or legacy data may still reach here with
                # format=ogg, which multimodal LLM APIs reject.
                if fmt == "ogg":
                    try:
                        from backend.audio_utils import convert_ogg_to_wav
                        raw = convert_ogg_to_wav(base64.b64decode(b64data))
                        b64data = base64.b64encode(raw).decode('utf-8')
                        fmt = "wav"
                    except Exception as conv_err:
                        _logger.error(
                            "OGG->WAV conversion fallback failed: %s -- "
                            "audio skipped for multimodal",
                            conv_err,
                        )
                        fmt, b64data = "wav", ""  # empty data = skip audio

                if b64data:
                    parts.append({"type": "input_audio", "input_audio": {"data": b64data, "format": fmt}})
            else:
                parts.append({"type": "input_audio", "input_audio": {"data": msg_audio, "format": "wav"}})
        if has_video:
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
