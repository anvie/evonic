"""
Agent-to-Agent Messaging Tools

Allows agents to send messages to other agents with fire-and-forget semantics.
Messages are delivered as [AGENT/<sender_name>] tagged user messages in a
dedicated inter-agent session (external_user_id = "__agent__<sender_id>"),
keeping them separate from human user sessions.

When the target agent replies, the response is automatically forwarded back
to the sender's user session via the event stream — no polling needed.

Guard rails:
- Self-messaging is blocked
- Rate limit: max 10 messages per (sender, target) pair per 60 seconds
- Depth limit: max 3 hops in a chain (A→B→C→stop) to prevent infinite loops
- Global rate limit: max 30 messages per sender per 60 seconds (across all targets)
- Fan-out limit: max 5 unique targets per 5-second window (per LLM turn)
"""

import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Dict, List

from backend.logging_config import get_logger
from models.db import db

_logger = get_logger(__name__)

_AGENT_MSG_PREFIX = "__agent__"
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60  # seconds
_MAX_DEPTH = 3

# Global rate limit: max messages per sender across ALL targets
_GLOBAL_RATE_LIMIT_MAX = 30
_GLOBAL_RATE_LIMIT_WINDOW = 60  # seconds

# Fan-out limit: max unique targets per sender per short window (proxy for "one LLM turn")
_FANOUT_MAX_TARGETS = 5
_FANOUT_WINDOW = 5  # seconds

# Rate limit state: maps (sender_id, target_id) → list of timestamps
_rate_limit_buckets: Dict[tuple, list] = defaultdict(list)

# Global rate limit state: maps sender_id → list of timestamps (across all targets)
_global_rate_limit_buckets: Dict[str, list] = defaultdict(list)

# Fan-out state: maps sender_id → list of (timestamp, target_id) tuples
_fanout_buckets: Dict[str, list] = defaultdict(list)


def _check_rate_limit(sender_id: str, target_id: str) -> bool:
    """Return True if the message is allowed, False if rate-limited."""
    key = (sender_id, target_id)
    now = time.time()
    bucket = _rate_limit_buckets[key]
    # Prune entries outside the window
    _rate_limit_buckets[key] = [t for t in bucket if now - t < _RATE_LIMIT_WINDOW]
    if len(_rate_limit_buckets[key]) >= _RATE_LIMIT_MAX:
        return False
    _rate_limit_buckets[key].append(now)
    return True


def _check_global_rate_limit(sender_id: str) -> bool:
    """Return True if allowed, False if sender's global rate limit is exceeded."""
    now = time.time()
    bucket = _global_rate_limit_buckets[sender_id]
    # Prune entries outside the window
    _global_rate_limit_buckets[sender_id] = [t for t in bucket if now - t < _GLOBAL_RATE_LIMIT_WINDOW]
    if len(_global_rate_limit_buckets[sender_id]) >= _GLOBAL_RATE_LIMIT_MAX:
        return False
    _global_rate_limit_buckets[sender_id].append(now)
    return True


def _check_fanout_limit(sender_id: str, target_id: str) -> bool:
    """Return True if allowed, False if sender is fanning out to too many targets."""
    now = time.time()
    bucket = _fanout_buckets[sender_id]
    # Prune entries outside the window
    _fanout_buckets[sender_id] = [(t, tid) for t, tid in bucket if now - t < _FANOUT_WINDOW]
    # Count unique targets currently in window
    targets_in_window = {tid for _, tid in _fanout_buckets[sender_id]}
    # If target is new and we already have max unique targets → block
    if target_id not in targets_in_window and len(targets_in_window) >= _FANOUT_MAX_TARGETS:
        return False
    _fanout_buckets[sender_id].append((now, target_id))
    return True


def _get_message_depth(agent_context: dict) -> int:
    """Extract current message depth from agent_context metadata, defaulting to 0."""
    return int(agent_context.get('agent_message_depth', 0))


# ==================== Tool Definitions ====================

