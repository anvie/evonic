"""
Explorer sub-agent support — isolated logic for the `explorer` skill.

An explorer is an in-memory sub-agent (``is_subagent=True``, so it reuses the
sub-agent lifecycle: force-execute mode, /tmp chat DB, idle cleanup, report-back)
that — UNLIKE a normal sub-agent — does NOT inherit the parent's system prompt,
tools, model, or KB. Its configuration comes entirely from the `explorer` skill
settings.

Separation of concerns: the `explorer` skill is the DELEGATOR's tool (it provides
``Explore``), while the explorer sub-agent that does the actual work is granted the
`direxplorer` skill's read-only worker tools (Grep/Read/Glob). DirExplorer is
therefore a required dependency — ``Explore`` errors clearly if it is disabled.

All explorer-specific behavior lives here. The core hot paths (context.py,
runtime.py, prefetch.py, llm_loop.py) contain only one-line guards keyed on
``agent.get('is_explorer')`` that delegate here. ``is_explorer`` is set ONLY by
``Explore`` (via ``SubAgentManager.spawn_explorer``); it is never present on a
DB agent, the super agent, or a normal sub-agent, so every guard is a no-op for
them — guaranteeing zero behavior change for existing callers.
"""

from typing import Any, Dict, List, Optional, Tuple

SKILL_ID = 'explorer'

# The worker skill whose read-only tools every explorer runs with.
WORKER_SKILL_ID = 'direxplorer'

# DirExplorer's read-only tools. Always granted to explorers, non-removable.
MANDATORY_TOOL_IDS: List[str] = [
    'skill:direxplorer:Grep',
    'skill:direxplorer:Read',
    'skill:direxplorer:Glob',
]


def worker_skill_enabled() -> bool:
    """True if the DirExplorer worker skill (mandatory tool source) is enabled."""
    from backend.skills_manager import skills_manager
    return skills_manager.is_skill_enabled(WORKER_SKILL_ID)


def is_explorer(agent: Optional[Dict[str, Any]]) -> bool:
    return bool((agent or {}).get('is_explorer'))


def tool_ids(agent: Optional[Dict[str, Any]]) -> List[str]:
    """Tool IDs an explorer is authorized to use (mandatory clones + extras).

    Used by the core tool-resolution guards in place of ``db.get_agent_tools``.
    """
    return list((agent or {}).get('_explorer_tool_ids') or MANDATORY_TOOL_IDS)


