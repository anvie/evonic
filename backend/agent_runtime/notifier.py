"""
notifier.py — Centralized agent notification system.

Provides notify_agent() as the single entry point for all system/plugin
notifications directed at an agent. Handles:
- Tag-prefixed message composition
- Routing resolution (external_user_id + channel_id lookup)
- Deduplication against recent message history
- LLM triggering (trigger_llm=True) or DB-only storage (trigger_llm=False)
"""

import json
import time as _time
from datetime import datetime, timezone

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
                 dedup_max_age_seconds: float = None,
                 trigger_llm: bool = True,
                 deliver_external: bool = False,
                 metadata: dict = None,
                 save_memory: bool = True,
                 ephemeral: bool = False,
                 ephemeral_ttl: int = 7200,
                 override_model_id: str = None,
                 lend_tools: list = None,
                 lend_tools_from: str = None) -> dict:
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
        dedup_max_age_seconds: Optional age limit for deduplication. When set, a
               matching message only suppresses the notification while it is newer
               than this many seconds. An older identical message (e.g. a stale-task
               reminder that was persisted but never processed because its turn was
               reaped) no longer blocks a fresh delivery. When None (default) any
               match in the window suppresses the notification, as before.
        trigger_llm: If True (default), route through handle_message() to trigger
                     the LLM loop. If False, save directly to DB without LLM processing
                     (use for informational notifications like system errors).
        deliver_external: When trigger_llm is False and the resolved session has an
                          active channel, also send the notification through it.
        metadata: Optional extra metadata dict merged into the saved message record.
        save_memory: When False, the exchange is never turned into the agent's memory: the message is marked
                     ``no_memory`` (the summarizer leaves it out) and, when this call picks or creates the session
                     itself, the whole session is flagged too. A session passed in with ``session_id`` already
                     belongs to someone else's conversation, so only the message is excluded there.
        ephemeral: When True, the session is disposable: it is removed once ``ephemeral_ttl`` seconds have
                   passed (see ``backend.ephemeral_sessions``). Meant for sessions this call creates.
        ephemeral_ttl: Lifetime in seconds of an ephemeral session. Default 2 hours.
        override_model_id: Model (llm_models id) the agent uses in this session instead of its own, stored as the
                     session's ``model_override_id``. It applies only to a disposable session this call opens
                     (``ephemeral=True`` and no ``session_id``): anywhere else it is ignored with a warning, so a
                     person's own conversation can never be switched to another model. An unknown or disabled
                     model is ignored too, and the agent keeps its own.
        lend_tools: Plugin tool ids (``plugin:<owner>:<fn>``) the sending plugin lends to the agent for this
                     message. They are advertised and authorized while that message is answered and, when the
                     resolved session is an agent-to-agent one, remembered for the session as well, so the rest of
                     that conversation keeps them (see ``backend.agent_runtime.session_tools``). They never become
                     part of the agent's tool assignment.
        lend_tools_from: The plugin those tools belong to; a lent id must be that plugin's own.

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

            if channel_id and not _is_channel_route_available(channel_id, agent_id, allow_shared=True):
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
    if dedup and _is_duplicate(target_session_id, full_message, dedup_window,
                               dedup_max_age_seconds):
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

    if ephemeral or not save_memory:
        try:
            from backend import ephemeral_sessions
            ephemeral_sessions.mark(target_session_id, agent_id, save_memory=save_memory or bool(session_id),
                                    ttl=ephemeral_ttl if ephemeral else None)
        except Exception:
            _logger.exception("notify_agent: could not flag session '%s'.", target_session_id)
        if not save_memory:
            metadata = {**(metadata or {}), 'no_memory': True}

    if override_model_id:
        _apply_session_model_override(target_session_id, _db_agent_id, override_model_id,
                                      allowed=bool(ephemeral and not session_id))

    # Tools a plugin lends with this message: carried in the metadata (the turn answering it sees them) and,
    # for an agent-to-agent session, remembered so the rest of that conversation keeps them too.
    if lend_tools and lend_tools_from:
        lent = [str(t).strip() for t in lend_tools if str(t).strip()]
        metadata = {**(metadata or {}), 'lend_tools': lent,
                    'lend_tools_from': str(lend_tools_from).strip()}
        try:
            from backend.agent_runtime import session_tools
            info = db.get_session_with_details(target_session_id) or {}
            if session_tools.is_inter_agent_session(info.get('external_user_id')):
                session_tools.grant(target_session_id, info.get('agent_id') or _db_agent_id,
                                    lend_tools_from, lent,
                                    granted_by=(metadata or {}).get('from_agent_id'))
        except Exception:
            _logger.warning('notify_agent: could not grant lent tools for session %s',
                            target_session_id, exc_info=True)

    delivery = 'runtime' if trigger_llm else 'web'
    try:
        if trigger_llm:
            _logger.info(
                "notify_agent: triggering LLM for agent='%s' via handle_message "
                "(session='%s', channel=%s).",
                agent_id, target_session_id, channel_id or 'none',
            )
            from backend.agent_runtime import agent_runtime
            hm_result = agent_runtime.handle_message(
                agent_id, external_user_id, full_message, channel_id,
                metadata=metadata, session_id=target_session_id,
            )
            # A runtime turn that short-circuits (busy injection, buffering, or
            # an async inter-agent hop) hands the message to a live loop rather
            # than processing it here.  Report that distinctly so callers that
            # need a confirmed resume (e.g. the Kanban stale-task watchdog) can
            # tell "queued into an active turn" apart from "actually handled".
            if isinstance(hm_result, dict):
                if hm_result.get('injected'):
                    delivery = 'injected'
                elif hm_result.get('buffered'):
                    delivery = 'buffered'
                elif hm_result.get('async'):
                    delivery = 'async'
        else:
            meta = dict(metadata) if metadata else {}
            message_id = db.add_chat_message(
                target_session_id, role='user', content=full_message,
                agent_id=_db_agent_id, metadata=meta if meta else None,
            )
            message_id = message_id if type(message_id) in (int, str) else None
            from models.chatlog import chatlog_manager
            chatlog_manager.get(_db_agent_id, target_session_id).append({
                'type': 'user', 'session_id': target_session_id,
                'content': full_message, 'metadata': meta,
                'message_id': message_id,
            })
            from backend.event_stream import event_stream
            event_stream.emit('message_received', {
                'agent_id': agent_id,
                'session_id': target_session_id,
                'external_user_id': external_user_id,
                'channel_id': channel_id,
                'message': full_message,
                'metadata': meta,
                'message_id': message_id,
                'role': 'user',
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


def _apply_session_model_override(session_id: str, agent_id: str, model_id: str, allowed: bool = False) -> bool:
    """Store ``model_id`` as the session's model override (read by the LLM loop). True when it was stored."""
    if not allowed:
        _logger.warning("notify_agent: override_model_id ignored for session '%s': only a new ephemeral session "
                        "takes one.", session_id)
        return False
    try:
        model = db.get_model_by_id(model_id)
        if not model or not model.get('enabled', True):
            _logger.warning("notify_agent: override_model_id '%s' is unknown or disabled; the agent keeps its own "
                            "model.", model_id)
            return False
        raw = db.get_session_state(session_id, agent_id=agent_id)
        try:
            state = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state['model_override_id'] = model_id
        db.upsert_session_state(session_id, json.dumps(state), agent_id=agent_id)
        return True
    except Exception:
        _logger.exception("notify_agent: could not set the model override of session '%s'.", session_id)
        return False


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


def _is_channel_route_available(channel_id: str, agent_id: str = None, allow_shared: bool = False) -> bool:
    """Return whether an owned persisted channel is enabled and running.

    ``allow_shared`` also accepts a shared channel (one with no owner agent). A caller that
    passes an explicit, ownership-validated session on such a channel has already been told
    which conversation it wants, so it is checked for liveness only, not for ownership.
    """
    channel = db.get_channel(channel_id)
    if not channel or not channel.get('enabled'):
        return False
    owner = channel.get('agent_id')
    if agent_id and owner != agent_id and not (allow_shared and owner is None):
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


def _parse_created_at(value) -> float | None:
    """Parse a chat message ``created_at`` into a POSIX timestamp (UTC).

    Accepts the SQLite ``CURRENT_TIMESTAMP`` form (``YYYY-MM-DD HH:MM:SS``,
    naive UTC) as well as ISO-8601 strings. Returns None if it cannot be read.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(text, fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _is_duplicate(session_id: str, full_message: str, window: int,
                  max_age_seconds: float = None) -> bool:
    """Return True if full_message already appears in the last `window` messages.

    When ``max_age_seconds`` is provided, only matches newer than that age count
    as duplicates; an older identical message (an unprocessed reminder whose
    turn was reaped) no longer suppresses delivery.  Matches whose timestamp
    cannot be parsed are treated as recent (deduplicated) to preserve the
    previous conservative behavior.
    """
    try:
        recent = db.get_session_messages(session_id, limit=window)
    except Exception:
        return False

    cutoff = None if max_age_seconds is None else _time.time() - max_age_seconds
    for m in recent:
        if m.get('role') != 'user' or m.get('content') != full_message:
            continue
        if cutoff is None:
            return True
        ts = _parse_created_at(m.get('created_at'))
        if ts is None or ts >= cutoff:
            return True
    return False
