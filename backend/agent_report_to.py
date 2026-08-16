"""Resolve report_to_id / report_to_channel_id for agent and sub-agent messaging."""

from __future__ import annotations

from typing import Any, Dict, Tuple

_AGENT_MSG_PREFIX = "__agent__"


def resolve_report_to_from_context(
    agent_context: Dict[str, Any],
    sender_id: str,
) -> Tuple[str, str, str]:
    """Map sender context to a human-visible session for reply auto-forward."""
    report_to_id = agent_context.get("user_id", "") or ""
    report_to_channel_id = agent_context.get("channel_id", "") or ""
    session_id = agent_context.get("session_id", "") or ""

    if not report_to_id.startswith(_AGENT_MSG_PREFIX):
        return report_to_id, report_to_channel_id, session_id

    # Inter-agent turns retain their originating human route in the runtime
    # context. Prefer its explicit session ID so a newer web session cannot
    # replace the originating channel session during chained delegation.
    origin_report_to_id = agent_context.get("origin_report_to_id", "") or ""
    origin_report_to_channel_id = agent_context.get("origin_report_to_channel_id", "") or ""
    origin_session_id = agent_context.get("origin_session_id", "") or ""
    if origin_report_to_id and origin_session_id:
        return origin_report_to_id, origin_report_to_channel_id, origin_session_id

    lookup_id = sender_id
    if agent_context.get("is_subagent"):
        parent_id = agent_context.get("parent_id", "") or ""
        if parent_id:
            lookup_id = parent_id

    result = _lookup_human_session(lookup_id)
    return result[0], result[1], ""


def resolve_report_to_for_subagent_spawn(
    parent_agent_id: str,
    spawner_user_id: str,
    spawner_channel_id: str,
) -> Tuple[str, str, str]:
    """Resolve where a spawned sub-agent should report its final answer."""
    report_to_id = spawner_user_id or ""
    report_to_channel_id = spawner_channel_id or ""

    if report_to_id and not report_to_id.startswith(_AGENT_MSG_PREFIX):
        return report_to_id, report_to_channel_id, ""

    # Inter-agent session: preserve the routing so sub-agent/explorer findings
    # are forwarded back to the caller's current (inter-agent) session rather
    # than resolved to the parent's human session (e.g. test web chat tab).
    if report_to_id:
        return report_to_id, report_to_channel_id, ""

    result = _lookup_human_session(parent_agent_id)
    return result[0], result[1], ""


def _lookup_human_session(lookup_agent_id: str) -> Tuple[str, str]:
    from models.db import db

    human_sess = db.get_latest_human_session(lookup_agent_id)
    if human_sess:
        return human_sess.get("external_user_id", ""), human_sess.get("channel_id") or ""
    return "", ""