def primary_model(agent: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Configured primary model for an explorer, or None to use the global default.

    Returns None for non-explorers and for explorers with no ``model_id`` set —
    in both cases the caller keeps its existing resolution (which already yields
    the global default for a row-less explorer id).
    """
    if not is_explorer(agent):
        return None
    mid = (agent or {}).get('model_id')
    if not mid:
        return None
    from models.db import db
    return db.get_model_by_id(mid)


def fallback_model(agent: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Configured fallback model for an explorer, or None."""
    if not is_explorer(agent):
        return None
    fid = (agent or {}).get('fallback_model_id')
    if not fid:
        return None
    from models.db import db
    return db.get_model_by_id(fid)


def _candidate_defs() -> List[Dict[str, Any]]:
    """All enabled tool defs, each tagged with a namespaced ``id``.

    Includes skill tools from LAZY skills (which ``get_all_skill_tool_defs``
    deliberately omits) plus the json/backend tools — because an explorer is
    granted its tools directly, bypassing ``use_skill``.
    """
    from backend.skills_manager import skills_manager
    from backend.tools import tool_registry
    out: List[Dict[str, Any]] = []
    for skill in skills_manager.list_skills():
        if not skill.get('enabled'):
            continue
        skill_id = skill.get('id', '')
        skill_dir = skill.get('_dir')
        if not skill_dir:
            continue
        for d in skills_manager._load_tool_defs(skill_dir, skill):
            fn = d.get('function', {}).get('name', '')
            dd = dict(d)
            dd['id'] = f"skill:{skill_id}:{fn}"
            out.append(dd)
    out.extend(tool_registry.get_tool_defs_from_json())
    return out


def resolve_tool_ids(extras_csv: str) -> Tuple[List[str], Optional[str]]:
    """Merge mandatory tool IDs with validated, comma-separated extras.

    Returns ``(tool_ids, error)``. ``error`` is non-None if any extra references
    an unknown or disabled tool — the caller should surface it and not spawn.
    """
    extras: List[str] = [t.strip() for t in (extras_csv or '').split(',') if t.strip()]
    if not extras:
        return list(MANDATORY_TOOL_IDS), None

    valid_ids = set()
    valid_fns = set()
    for d in _candidate_defs():
        tid = d.get('id', '')
        fn = d.get('function', {}).get('name', '')
        if tid:
            valid_ids.add(tid)
        if fn:
            valid_fns.add(fn)

    resolved: List[str] = list(MANDATORY_TOOL_IDS)
    for extra in extras:
        if extra in MANDATORY_TOOL_IDS:
            continue
        if extra not in valid_ids and extra not in valid_fns:
            return [], (
                f"Configured explorer tool '{extra}' is not a valid or enabled tool. "
                f"Check the Explorer skill's 'Extra explorer tools' setting."
            )
        if extra not in resolved:
            resolved.append(extra)
    return resolved, None


def tool_defs(agent: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI function tool defs for an explorer's tool ids (resolves lazy skills)."""
    wanted = set(tool_ids(agent))
    out: List[Dict[str, Any]] = []
    seen = set()
    for d in _candidate_defs():
        tid = d.get('id', '')
        fn = d.get('function', {}).get('name', '')
        if not fn or fn in seen:
            continue
        if tid in wanted or fn in wanted:
            seen.add(fn)
            out.append({"type": "function", "function": d['function']})
    return out


def build_config(
    parent_agent: Dict[str, Any],
    explorer_id: str,
    path: str,
    skill_cfg: Dict[str, Any],
    explorer_tool_ids: List[str],
) -> Dict[str, Any]:
    """Build the in-memory explorer agent config (NOT a copy of the parent)."""
    parent_id = parent_agent.get('id', '')
    parent_name = parent_agent.get('name', parent_id)
    return {
        'id': explorer_id,
        'name': f'{parent_name} · explorer',
        'is_subagent': True,        # reuse sub-agent lifecycle / messaging / cleanup
        'is_explorer': True,        # suppress parent inheritance (guards key on this)
        'parent_id': parent_id,
        '_db_agent_id': explorer_id,  # own id → no parent model/tools/KB
        'system_prompt': skill_cfg.get('system_prompt', '') or '',
        '_explorer_tool_ids': explorer_tool_ids,
        'model_id': (parent_agent.get('model_id') if skill_cfg.get('inherit_parent_model') else skill_cfg.get('model_id')) or None,
        'fallback_model_id': (parent_agent.get('fallback_model_id') if skill_cfg.get('inherit_parent_model') else skill_cfg.get('fallback_model_id')) or None,
        'workspace': path,          # boundary root for Grep/Read/Glob
        'agent_messaging_enabled': True,
        'builtin_tools_enabled': True,
        'enabled': True,
        'message_wrapper_enabled': False,   # explorers don't have remember()/recall() — wrapper wastes tokens
        # Inherit the delegator's EXECUTION ENVIRONMENT so the explorer runs the
        # same way the delegator does (sandbox on/off, remote workplace/tunnel,
        # run-as user). Prompt/tools/model are still the explorer's own.
        'sandbox_enabled': parent_agent.get('sandbox_enabled', 1),
        'workplace_id': parent_agent.get('workplace_id'),
        'run_as_user': parent_agent.get('run_as_user'),
        # carry the parent's user/channel so report-back routing resolves
        'user_id': parent_agent.get('user_id', ''),
        'user_phone': parent_agent.get('user_phone', ''),
        'user_jid': parent_agent.get('user_jid', ''),
        'user_id_namespace': parent_agent.get('user_id_namespace', ''),
        'channel_id': parent_agent.get('channel_id', '') or '',
    }