_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "send_agent_message",
            "description": (
                "Send a message to another agent on this platform. "
                "The message is delivered asynchronously — the target agent will process it "
                "and their reply will be automatically forwarded back to you (fire-and-forget). "
                "Use this for delegation, collaboration, or requesting specialist help. "
                "By default, the message is delivered to the agent's inter-agent session "
                "(__agent__&lt;sender-id&gt;). Pass 'session' to target a specific session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_agent_id": {
                        "type": "string",
                        "description": "The ID of the agent to send the message to (lowercase snake_case)."
                    },
                    "message": {
                        "type": "string",
                        "description": "The message content to send."
                    },
                    "session": {
                        "type": "string",
                        "description": "The target session ID to send the message to. If omitted, defaults to the agent's inter-agent session (__agent__<sender-id>)."
                    }
                },
                "required": ["target_agent_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_user",
            "description": (
                "Forward a message to your human user session when you need their input "
                "while processing in an inter-agent conversation. Use this to escalate "
                "approval requests or ask clarifying questions that only the user can answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to forward to the user."
                    }
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_agent_approval",
            "description": (
                "Approve or reject a pending tool-call approval from another agent. "
                "Use this when you receive an approval request notification from an agent you messaged. "
                "The approval_id is included in the notification message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "approval_id": {
                        "type": "string",
                        "description": "The approval ID from the notification message."
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "reject"],
                        "description": "Whether to approve or reject the tool execution."
                    }
                },
                "required": ["approval_id", "decision"]
            }
        }
    }
]


# ==================== Executors ====================

