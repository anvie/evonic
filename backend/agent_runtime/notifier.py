"""
notifier.py — Centralized agent notification system.

Provides notify_agent() as the single entry point for all system/plugin
notifications directed at an agent. Handles:
- Tag-prefixed message composition
- Routing resolution (external_user_id + channel_id lookup)
- Deduplication against recent message history
- LLM triggering (trigger_llm=True) or DB-only storage (trigger_llm=False)
"""

from models.db import db
from backend.logging_config import get_logger

_logger = get_logger(__name__)


def notify_agent(agent_id: str, tag: str, message: str,
                 channel_type: str = None,
                 external_user_id: str = None,
                 channel_id: str = None,
                 session_id: str = None,
                 dedup: bool = True,
                 dedup_window: int = 5,
                 trigger_llm: bool = True,
                 deliver_external: bool = False,
                 metadata: dict = None) -> dict:
    """Send a system notification to an agent.

    Args:
        agent_id: Target agent ID.
        tag: Notification tag without brackets, e.g. "System/Task" or
             "SYSTEM NOTIFICATION". The full message will be prefixed as
             "[{tag}] {message}".
        message: Notification body text (without tag prefix).
        channel_type: Channel type hint for routing resolution, e.g. "telegram".
                      Used when external_user_id/channel_id are not provided.
        external_user_id: Explicit routing — the user ID for the target session.
                          If omitted, auto-resolved from the agent's most recent session.
        channel_id: Explicit routing — the channel UUID. If omitted, auto-resolved.
        session_id: If provided, target this existing session after validating
                    that it belongs to the target agent. Takes precedence over
                    external_user_id/channel_id.
        dedup: If True, skip sending if an identical message already exists in
               the last `dedup_window` messages of the session. Default True.
        dedup_window: Number of recent messages to check for duplicates. Default 5.
        trigger_llm: If True (default), route through handle_message() to trigger
                     the LLM loop. If False, save directly to DB without LLM processing
                     (use for informational notifications like system errors).
        deliver_external: When trigger_llm is False and the resolved session has an
                          active channel, also send the notification through it.
        metadata: Optional extra metadata dict merged into the saved message record.

    Returns:
        dict with keys:
          - success (bool)
          - session_id (str or None)
          - reason (str or None): failure reason, or None on success
          - route (str): "direct", "web_fallback", or "system_fallback"
          - fallback_reason (str or None): why a fallback route was selected
    """
    full_message = f"[{tag}] {message}"

    # Sub-agents don't have their own per-agent chat DB — use parent's ID for DB ops
    _db_agent_id = agent_id
    try:
        from backend.subagent_manager import subagent_manager
        _sub = subagent_manager.get(agent_id)
        if _sub:
            _db_agent_id = _sub.get('parent_id', agent_id)
    except Exception:
        pass

    fallback_reason = None
    route_kind = 'direct'

    try:
        if session_id:
            # Session IDs are global lookup keys, so validate ownership at this
            # central boundary before using any routing data from the session.
            session_info = db.get_session_with_details(session_id)
            if not session_info:
                _logger.warning(
                    "notify_agent: provided session_id '%s' not found for agent '%s'.",
                    session_id, agent_id,
                )
                return {"success": False, "session_id": None, "reason": "session_not_found"}
            if session_info.get('agent_id') != agent_id:
                _logger.warning(
                    "notify_agent: rejected session '%s' owned by '%s' for target agent '%s'.",
                    session_id, session_info.get('agent_id') or 'unknown', agent_id,
                )
                return {"success": False, "session_id": None, "reason": "session_owner_mismatch"}

            external_user_id = session_info.get('external_user_id')
            channel_id = session_info.get('channel_id')
            target_session_id = session_id

            if channel_id and not _is_channel_route_available(channel_id, agent_id):
                # An explicit, ownership-validated session is a caller's context
                # contract.  Keep it intact even when its external channel is down;
                # moving it to an unrelated web session contaminates that conversation.
                fallback_reason = 'inactive_channel'
                _logger.warning(
                    "notify_agent: channel '%s' for explicit session '%s' and agent '%s' "
                    "is inactive; preserving the explicit session.",
                    channel_id, session_id, agent_id,
                )
                metadata = dict(metadata) if metadata else {}
                metadata['notification_channel_unavailable'] = True
        else:
            explicit_route = external_user_id is not None or channel_id is not None
            if not external_user_id:
                resolved_uid, resolved_cid = _resolve_agent_target(_db_agent_id, channel_type)
                if resolved_uid:
                    external_user_id = resolved_uid
                    channel_id = channel_id or resolved_cid
                elif not explicit_route:
                    # Preserve system-notification behavior when the caller did
                    # not request a particular human/external route.
                    external_user_id = f"__system__{agent_id}"
                    channel_id = None
                    route_kind = 'system_fallback'

            if channel_id and not _is_channel_route_available(channel_id, agent_id):
                fallback_reason = 'inactive_channel'
                fallback = _resolve_web_fallback(agent_id)
                if not fallback:
                    _logger.warning(
                        "notify_agent: requested channel '%s' for agent '%s' is inactive; "
                        "no safe web fallback is available.",
                        channel_id, agent_id,
                    )
                    return {
                        "success": False,
                        "session_id": None,
                        "reason": "inactive_channel_no_fallback",
                    }
                target_session_id = fallback['id']
                external_user_id = fallback['external_user_id']
                channel_id = None
                route_kind = 'web_fallback'
            else:
                if not external_user_id:
                    return {"success": False, "session_id": None, "reason": "no_route"}
                target_session_id = db.get_or_create_session(
                    agent_id, external_user_id, channel_id,
                    db_agent_id=_db_agent_id)
    except Exception as e:
        _logger.error(
            "notify_agent: failed to get/create session for agent '%s' "
            "(external_user_id=%s, channel_id=%s): %s",
            agent_id, external_user_id, channel_id, e,
        )
        return {"success": False, "session_id": None, "reason": "error"}

    _logger.info(
        "notify_agent: resolved target_session_id='%s' for agent='%s' "
        "(route=%s, fallback_reason=%s).",
        target_session_id, agent_id, route_kind, fallback_reason or 'none',
    )

    # Deduplication check
    if dedup and _is_duplicate(target_session_id, full_message, dedup_window):
        _logger.info(
            "notify_agent: dedup — skipping duplicate [%s] notification for agent '%s' "
            "in session '%s'.",
            tag, agent_id, target_session_id,
        )
        return {
            "success": False,
            "session_id": target_session_id,
            "reason": "deduplicated",
            "route": route_kind,
            "fallback_reason": fallback_reason,
        }

    delivery = 'runtime' if trigger_llm else 'web'
    try:
        if trigger_llm:
            _logger.info(
                "notify_agent: triggering LLM for agent='%s' via handle_message "
                "(session='%s', channel=%s).",
                agent_id, target_session_id, channel_id or 'none',
            )
            from backend.agent_runtime import agent_runtime
            agent_runtime.handle_message(
                agent_id, external_user_id, full_message, channel_id,
                metadata=metadata, session_id=target_session_id,
            )
        else:
            meta = dict(metadata) if metadata else {}
            db.add_chat_message(
                target_session_id, role='user', content=full_message,
                agent_id=_db_agent_id, metadata=meta if meta else None,
            )
            from backend.event_stream import event_stream
            event_stream.emit('message_received', {
                'agent_id': agent_id,
                'session_id': target_session_id,
                'external_user_id': external_user_id,
                'channel_id': channel_id,
                'message': full_message,
                'metadata': meta,
            })
            if deliver_external and channel_id:
                from backend.channels.registry import channel_manager
                from backend.tools.channel_send_guard import wait_for_send_slot
                instance = channel_manager.get_channel_instance(channel_id)
                if not instance or not instance.is_running:
                    return {
                        "success": False,
                        "session_id": target_session_id,
                        "reason": "channel_unavailable",
                        "route": route_kind,
                        "fallback_reason": fallback_reason,
                        "delivery": "database_only",
                    }
                wait_for_send_slot(agent_id)
                instance.send_message(external_user_id, full_message)
                if instance.has_send_error(external_user_id):
                    instance.get_send_error(external_user_id)
                    return {
                        "success": False,
                        "session_id": target_session_id,
                        "reason": "channel_send_failed",
                        "route": route_kind,
                        "fallback_reason": fallback_reason,
                        "delivery": "database_only",
                    }
                delivery = 'external_channel'
    except Exception as e:
        import traceback as _tb
        _logger.error(
            "notify_agent: failed to notify agent '%s' (session='%s'): %s\n%s",
            agent_id, target_session_id, e, _tb.format_exc(),
        )
        return {"success": False, "session_id": target_session_id, "reason": "error"}

    return {
        "success": True,
        "session_id": target_session_id,
        "reason": None,
        "route": route_kind,
        "fallback_reason": fallback_reason,
        "delivery": delivery,
    }


