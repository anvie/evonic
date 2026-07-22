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

import json
import time
import uuid
import atexit
import threading
from queue import Queue, Empty
from collections import defaultdict
from typing import Any, Callable, Dict, List

from backend.agent_state import AgentState
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

# Wait registry: maps reply_to_id → Queue for blocking wait_for_reply
# Access protected by _WAIT_REGISTRY_LOCK for thread safety.
_WAIT_REGISTRY: Dict[str, Queue] = {}
_WAIT_REGISTRY_LOCK = threading.Lock()

# Wait timeout constants (seconds)
_WAIT_TIMEOUT_DEFAULT = 300
_WAIT_TIMEOUT_MIN = 10
_WAIT_TIMEOUT_MAX = 600


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
                    },
                    "injected_system_vars": {
                        "type": "object",
                        "description": "Optional flat key\u2192value pairs. Keys matching {{key}} placeholders in the target agent's SYSTEM.md will be replaced with the corresponding value for this session turn only. Keys must match [a-zA-Z_][a-zA-Z0-9_]*. Max 10 keys per call, max 1024 chars per value. Reserved keys (time, date, day) are rejected."
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
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": (
                "List external channel sessions for this agent. "
                "Use this to discover valid session IDs for send_channel_message. "
                "Returns sessions sorted by most recent activity first. "
                "Excludes inter-agent sessions and web-only sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of sessions to return (default 20, max 50)."
                    },
                    "channel_type": {
                        "type": "string",
                        "description": "Filter by channel type (e.g. 'whatsapp', 'telegram', 'discord'). Omit for all."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_channel_message",
            "description": (
                "Send a text message to a specific external channel session "
                "(WhatsApp, Telegram, Discord). Use list_sessions first to find "
                "valid session IDs. Supports session targeting (by session_id) "
                "or channel targeting (by channel_id + user_id). "
                "All sends are logged. Rate limited to 20 messages/min globally "
                "with 2-second debounce between sends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Target session_id (e.g. 'sess_abc123') OR "
                            "channel_id (e.g. 'ch_telegram_1'). "
                            "If channel_id, user_id is required."
                        )
                    },
                    "user_id": {
                        "type": "string",
                        "description": (
                            "External user ID (required when target is a channel_id). "
                            "Example: '628123456789@s.whatsapp.net' for WhatsApp, "
                            "'123456789' for Telegram."
                        )
                    },
                    "message": {
                        "type": "string",
                        "description": "The message content to send."
                    }
                },
                "required": ["target", "message"]
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

    # ---- injected_system_vars validation ----
    _INJECTED_VARS_RESERVED = frozenset({'time', 'date', 'day'})
    _INJECTED_VARS_KEY_RE = _re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
    injected_system_vars = args.get('injected_system_vars')
    sanitized_vars = None
    if injected_system_vars is not None:
        if not isinstance(injected_system_vars, dict):
            return {'error': 'injected_system_vars must be a flat key->value object.'}
        if len(injected_system_vars) > 10:
            return {'error': 'injected_system_vars: maximum 10 keys allowed.'}
        sanitized = {}
        seen = set()
        for raw_key, raw_value in injected_system_vars.items():
            key = str(raw_key).strip()
            value = str(raw_value).strip()
            if not _INJECTED_VARS_KEY_RE.match(key):
                return {'error': f'injected_system_vars: invalid key "{key}". Keys must match [a-zA-Z_][a-zA-Z0-9_]*.'}
            if key.lower() in _INJECTED_VARS_RESERVED:
                return {'error': f'injected_system_vars: key "{key}" is reserved and cannot be overridden.'}
            key_lower = key.lower()
            if key_lower in seen:
                return {'error': f'injected_system_vars: duplicate key "{key}" (case-insensitive).'}
            seen.add(key_lower)
            if len(value) > 1024:
                return {'error': f'injected_system_vars: value for key "{key}" exceeds 1024 characters.'}
            sanitized[key] = value
        if sanitized:
            sanitized_vars = sanitized

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

    # Messaging ACL guard — super agents bypass entirely
    if not agent_context.get('is_super'):
        acl_raw = agent_context.get('messaging_acl')
        if acl_raw:
            try:
                acl_list = json.loads(acl_raw) if isinstance(acl_raw, str) else acl_raw
            except (json.JSONDecodeError, TypeError):
                acl_list = []
            if isinstance(acl_list, list) and acl_list:
                acl_mode = agent_context.get('messaging_acl_mode', 'whitelist')
                if acl_mode == 'whitelist' and target_id not in acl_list:
                    _logger.warning(
                        "ACL blocked: agent '%s' not allowed to message '%s' (not in whitelist).",
                        sender_id, target_id,
                    )
                    return {
                        'error': (
                            f"Agent '{sender_id}' is not allowed to message '{target_id}' "
                            f"(not in messaging whitelist)."
                        )
                    }
                elif acl_mode == 'blacklist' and target_id in acl_list:
                    _logger.warning(
                        "ACL blocked: agent '%s' not allowed to message '%s' (in blacklist).",
                        sender_id, target_id,
                    )
                    return {
                        'error': (
                            f"Agent '{sender_id}' is not allowed to message '{target_id}' "
                            f"(in messaging blacklist)."
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

    # Focus mode guard — reject messages to agents that are in focus mode
    # (e.g., working on a kanban task and blocking interruptions from other sessions).
    if target_agent.get('enable_agent_state'):
        try:
            agent_state_json = db.get_agent_state(agent_id=target_id)
            if agent_state_json:
                agent_state = AgentState.deserialize(agent_state_json)
                if agent_state.focus:
                    reason = agent_state.focus_reason or "no reason specified"
                    _logger.info(
                        "Agent '%s' tried to message focused agent '%s' (reason: %s) — blocked.",
                        sender_id, target_id, reason,
                    )
                    return {
                        'error': (
                            f"Cannot send message to agent '{target_id}': "
                            f"agent is currently focused.\n"
                            f"Focus reason: {reason}"
                        )
                    }
        except Exception as e:
            _logger.warning(
                "Failed to check focus state for agent '%s': %s — allowing message through.",
                target_id, e,
            )
            # If we can't read the focus state, err on the side of allowing the message.

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
    report_to_id, report_to_channel_id, session_id = resolve_report_to_from_context(
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
        'injected_system_vars': sanitized_vars,
        'from_agent_name': sender_name,
        'agent_message_depth': current_depth + 1,
        'reply_to_id': reply_to_id,
        'report_to_id': report_to_id,
        'report_to_channel_id': report_to_channel_id,
    }
    if session_id:
        metadata['session_id'] = session_id

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

    if not result.get('success'):
        reason = result.get('reason', 'unknown')
        _logger.error(
            "Agent message FAILED: '%s' → '%s', notify_agent reason=%s, result=%s",
            sender_id, target_id, reason, result,
        )
        return {
            'success': False,
            'error': f"Message delivery failed: {reason}.",
            'detail': result,
        }

    # ---- wait_for_reply (internal-only, NOT in tool definition) ----
    wait_for_reply = args.get('wait_for_reply', False)
    if wait_for_reply:
        wait_timeout = int(args.get('wait_timeout', _WAIT_TIMEOUT_DEFAULT))
        wait_timeout = max(_WAIT_TIMEOUT_MIN, min(wait_timeout, _WAIT_TIMEOUT_MAX))
        reply_queue: Queue = Queue()

        with _WAIT_REGISTRY_LOCK:
            _WAIT_REGISTRY[reply_to_id] = reply_queue

        _logger.info(
            "Agent '%s' waiting for reply from '%s' (reply_to=%s, timeout=%ds).",
            sender_id, target_id, reply_to_id, wait_timeout,
        )

        wait_start = time.time()
        try:
            answer = reply_queue.get(timeout=wait_timeout)
            elapsed = time.time() - wait_start
            _logger.info(
                "Agent '%s' received reply from '%s' after %.1fs (reply_to=%s).",
                sender_id, target_id, elapsed, reply_to_id,
            )
            return {
                'success': True,
                'wait_for_reply': True,
                'reply': answer,
                'reply_from': target_agent.get('name', target_id),
                'reply_agent_id': target_id,
                'waited_seconds': round(elapsed, 1),
                'tip': (
                    f"Reply received from {target_agent.get('name', target_id)} "
                    f"({elapsed:.1f}s). Use result['reply'] to access it."
                ),
            }
        except Empty:
            _logger.info(
                "Agent '%s' wait_for_reply timed out after %ds for '%s' (reply_to=%s).",
                sender_id, wait_timeout, target_id, reply_to_id,
            )
            with _WAIT_REGISTRY_LOCK:
                _WAIT_REGISTRY.pop(reply_to_id, None)
            return {
                'success': True,
                'wait_for_reply': True,
                'timed_out': True,
                'waited_seconds': wait_timeout,
                'tip': (
                    f"Wait timed out after {wait_timeout}s. "
                    f"The reply from {target_agent.get('name', target_id)} will arrive "
                    f"in your next turn automatically."
                ),
            }
        except Exception as e:
            _logger.error(
                "Agent '%s' wait_for_reply error for '%s': %s",
                sender_id, target_id, e,
            )
            with _WAIT_REGISTRY_LOCK:
                _WAIT_REGISTRY.pop(reply_to_id, None)
            return {
                'success': True,
                'wait_for_reply': True,
                'error': f"Internal wait error: {e}",
                'tip': (
                    "The message was sent successfully but waiting failed. "
                    "The reply will arrive in your next turn."
                ),
            }

    return {
        'success': True,
        'message': f"Message sent to {target_agent.get('name', target_id)}.",
        'reply_to_id': reply_to_id,
        'tip': (
            f"Reply from {target_agent.get('name', target_id)} will be automatically forwarded to your session. "
            f"To continue the conversation, call send_agent_message again."
        )
    }


def _exec_escalate_to_user(args: dict, agent_context: dict) -> dict:
    agent_id = agent_context.get('id', '')
    current_user_id = agent_context.get('user_id', '')
    requesting_session_id = agent_context.get('session_id', '')
    message = args.get('message', '').strip()

    if not message:
        return {'error': 'message is required.'}
    if not current_user_id.startswith('__agent__'):
        _logger.debug("Agent '%s' already in user session — escalate skipped.", agent_id)
        return {'error': 'Already in a user session — use send_agent_message or reply directly.'}

    sender_id = current_user_id[len(_AGENT_MSG_PREFIX):]
    # Runtime copies the exact originating route from the request that started
    # this turn. Fall back to durable message metadata for older/in-flight turns.
    request_meta = {
        'session_id': agent_context.get('origin_session_id'),
        'report_to_id': agent_context.get('origin_report_to_id'),
        'report_to_channel_id': agent_context.get('origin_report_to_channel_id'),
        'reply_to_id': agent_context.get('origin_reply_to_id'),
        'agent_message_depth': agent_context.get('agent_message_depth', 0),
    }
    if not request_meta.get('report_to_id'):
        request_meta = db.get_latest_agent_request_metadata(
            requesting_session_id,
            agent_id=agent_context.get('_db_agent_id') or agent_id,
            sender_agent_id=sender_id,
        ) or {}
    origin_session_id = request_meta.get('session_id')
    origin_user_id = request_meta.get('report_to_id')
    origin_channel_id = request_meta.get('report_to_channel_id') or None
    if not origin_user_id:
        _logger.warning(
            "Escalate failed: originating human route metadata is missing for "
            "agent '%s' session '%s'.", agent_id, requesting_session_id,
        )
        return {'error': 'No originating human user session found for this request.'}
    if not origin_session_id:
        origin_session_id = db.get_session_id(
            sender_id, origin_user_id, origin_channel_id,
        )
    if not origin_session_id:
        return {'error': 'No originating human user session found for this request.'}

    origin_session = db.get_session_with_details(origin_session_id)
    if (not origin_session
            or origin_session.get('agent_id') != sender_id
            or origin_session.get('external_user_id') != origin_user_id):
        return {'error': 'The originating human session is unavailable or no longer valid.'}

    escalation_id = str(uuid.uuid4())
    delivery_meta = {
        'escalated_from_agent_session': True,
        'escalation_id': escalation_id,
        'requesting_agent_id': agent_id,
        'requesting_session_id': requesting_session_id,
    }
    try:
        db.create_user_escalation(
            escalation_id=escalation_id,
            requesting_agent_id=agent_id,
            requesting_session_id=requesting_session_id,
            originating_agent_id=sender_id,
            originating_session_id=origin_session_id,
            delivery_session_id=origin_session_id,
            external_user_id=origin_user_id,
            channel_id=origin_channel_id,
            metadata={
                'agent_message_depth': request_meta.get('agent_message_depth', 0),
                'reply_to_id': request_meta.get('reply_to_id'),
            },
        )
    except Exception:
        _logger.exception("Failed to persist escalation correlation '%s'.", escalation_id)
        return {'success': False, 'error': 'Reply routing could not be registered.'}

    from backend.agent_runtime.notifier import notify_agent
    result = notify_agent(
        agent_id=sender_id,
        tag=f'ESCALATION/{agent_context.get("name", agent_id)}',
        message=message,
        session_id=origin_session_id,
        dedup=False,
        trigger_llm=False,
        deliver_external=True,
        metadata=delivery_meta,
    )
    if not result.get('success'):
        db.cancel_user_escalation(escalation_id)
        reason = result.get('reason', 'unknown')
        return {
            'success': False,
            'error': f'Escalation delivery failed: {reason}.',
            'detail': result,
        }
    if not db.mark_user_escalation_delivered(escalation_id):
        return {
            'success': False,
            'error': 'The request was delivered, but reply routing could not be activated.',
            'partial_success': True,
            'detail': result,
        }

    return {
        'success': True,
        'message': 'Escalation delivered to the originating user session.',
        'escalation_id': escalation_id,
        'session_id': result['session_id'],
        'delivery': result.get('delivery'),
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

    # Find the newest routable request metadata from A. This targeted lookup
    # considers user requests only, so tool and assistant traffic cannot hide
    # the originating delegation.
    try:
        meta = db.get_latest_agent_request_metadata(
            session_id, agent_id=_db_agent_id, sender_agent_id=sender_id,
        )
    except Exception as e:
        _logger.warning(
            "Auto-forward: agent-request metadata lookup failed for '%s' (agent_b=%s): %s",
            session_id, agent_b_id, e,
        )
        return

    if not meta or meta.get('from_agent_id') != sender_id or not meta.get('report_to_id'):
        _logger.warning(
            "Auto-forward skip: no routable request metadata found for sender '%s' in session '%s'.",
            sender_id, session_id,
        )
        return

    report_to_id = meta['report_to_id']
    report_to_channel_id = meta.get('report_to_channel_id') or None
    session_id_from_meta = meta.get('session_id')
    original_depth = meta.get('agent_message_depth', 0)
    subagent_user_direct = meta.get('subagent_user_direct', False)
    reply_to_id = meta.get('reply_to_id')
    skip_auto_forward = meta.get('skip_auto_forward', False)

    if skip_auto_forward:
        _logger.info(
            "Auto-forward skip: skip_auto_forward set for '%s' in session '%s' "
            "(sync explore — result already returned via tool output).",
            agent_b_id, session_id,
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

    # Prepend direct-sub-agent marker if this sub-agent was spawned via /sub command
    if subagent_user_direct:
        answer = "[Sub-agent response \u2014 spawned directly by user via /sub command]\n\n" + answer

    # Append a hint so Agent A knows it can continue the conversation if needed
    forwarded_message = (
        answer
        + f"\n\n[If you need to continue this conversation with {agent_b_name}, "
        f"call send_agent_message(target_agent_id=\"{agent_b_id}\", message=...). "
        f"Only do this if you need clarification or additional work from this agent.]"
    )

    try:
        from backend.agent_runtime.notifier import notify_agent
        notify_kwargs = dict(
            agent_id=sender_id,
            tag=f'AGENT/{agent_b_name}',
            message=forwarded_message,
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
                'reply_to_agent_id': agent_b_id,
                'reply_to_session_id': session_id,
            },
        )
        # Pass session_id from originating message metadata so the reply
        # is delivered to the exact session, not re-routed via get_or_create_session.
        # Skip for inter-agent chains (session_id from an __agent__ session would be wrong).
        if session_id_from_meta and not report_to_id.startswith(_AGENT_MSG_PREFIX):
            notify_kwargs['session_id'] = session_id_from_meta
        result = notify_agent(**notify_kwargs)
        if result.get('success'):
            _logger.info(
                "Auto-forward: '%s' reply forwarded to '%s' session '%s' "
                "(requested_channel=%s, route=%s, fallback_reason=%s).",
                agent_b_id, sender_id, result.get('session_id'),
                report_to_channel_id or 'none', result.get('route', 'direct'),
                result.get('fallback_reason') or 'none',
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

    # ---- wake-up: signal any waiting send_agent_message(wait_for_reply=true) ----
    if reply_to_id:
        with _WAIT_REGISTRY_LOCK:
            q = _WAIT_REGISTRY.pop(reply_to_id, None)
        if q is not None:
            try:
                q.put(answer, timeout=1)
                _logger.info(
                    "Wait registry: woke up waiter for reply_to=%s (sender=%s, agent_b=%s).",
                    reply_to_id, sender_id, agent_b_id,
                )
            except Exception:
                _logger.warning(
                    "Wait registry: failed to wake waiter for reply_to=%s (full queue?).",
                    reply_to_id,
                )

# NOTE: _on_final_answer listener is registered in
# backend/agent_runtime/__init__.py at startup, not here,
# so it fires regardless of whether agent_messaging tools are loaded.


# ==================== Channel Send Tools ====================


def _exec_list_sessions(args: dict, agent_context: dict) -> dict:
    """List external channel sessions for the calling agent."""
    agent_id = agent_context.get('id', '')
    limit = min(int(args.get('limit', 20)), 50)
    channel_type = args.get('channel_type')

    # Query session_index for this agent's sessions
    try:
        sessions, total = db.get_all_sessions(limit=limit + 100, offset=0, exclude_test=True)
    except Exception as e:
        _logger.error("list_sessions: failed to query sessions: %s", e)
        return {'error': f'Failed to list sessions: {e}'}

    # Filter to agent's own sessions, exclude inter-agent and web-only
    results = []
    for s in sessions:
        if s.get('agent_id') != agent_id:
            continue
        ext = s.get('external_user_id', '')
        # Skip inter-agent sessions
        if ext.startswith('__agent__'):
            continue
        # Skip web-only sessions (no meaningful external delivery)
        if not s.get('channel_id'):
            continue
        # Filter by channel type if specified
        if channel_type and s.get('channel_type') != channel_type:
            continue

        results.append({
            'session_id': s['id'],
            'channel_type': s.get('channel_type') or '',
            'channel_name': s.get('channel_name') or '',
            'external_user_id': ext,
            'updated_at': s.get('updated_at'),
        })

    # Sort by updated_at descending (most recent first)
    results.sort(key=lambda x: x.get('updated_at') or '', reverse=True)

    # Apply limit
    results = results[:limit]

    return {
        'sessions': results,
        'total': len(results),
    }


def _exec_send_channel_message(args: dict, agent_context: dict) -> dict:
    """Send a text message to an external channel session."""
    sender_id = agent_context.get('id', '')
    target = args.get('target', '').strip()
    message = args.get('message', '').strip()
    user_id = args.get('user_id')

    if not target:
        return {'error': 'target is required.'}
    if not message:
        return {'error': 'message is required.'}

    session_id = None
    channel_id = None
    external_user_id = None
    channel_type = None

    # ---- Resolve target ----
    if not target.startswith('ch_'):
        # Session targeting
        session = db.get_session_with_details(target)
        if not session:
            return {'error': f'Session \'{target}\' not found.'}
        # Ownership check
        if session.get('agent_id') != sender_id:
            _logger.warning(
                "send_channel_message: agent '%s' tried to send to "
                "session '%s' owned by agent '%s' — blocked.",
                sender_id, target, session.get('agent_id'),
            )
            return {'error': 'You can only send to your own sessions.'}
        # Channel check
        if not session.get('channel_id'):
            return {'error': 'Session has no associated channel (web-only sessions cannot receive external messages).'}
        if session.get('channel_type') == 'web':
            return {'error': 'Cannot send to web-only sessions.'}

        session_id = target
        channel_id = session['channel_id']
        external_user_id = session['external_user_id']
        channel_type = session.get('channel_type')
    else:
        # Channel targeting
        if not user_id:
            return {'error': 'user_id is required when targeting by channel_id.'}
        external_user_id = user_id.strip()

        channel = db.get_channel(target)
        if not channel:
            return {'error': f'Channel \'{target}\' not found.'}
        # Ownership check: dedicated channels require matching agent_id;
        # shared channels (agent_id=None) allow any agent listed in routes.
        if channel.get('agent_id') is not None:
            if channel.get('agent_id') != sender_id:
                _logger.warning(
                    "send_channel_message: agent '%s' tried to use "
                    "channel '%s' owned by agent '%s' — blocked.",
                    sender_id, target, channel.get('agent_id'),
                )
                return {'error': 'You can only send via your own channels.'}
        else:
            # Shared channel — verify agent is in the route map
            routes = (channel.get('config') or {}).get('routes') or {}
            if sender_id not in routes.values():
                _logger.warning(
                    "send_channel_message: agent '%s' not in shared channel '%s' routes — blocked.",
                    sender_id, target,
                )
                return {'error': 'You are not authorized to send via this shared channel.'}
        if channel.get('channel_type') == 'web':
            return {'error': 'Cannot send via web channels.'}

        channel_id = target
        channel_type = channel.get('type')

        # Get or create session
        try:
            session_id = db.get_or_create_session(
                agent_id=sender_id,
                external_user_id=external_user_id,
                channel_id=channel_id,
            )
        except Exception as e:
            _logger.error("send_channel_message: failed to get/create session: %s", e)
            return {'error': f'Failed to resolve session: {e}'}

    # ---- Safety checks ----
    # Channel running check
    from backend.channels.registry import channel_manager
    instance = channel_manager.get_channel_instance(channel_id)
    if not instance or not instance.is_running:
        _logger.warning(
            "send_channel_message: channel '%s' is not running for agent '%s'.",
            channel_id, sender_id,
        )
        return {
            'error': (
                f'Channel \'{channel_id}\' is not currently running. '
                'Cannot send messages to inactive channels.'
            ),
        }

    # ---- Rate limit + debounce ----
    from backend.tools.channel_send_guard import wait_for_send_slot
    try:
        wait_for_send_slot(sender_id)
    except Exception as e:
        _logger.error("send_channel_message: rate guard error: %s", e)
        return {'error': f'Rate guard error: {e}'}

    # ---- Send via channel ----
    try:
        instance.send_message(external_user_id, message)
    except Exception as e:
        _logger.error(
            "send_channel_message: channel send failed for agent '%s' "
            "session '%s' channel '%s': %s",
            sender_id, session_id, channel_id, e,
        )
        return {
            'error': f'Channel send failed: {e}',
            'session_id': session_id,
            'channel_type': channel_type,
        }

    # ---- Record in chat log ----
    try:
        db.add_chat_message(
            session_id, 'assistant', message,
            agent_id=sender_id,
            metadata={'channel_send': True},
        )
        from models.chatlog import chatlog_manager
        chatlog_manager.get(sender_id, session_id).append({
            'type': 'final',
            'session_id': session_id,
            'content': message,
            'metadata': {'channel_send': True},
        })
    except Exception as e:
        _logger.warning("send_channel_message: chat log error: %s", e)

    # ---- Log success ----
    _logger.info(
        "send_channel_message: agent '%s' sent to session '%s' "
        "(channel: %s, type: %s, user: %s)",
        sender_id, session_id, channel_id, channel_type, external_user_id,
    )

    return {
        'success': True,
        'message': 'Message sent successfully.',
        'session_id': session_id,
        'channel_type': channel_type,
        'external_user_id': external_user_id,
    }


# ==================== Registry-style access ====================

_EXECUTORS: Dict[str, Callable] = {
    'send_agent_message': _exec_send_agent_message,
    'escalate_to_user': _exec_escalate_to_user,
    'resolve_agent_approval': _exec_resolve_agent_approval,
    'list_sessions': _exec_list_sessions,
    'send_channel_message': _exec_send_channel_message,
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


# ==================== Periodic bucket cleanup ====================

def _cleanup_buckets() -> None:
    """Remove defaultdict keys whose timestamp lists have been fully pruned.

    The rate-limit check functions prune expired timestamps inline on every
    call, but defaultdict keys accumulate over time if a sender/target pair
    becomes inactive. This periodic sweep removes empty-list entries to
    prevent unbounded dict growth.

    Non-blocking: iterates the dict under no lock (stale reads are harmless —
    the check functions will re-create keys as needed on next message).
    """
    for key in list(_rate_limit_buckets.keys()):
        if not _rate_limit_buckets[key]:
            _rate_limit_buckets.pop(key, None)
    for key in list(_global_rate_limit_buckets.keys()):
        if not _global_rate_limit_buckets[key]:
            _global_rate_limit_buckets.pop(key, None)
    for key in list(_fanout_buckets.keys()):
        if not _fanout_buckets[key]:
            _fanout_buckets.pop(key, None)


def _start_bucket_cleanup(interval: int = 300):
    """Launch a daemon thread that periodically cleans stale bucket keys."""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                _cleanup_buckets()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True, name='rate-limit-cleanup').start()


atexit.register(_cleanup_buckets)
_start_bucket_cleanup()