def _exec_send_agent_message(args: dict, agent_context: dict) -> dict:
    import re as _re
    sender_id = agent_context.get('id', '')
    sender_name = agent_context.get('name', sender_id)
    target_id = args.get('target_agent_id', '').strip().lower()
    message = args.get('message', '').strip()

    if not target_id:
        return {'error': 'target_agent_id is required.'}
    if not message:
        return {'error': 'message is required.'}
    if not _re.match(r'^[a-z0-9_]+$', target_id):
        return {'error': 'Invalid target_agent_id. Must be lowercase snake_case (alphanumeric and underscores only).'}

    # Prevent self-messaging
    if target_id == sender_id:
        _logger.warning("Agent '%s' attempted to send a message to itself — blocked.", sender_id)
        return {'error': 'An agent cannot send a message to itself.'}

    # Prevent reply-back loops: block sending to the agent that sent us this task.
    # B should end its turn with a final answer — _on_final_answer auto-forwards it to A.
    from_agent_id = agent_context.get('from_agent_id', '')
    if from_agent_id and target_id == from_agent_id:
        _logger.warning(
            "Agent '%s' tried to send_agent_message back to sender '%s' — blocked to prevent loop.",
            sender_id, from_agent_id,
        )
        return {
            'error': (
                "Cannot send a message back to the agent who delegated this task to you. "
                "Simply end your turn with a response — it will be automatically forwarded "
                "back to the sender. If you need human input, use escalate_to_user instead."
            )
        }

    # Sub-agents can only message their parent agent
    if agent_context.get('is_subagent'):
        parent_id = agent_context.get('parent_id', '')
        if target_id != parent_id:
            _logger.warning(
                "Sub-agent '%s' tried to message '%s' — blocked (can only message parent '%s').",
                sender_id, target_id, parent_id,
            )
            return {
                'error': (
                    f"Sub-agents can only send messages to their parent agent ('{parent_id}'). "
                    f"End your turn with a response — it will be automatically forwarded to the parent."
                )
            }

    # Validate target agent
    target_agent = db.get_agent(target_id)
    if not target_agent:
        # Check for in-memory sub-agent
        from backend.subagent_manager import subagent_manager
        target_agent = subagent_manager.get(target_id)
    if not target_agent:
        _logger.warning("Agent '%s' tried to message non-existent target '%s'.", sender_id, target_id)
        return {'error': f"Agent '{target_id}' not found."}
    if not target_agent.get('is_super') and not target_agent.get('enabled', True):
        _logger.warning("Agent '%s' tried to message disabled agent '%s'.", sender_id, target_id)
        return {'error': f"Agent '{target_agent.get('name', target_id)}' is currently disabled."}

    # Global rate limit — cap total messages per sender across all targets
    if not _check_global_rate_limit(sender_id):
        _logger.warning(
            "Global rate limit hit: '%s' sent %d messages in %ds window.",
            sender_id, _GLOBAL_RATE_LIMIT_MAX, _GLOBAL_RATE_LIMIT_WINDOW,
        )
        return {
            'error': (
                f"Global rate limit exceeded: maximum {_GLOBAL_RATE_LIMIT_MAX} messages "
                f"per {_GLOBAL_RATE_LIMIT_WINDOW}s per agent."
            )
        }

    # Fan-out limit — cap unique targets in a short window (proxy for one LLM turn)
    if not _check_fanout_limit(sender_id, target_id):
        _logger.warning(
            "Fan-out limit hit: '%s' tried to message too many targets in %ds window.",
            sender_id, _FANOUT_WINDOW,
        )
        return {
            'error': (
                f"Fan-out limit exceeded: maximum {_FANOUT_MAX_TARGETS} unique targets "
                f"per {_FANOUT_WINDOW}s window."
            )
        }

    # Rate limit check (per sender→target pair)
    if not _check_rate_limit(sender_id, target_id):
        _logger.warning(
            "Rate limit hit: '%s' → '%s' (%d messages in %ds window).",
            sender_id, target_id, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW,
        )
        return {
            'error': (
                f"Rate limit exceeded: maximum {_RATE_LIMIT_MAX} messages "
                f"per {_RATE_LIMIT_WINDOW}s to the same agent."
            )
        }

    # Depth guard — prevent infinite chain reactions
    current_depth = _get_message_depth(agent_context)
    if current_depth >= _MAX_DEPTH:
        _logger.warning(
            "Depth limit reached: '%s' at depth %d (max %d). Chain stopped.",
            sender_id, current_depth, _MAX_DEPTH,
        )
        return {
            'error': (
                f"Message depth limit reached ({_MAX_DEPTH}). "
                "Cannot forward agent messages further down the chain."
            )
        }

    # Build the tagged message content and metadata
    tagged_message = f"[AGENT/{sender_name}] {message}"

    from backend.agent_report_to import resolve_report_to_from_context

    reply_to_id = str(uuid.uuid4())
    report_to_id, report_to_channel_id = resolve_report_to_from_context(
        agent_context, sender_id,
    )
    if (agent_context.get('user_id', '') or '').startswith(_AGENT_MSG_PREFIX) and not report_to_id:
        _logger.warning(
            "send_agent_message: no human session found for sender '%s'. "
            "Reply auto-forward will be skipped.",
            sender_id,
        )

    metadata = {
        'agent_message': True,
        'from_agent_id': sender_id,
        'from_agent_name': sender_name,
        'agent_message_depth': current_depth + 1,
        'reply_to_id': reply_to_id,
        'report_to_id': report_to_id,
        'report_to_channel_id': report_to_channel_id,
    }

    # Deliver via notify_agent (handles routing, dedup, and LLM triggering)
    from backend.agent_runtime.notifier import notify_agent
    target_session = args.get('session', '').strip() if args.get('session') else None
    result = notify_agent(
        agent_id=target_id,
        tag=f"AGENT/{sender_name}",
        message=message,
        external_user_id=(f"{_AGENT_MSG_PREFIX}{sender_id}" if not target_session else None),
        channel_id=None,
        session_id=target_session,
        dedup=False,
        metadata=metadata,
    )

    _logger.info(
        "Agent message sent: '%s' → '%s' (depth=%d, reply_to=%s, report_to=%s, "
        "report_to_channel=%s, notify_result=%s).",
        sender_id, target_id, current_depth + 1, reply_to_id, report_to_id,
        report_to_channel_id or 'none', result,
    )

    return {
        'success': True,
        'message': f"Message sent to {target_agent.get('name', target_id)}.",
        'reply_to_id': reply_to_id,
        'tip': f"Reply from {target_agent.get('name', target_id)} will be automatically forwarded to your session."
    }


