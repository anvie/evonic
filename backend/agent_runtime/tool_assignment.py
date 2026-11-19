"""Implicit tool assignments shared by every assigned_tool_ids builder.

Some tools only make sense as companions of another tool and are never stored
in the ``agent_tools`` table: ``monitor`` is useless without ``bash`` (only bash
can start a background process), and ``bash`` implies it.

``context.build_tools`` has always added these companions to the tool
definitions it shows the model. The authorization guard in
``backend/tools/registry.py`` however only accepts tools present in
``assigned_tool_ids``, which is built separately by ``runtime.py`` and
``prefetch.py``. When those two builders drift from build_tools the model is
told to use a tool that is then blocked, which is a dead end (joko-8bc8d7b3:
monitor advertised, denied, 21 consecutive ``sleep 9`` polls).

Keep the rule here so the three call sites cannot drift again.
"""

from typing import Iterable


#: Implicit tool id -> the assigned tool id that implies it.
IMPLICIT_COMPANIONS = {
    'monitor': 'bash',
}


def implicit_tool_ids(assigned_tool_ids: Iterable[str]) -> set:
    """Return the implicit companion tool ids implied by an assignment."""
    ids = set(assigned_tool_ids)
    return {
        tool_id for tool_id, implied_by in IMPLICIT_COMPANIONS.items()
        if implied_by in ids and tool_id not in ids
    }


def apply_implicit_tools(assigned_tool_ids):
    """Add implicit companion tools to an assignment, in place when possible.

    The three builders use different containers (a set in ``build_tools``, a
    list in ``runtime.py`` and ``prefetch.py``), so both are supported. Returns
    the same container for convenience. Idempotent.
    """
    extra = implicit_tool_ids(assigned_tool_ids)
    if not extra:
        return assigned_tool_ids
    if isinstance(assigned_tool_ids, set):
        assigned_tool_ids.update(extra)
    else:
        assigned_tool_ids.extend(sorted(extra))
    return assigned_tool_ids
