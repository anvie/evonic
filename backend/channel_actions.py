"""
Channel Actions — interactive choices attached to outbound channel messages.

A message can carry a short list of actions, each ``{label, namespace, payload}``.
Channels with native buttons (Telegram inline keyboards) render them as buttons.
Every other channel falls back to numbered text options, and a reply consisting
of just that number selects the action. Either way the choice is dispatched to
the handler registered for the action's namespace.

Handlers are plain callables:
    handler(payload: str, context: dict) -> Optional[str]

``context`` carries channel_type, channel_id, agent_id, external_user_id,
actor_id (the user who tapped or replied) and source ('button' | 'text').
Return a short acknowledgement to show the user, or None to show nothing.
Handlers must verify ``actor_id`` themselves when the action is privileged.
"""

import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

CALLBACK_PREFIX = 'act:'
# Telegram limits callback_data to 64 bytes; enforcing it everywhere keeps
# actions portable across channels.
MAX_CALLBACK_BYTES = 64
MAX_ACTIONS = 8
MAX_LABEL_CHARS = 40
TEXT_OPTIONS_TTL_SECONDS = 6 * 3600

# Prefixes the Telegram channel already uses for its own callbacks.
RESERVED_NAMESPACES = frozenset({'approve', 'reject', 'aq', 'act'})

_NAMESPACE_RE = re.compile(r'^[a-z][a-z0-9_.-]{0,23}$')
_NUMBER_REPLY_RE = re.compile(r'^\s*(\d{1,2})\s*\.?\s*$')

UNAVAILABLE_TEXT = 'This action is no longer available.'
FAILED_TEXT = 'Sorry, that action could not be completed.'

_handlers: Dict[str, Callable] = {}
_handlers_lock = threading.Lock()

# (channel_id, external_user_id) -> (expires_at, [action, ...])
_text_options: Dict[Tuple[str, str], Tuple[float, List[dict]]] = {}
_text_options_lock = threading.Lock()


class ChannelActionError(ValueError):
    """Raised when an action list is malformed."""


# ═══════════════════════════════════════════════════════════════════
# Handler registry
# ═══════════════════════════════════════════════════════════════════

def register_channel_action_handler(namespace: str, fn: Callable) -> None:
    """Register the handler for one action namespace (one handler per namespace)."""
    _validate_namespace(namespace)
    with _handlers_lock:
        existing = _handlers.get(namespace)
        if existing is not None and existing is not fn:
            _logger.warning("Channel action namespace '%s' re-registered", namespace)
        _handlers[namespace] = fn


def unregister_channel_action_handler(namespace: str, fn: Optional[Callable] = None) -> None:
    """Remove a namespace's handler. With ``fn``, only if it is the registered one."""
    with _handlers_lock:
        if fn is None or _handlers.get(namespace) is fn:
            _handlers.pop(namespace, None)


def dispatch_channel_action(namespace: str, payload: str, context: dict) -> Optional[str]:
    """Run the handler for ``namespace``; return its acknowledgement text."""
    with _handlers_lock:
        handler = _handlers.get(namespace)
    if handler is None:
        return UNAVAILABLE_TEXT
    try:
        result = handler(payload, dict(context))
    except Exception:
        _logger.exception("Channel action handler failed for namespace '%s'", namespace)
        return FAILED_TEXT
    return str(result) if result else None


# ═══════════════════════════════════════════════════════════════════
# Validation and encoding
# ═══════════════════════════════════════════════════════════════════

def _validate_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or not _NAMESPACE_RE.match(namespace):
        raise ChannelActionError(f"Invalid action namespace: {namespace!r}")
    if namespace in RESERVED_NAMESPACES:
        raise ChannelActionError(f"Action namespace '{namespace}' is reserved")


def encode_action(action: dict) -> str:
    """Encode an action as callback data: ``act:<namespace>:<payload>``."""
    return f"{CALLBACK_PREFIX}{action['namespace']}:{action['payload']}"