def _exec_escalate_to_user(args: dict, agent_context: dict) -> dict:
    agent_id = agent_context.get('id', '')
    current_user_id = agent_context.get('user_id', '')
    message = args.get('message', '').strip()

    if not message:
        return {'error': 'message is required.'}

    if not current_user_id.startswith('__agent__'):
        _logger.debug("Agent '%s' already in user session — escalate skipped.", agent_id)
        return {'error': 'Already in a user session — use send_agent_message or reply directly.'}

    # Priority 1: send to the primary session (prefers channel sessions like Telegram)
    primary_session = db.get_latest_human_session(agent_id)
    if not primary_session:
        _logger.warning("Escalate failed: no human session found for agent '%s'.", agent_id)
        return {'error': 'No active human user session found for this agent.'}

    from backend.agent_runtime.notifier import notify_agent

    def _deliver(session: dict) -> None:
        notify_agent(
            agent_id=agent_id,
            tag='SYSTEM',
            message=message,
            external_user_id=session['external_user_id'],
            channel_id=session.get('channel_id'),
            dedup=False,
            trigger_llm=False,
            metadata={'escalated_from_agent_session': True},
        )

    _deliver(primary_session)
    _logger.info("Agent '%s' escalated message to primary session '%s' (channel=%s).",
                 agent_id, primary_session['external_user_id'], primary_session.get('channel_id'))

    # Priority 2: also deliver to a web fallback session (no channel),
    # so the user can see the message in the web UI too.
    secondary = db.get_web_fallback_session(
        agent_id,
        exclude_session_id=primary_session.get('id'),
    )
    if secondary:
        _deliver(secondary)
        _logger.info("Agent '%s' also escalated message to web session '%s'.",
                     agent_id, secondary['external_user_id'])

    return {
        'success': True,
        'message': 'Message forwarded to user session.',
    }


def _exec_resolve_agent_approval(args: dict, agent_context: dict) -> dict:
    approval_id = args.get('approval_id', '').strip()
    decision = args.get('decision', '').strip()

    if not approval_id:
        return {'error': 'approval_id is required.'}
    if decision not in ('approve', 'reject'):
        return {'error': 'decision must be "approve" or "reject".'}

    from backend.agent_runtime.approval import approval_registry
    pa = approval_registry.get(approval_id)
    if pa is None:
        _logger.warning("Approval '%s' not found or expired.", approval_id)
        return {'error': 'Approval not found or already expired.'}
    if pa.decision is not None:
        _logger.warning("Approval '%s' already resolved as '%s'.", approval_id, pa.decision)
        return {'error': f'Approval already resolved: {pa.decision}.'}

    resolved = approval_registry.resolve(approval_id, decision)
    if not resolved:
        _logger.warning("Approval '%s' could not be resolved (just expired).", approval_id)
        return {'error': 'Could not resolve approval (may have just expired).'}

    _logger.info("Approval '%s' %sd for session '%s'.", approval_id, decision, pa.session_id)
    return {
        'success': True,
        'decision': decision,
        'message': f'Tool execution {decision}d for agent session {pa.session_id}.',
    }


# ==================== Fire-and-Forget: auto-forward B's reply to A ====================