def _resolve_web_fallback(agent_id: str, exclude_session_id: str = None):
    """Return an existing channel-less human web session owned by agent_id."""
    fallback = db.get_web_fallback_session(
        agent_id, exclude_session_id=exclude_session_id)
    if not fallback:
        return None

    external_user_id = fallback.get('external_user_id') or ''
    is_internal = external_user_id.startswith(
        ('__agent__', '__system__', '__scheduler__'))
    if (fallback.get('agent_id') == agent_id
            and not fallback.get('channel_id')
            and not is_internal):
        return fallback
    return None


def _is_channel_route_available(channel_id: str, agent_id: str = None) -> bool:
    """Return whether an owned persisted channel is enabled and running."""
    channel = db.get_channel(channel_id)
    if not channel or not channel.get('enabled'):
        return False
    if agent_id and channel.get('agent_id') != agent_id:
        return False

    from backend.channels.registry import channel_manager
    return channel_manager.is_running(channel_id)


def _resolve_agent_target(agent_id: str, channel_type: str = None):
    """Resolve (external_user_id, channel_id) for an agent.

    Priority:
    1. If the agent has a primary_channel_id and it's active → use it
    2. Otherwise, find the most recent active channel session
    3. Optionally filter by channel_type (e.g. "telegram")

    Returns (None, None) if not found.
    """
    try:
        import sqlite3
        from models.db import AgentChatDB

        channels = db.get_channels(agent_id)
        active_channel = None

        # Check primary channel first.
        primary_cid = db.get_primary_channel_id(agent_id)
        if primary_cid and _is_channel_route_available(primary_cid, agent_id):
            for ch in channels:
                if ch['id'] == primary_cid:
                    active_channel = ch
                    break

        if not active_channel:
            # Fallback: find any enabled channel with a live runtime instance.
            for ch in channels:
                ch_type = ch.get('type', '').lower()
                if channel_type and ch_type != channel_type.lower():
                    continue
                if _is_channel_route_available(ch['id'], agent_id):
                    active_channel = ch
                    break

        if not active_channel:
            return None, None

        resolved_channel_id = active_channel['id']
        agent_db = AgentChatDB(agent_id)
        with agent_db._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT external_user_id FROM chat_sessions
                WHERE agent_id = ? AND channel_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
            """, (agent_id, resolved_channel_id))
            row = cursor.fetchone()

        if not row:
            return None, None

        return row['external_user_id'], resolved_channel_id

    except Exception:
        return None, None


def _is_duplicate(session_id: str, full_message: str, window: int) -> bool:
    """Return True if full_message already appears in the last `window` user messages."""
    try:
        recent = db.get_session_messages(session_id, limit=window)
        return any(
            m.get('role') == 'user' and m.get('content') == full_message
            for m in recent
        )
    except Exception:
        return False