def decode_action(data: str) -> Optional[Tuple[str, str]]:
    """Decode callback data into (namespace, payload), or None if not an action."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    namespace, sep, payload = data[len(CALLBACK_PREFIX):].partition(':')
    if not sep or not _NAMESPACE_RE.match(namespace):
        return None
    return namespace, payload


def normalize_actions(actions) -> List[dict]:
    """Validate an action list and return clean copies. Raises ChannelActionError."""
    if not actions:
        return []
    if not isinstance(actions, (list, tuple)):
        raise ChannelActionError("actions must be a list")
    if len(actions) > MAX_ACTIONS:
        raise ChannelActionError(f"At most {MAX_ACTIONS} actions per message")
    clean = []
    for raw in actions:
        if not isinstance(raw, dict):
            raise ChannelActionError("Each action must be a dict")
        label = str(raw.get('label') or '').strip()
        namespace = raw.get('namespace')
        payload = str(raw.get('payload') or '')
        if not label:
            raise ChannelActionError("Action label is required")
        if len(label) > MAX_LABEL_CHARS:
            raise ChannelActionError(f"Action label longer than {MAX_LABEL_CHARS} characters")
        _validate_namespace(namespace)
        action = {'label': label, 'namespace': namespace, 'payload': payload}
        if len(encode_action(action).encode('utf-8')) > MAX_CALLBACK_BYTES:
            raise ChannelActionError(
                f"Encoded action exceeds {MAX_CALLBACK_BYTES} bytes: {namespace}:{payload}")
        clean.append(action)
    return clean


# ═══════════════════════════════════════════════════════════════════
# Text fallback (channels without buttons)
# ═══════════════════════════════════════════════════════════════════

def render_text_options(text: str, actions: List[dict]) -> str:
    """Append numbered options to ``text`` for channels without buttons."""
    lines = [f"{i}. {a['label']}" for i, a in enumerate(actions, start=1)]
    return f"{text}\n\nReply with a number:\n" + '\n'.join(lines)


def remember_text_options(channel_id: str, external_user_id: str, actions: List[dict],
                          ttl_seconds: int = TEXT_OPTIONS_TTL_SECONDS) -> None:
    """Remember the latest numbered options sent to a user (replaces older ones)."""
    with _text_options_lock:
        _purge_expired_locked()
        _text_options[(channel_id, str(external_user_id))] = (time.time() + ttl_seconds, list(actions))


def take_text_option(channel_id: str, external_user_id: str, message: str) -> Optional[dict]:
    """If ``message`` selects a pending numbered option, consume and return it."""
    if not channel_id or not message:
        return None
    match = _NUMBER_REPLY_RE.match(message)
    if not match:
        return None
    key = (channel_id, str(external_user_id))
    with _text_options_lock:
        entry = _text_options.get(key)
        if entry is None:
            return None
        expires_at, actions = entry
        if expires_at < time.time():
            _text_options.pop(key, None)
            return None
        index = int(match.group(1)) - 1
        if not 0 <= index < len(actions):
            return None
        _text_options.pop(key, None)
        return actions[index]


def handle_text_reply(channel_id: str, external_user_id: str, message: str,
                      agent_id: str = None, channel_type: str = None) -> Tuple[bool, Optional[str]]:
    """Dispatch a numbered reply. Returns (matched, acknowledgement)."""
    action = take_text_option(channel_id, external_user_id, message)
    if action is None:
        return False, None
    if channel_type is None:
        channel_type = _active_channel_type(channel_id)
    context = {
        'channel_type': channel_type,
        'channel_id': channel_id,
        'agent_id': agent_id,
        'external_user_id': str(external_user_id),
        'actor_id': str(external_user_id),
        'source': 'text',
    }
    return True, dispatch_channel_action(action['namespace'], action['payload'], context)


def _active_channel_type(channel_id: str) -> Optional[str]:
    try:
        from backend.channels.registry import channel_manager
        instance = channel_manager._active.get(channel_id)
        return instance.get_channel_type() if instance else None
    except Exception:
        return None


def _purge_expired_locked() -> None:
    now = time.time()
    for key in [k for k, (exp, _) in _text_options.items() if exp < now]:
        del _text_options[key]


def _reset_for_tests() -> None:
    with _handlers_lock:
        _handlers.clear()
    with _text_options_lock:
        _text_options.clear()