def _on_final_answer(data: dict) -> None:
    """Event listener: when agent B finishes a turn in an inter-agent session,
    forward the reply to agent A's user session so A can relay it to the user."""
    external_user_id = data.get('external_user_id', '')

    # Only handle inter-agent sessions
    if not external_user_id or not external_user_id.startswith('__agent__'):
        return

    agent_b_id = data.get('agent_id', '')
    session_id = data.get('session_id', '')
    answer = data.get('answer', '')

    if not agent_b_id or not session_id or not answer:
        _logger.debug(
            "Auto-forward skip: incomplete event data (agent_b=%s, session=%s, has_answer=%s).",
            agent_b_id, session_id, bool(answer),
        )
        return

    sender_id = external_user_id[len('__agent__'):]  # Agent A

    _logger.info(
        "Auto-forward: '%s' finished turn in inter-agent session '%s' (sender='%s'). "
        "Looking up report_to metadata...",
        agent_b_id, session_id, sender_id,
    )

    # Resolve DB agent ID — sub-agents use their parent's per-agent chat DB
    _db_agent_id = agent_b_id
    try:
        from backend.subagent_manager import subagent_manager
        _sub = subagent_manager.get(agent_b_id)
        if _sub:
            _db_agent_id = _sub.get('parent_id', agent_b_id)
    except Exception:
        pass

    # Find the original message metadata from A
    try:
        messages = db.get_session_messages(session_id, limit=20, agent_id=_db_agent_id)
    except Exception as e:
        _logger.warning(
            "Auto-forward: could not fetch session messages for '%s' (agent_b=%s): %s",
            session_id, agent_b_id, e,
        )
        return

    report_to_id = None
    report_to_channel_id = None
    original_depth = 0
    for msg in reversed(messages):
        meta = msg.get('metadata') or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        if meta.get('from_agent_id') == sender_id:
            report_to_id = meta.get('report_to_id')
            report_to_channel_id = meta.get('report_to_channel_id') or None
            original_depth = meta.get('agent_message_depth', 0)
            break

    if not report_to_id:
        # The originating message may be older than the recent-message window
        # (e.g. when B executed many tool calls).  Fall back to a targeted DB
        # query that finds the first agent-request message in the session.
        _logger.debug(
            "Auto-forward: report_to_id not found in recent %d messages for '%s' "
            "in session '%s' — falling back to latest-agent-request lookup.",
            len(messages), sender_id, session_id,
        )
        try:
            latest_meta = db.get_latest_agent_request_metadata(
                session_id, agent_id=_db_agent_id, sender_agent_id=sender_id,
            )
        except Exception as e:
            _logger.warning("Auto-forward: latest-agent-request fallback failed for '%s': %s", session_id, e)
            latest_meta = None
        if latest_meta and latest_meta.get('from_agent_id') == sender_id:
            report_to_id = latest_meta.get('report_to_id')
            report_to_channel_id = latest_meta.get('report_to_channel_id') or None
            original_depth = latest_meta.get('agent_message_depth', 0)

    if not report_to_id:
        _logger.warning(
            "Auto-forward skip: no report_to_id found for sender '%s' in session '%s' "
            "(searched %d messages + first-message fallback).",
            sender_id, session_id, len(messages),
        )
        return

    # Guard: if report_to_id would create a self-session (agent_b == sender extracted from
    # report_to_id), bail out. This catches any residual cases where report_to_id was set
    # to an inter-agent external_user_id that references the same agent as agent_b_id.
    if report_to_id == f"{_AGENT_MSG_PREFIX}{agent_b_id}":
        _logger.warning(
            "Auto-forward skip: report_to_id '%s' would create a self-session for '%s'. "
            "Possible cause: stale inter-agent report_to_id in message metadata.",
            report_to_id, agent_b_id,
        )
        return

    _logger.info(
        "Auto-forward: report_to_id='%s', report_to_channel_id='%s'.",
        report_to_id, report_to_channel_id or 'none',
    )

    # Forward B's reply to A's user session
    agent_b = db.get_agent(agent_b_id)
    agent_b_name = agent_b.get('name', agent_b_id) if agent_b else agent_b_id

    try:
        from backend.agent_runtime.notifier import notify_agent
        result = notify_agent(
            agent_id=sender_id,
            tag=f'AGENT/{agent_b_name}',
            message=answer,
            external_user_id=report_to_id,
            channel_id=report_to_channel_id,
            dedup=False,
            trigger_llm=True,
            metadata={
                'agent_message': True,
                'from_agent_id': agent_b_id,
                'from_agent_name': agent_b_name,
                'agent_reply': True,
                'report_to_id': report_to_id,
                'agent_message_depth': original_depth,
            },
        )
        if result.get('success'):
            _logger.info(
                "Auto-forward: '%s' reply forwarded to '%s' session '%s' (channel=%s).",
                agent_b_id, sender_id, result.get('session_id'), report_to_channel_id or 'none',
            )
        else:
            _logger.warning(
                "Auto-forward: notify_agent returned failure for '%s' → '%s': reason=%s, "
                "report_to=%s, channel=%s.",
                agent_b_id, sender_id, result.get('reason'), report_to_id,
                report_to_channel_id or 'none',
            )
    except Exception as e:
        _logger.error(
            "Auto-forward failed for '%s' → '%s': %s", agent_b_id, sender_id, e,
        )


# NOTE: _on_final_answer listener is registered in
# backend/agent_runtime/__init__.py at startup, not here,
# so it fires regardless of whether agent_messaging tools are loaded.


# ==================== Registry-style access ====================

_EXECUTORS: Dict[str, Callable] = {
    'send_agent_message': _exec_send_agent_message,
    'escalate_to_user': _exec_escalate_to_user,
    'resolve_agent_approval': _exec_resolve_agent_approval,
}


def get_agent_messaging_tool_defs() -> List[Dict[str, Any]]:
    """Return OpenAI-format tool definitions for agent messaging tools."""
    return list(_TOOL_DEFS)


def get_agent_messaging_executor(agent_context: dict) -> Callable:
    """Return an executor callable for agent messaging tools."""
    def executor(fn_name: str, args: dict):
        if fn_name in _EXECUTORS:
            try:
                return _EXECUTORS[fn_name](args, agent_context)
            except Exception as e:
                return {'error': f"Agent messaging tool error: {str(e)}"}
        return None  # not an agent messaging tool — fall through
    return executor
