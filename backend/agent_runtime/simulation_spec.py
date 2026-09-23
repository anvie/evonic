"""
simulation_spec.py — inline spec resolution for agent-template simulations.

A simulated agent (see :mod:`backend.agent_runtime.simulation_runtime`) has NO
row in the ``agents`` table.  Every ``db.get_agent_*`` lookup would therefore
return empty, which would silently strip the template's tools, skills, model
and variables from the run and destroy fidelity.

To keep the run faithful, the ephemeral agent dict carries its resolved spec
inline under a small set of private keys.  This module is the ONE place that
knows how to read those keys and how to fall back to the DB for a normal agent,
so the guards scattered across ``context.py`` / ``runtime.py`` / ``prefetch.py``
stay one-liners.

These helpers are strict no-ops for non-simulation agents (they delegate to the
database exactly as before).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Truthy marker that identifies an in-memory simulation agent.
SIMULATION_KEY = "is_simulation"

#: Inline spec keys carried on the ephemeral agent dict.
SIM_TOOL_IDS = "_simulation_tool_ids"
SIM_SKILL_IDS = "_simulation_skill_ids"
SIM_VARIABLES = "_simulation_variables"          # {key: value}
SIM_VARIABLES_META = "_simulation_variables_meta"  # {key: is_secret}
SIM_MODEL = "_simulation_model"                   # model row dict or None


def is_simulation(agent: Optional[dict]) -> bool:
    """True when *agent* is an ephemeral simulation agent."""
    return bool(agent) and bool(agent.get(SIMULATION_KEY))


# ---------------------------------------------------------------------------
# Resolution helpers (simulation inline spec -> live DB fallback)
# ---------------------------------------------------------------------------

def tools(agent: Optional[dict], eid: Optional[str] = None) -> List[str]:
    """Assigned tool ids for *agent* (inline spec for sims)."""
    if is_simulation(agent):
        return list(agent.get(SIM_TOOL_IDS) or [])
    from models.db import db
    return db.get_agent_tools(eid if eid is not None else (agent or {}).get("id", ""))


def skills(agent: Optional[dict], eid: Optional[str] = None) -> List[str]:
    """Assigned skill ids for *agent* (inline spec for sims)."""
    if is_simulation(agent):
        return list(agent.get(SIM_SKILL_IDS) or [])
    from models.db import db
    return db.get_agent_skills(eid if eid is not None else (agent or {}).get("id", ""))


def variable_dict(agent: Optional[dict], eid: Optional[str] = None) -> Dict[str, str]:
    """Flat ``{key: value}`` variable map (inline spec for sims)."""
    if is_simulation(agent):
        return dict(agent.get(SIM_VARIABLES) or {})
    from models.db import db
    return db.get_agent_variables_dict(eid if eid is not None else (agent or {}).get("id", ""))


def variables(agent: Optional[dict], eid: Optional[str] = None) -> List[Dict[str, Any]]:
    """Variable rows ``{key, value, is_secret}`` (inline spec for sims)."""
    if is_simulation(agent):
        meta = agent.get(SIM_VARIABLES_META) or {}
        return [
            {"key": key, "value": value, "is_secret": 1 if meta.get(key) else 0}
            for key, value in sorted((agent.get(SIM_VARIABLES) or {}).items())
        ]
    from models.db import db
    return db.get_agent_variables(eid if eid is not None else (agent or {}).get("id", ""))


def model(agent: Optional[dict], eid: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Primary model row for *agent*, or None to fall back to the global default."""
    if is_simulation(agent):
        mid = agent.get(SIM_MODEL) or agent.get(SIM_MODEL_ID)
        if isinstance(mid, dict):
            return mid
        if not mid:
            return None
        from models.db import db
        return db.get_model_by_id(mid)
    from models.db import db
    return db.get_agent_model(eid if eid is not None else (agent or {}).get("id", ""))


# Backwards-compatible aliases: older call sites/tests used the ``*_ids`` /
# ``variables_dict`` names.
SIM_MODEL_ID = "_simulation_model_id"
tool_ids = tools
skill_ids = skills
variables_dict = variable_dict
