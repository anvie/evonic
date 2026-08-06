"""
Unified Real-Time SSE Endpoint — consolidates 5 separate SSE connections
into 1 multiplexed connection with per-channel priority queuing.

Endpoint: GET /api/realtime/stream

Query parameters (opt-in channels):
  channels      — comma-separated: status,approvals,update
  chat          — 1 to include per-session chat events
  session_id    — chat session ID (required when chat=1)
  agent_id      — agent ID (required when chat=1)
  after         — chat event resume seq
  workplace     — workplace ID for connector events
  chat_throttle — throttle interval ms for chat events (default 100)
"""

import collections
import json
import logging
import math
import os
import queue
import random
import signal
import socket
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, request, stream_with_context

log = logging.getLogger(__name__)

realtime_bp = Blueprint('realtime', __name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RING_SIZES = {
    'chat': 256,
    'approvals': 8,
    'status': 32,
    'update': 16,
    'workplace': 16,
}

RING_STRATEGIES = {
    'chat': 'drop_oldest',
    'approvals': 'drop_oldest',   # on overflow keep the newest approval, never drop it
    'status': 'drop_oldest',
    'update': 'drop_oldest',
    'workplace': 'drop_oldest',
}

CHANNEL_PRIORITY = {
    'update': 0,     # highest — small, rare, must be fast
    'status': 0,
    'approvals': 1,   # user-facing modal
    'chat': 2,       # high throughput, tolerable delay
    'workplace': 2,  # high throughput, tolerable delay
}

# Weighted round-robin: 1 L0/L1 event per 5 L2 events
L2_WEIGHT = 5

HEARTBEAT_INTERVAL = 15       # seconds
HEARTBEAT_MAX_FAILURES = 3
TCP_KEEPIDLE = 60
TCP_KEEPINTVL = 10
TCP_KEEPCNT = 3

CIRCUIT_BREAKER_WINDOW = 10   # seconds
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 60

# Per-channel buffer for pause/resume
PAUSE_BUFFER = {
    'chat': 64,
    'workplace': 16,
}

# ---------------------------------------------------------------------------
# Bounded Ring Buffer
# ---------------------------------------------------------------------------

class BoundedRing:
    """Thread-safe bounded queue with configurable overflow strategy."""

    def __init__(self, channel: str, maxlen: int, strategy: str):
        self.channel = channel
        self.maxlen = maxlen
        self.strategy = strategy
        self._lock = threading.Lock()
        self._q = collections.deque(maxlen=maxlen)
        self._dropped_count = 0
        self._seq = 0

    def put(self, item):
        """Put an item. Returns (inserted, dropped_count_for_this_put)."""
        dropped = 0
        with self._lock:
            if len(self._q) >= self.maxlen:
                if self.strategy == 'drop_oldest':
                    self._q.popleft()
                    dropped = 1
                elif self.strategy == 'drop_newest':
                    dropped = 1
                    # don't actually enqueue — drop the new item
                    self._dropped_count += 1
                    return (False, 1)
            self._seq += 1
            self._q.append((self._seq, item))
            if dropped:
                self._dropped_count += 1
        return (True, dropped)

    def get(self):
        """Get the oldest item, or None if empty."""
        with self._lock:
            if self._q:
                return self._q.popleft()
            return None

    def get_many(self, max_count: int):
        """Get up to max_count items."""
        items = []
        with self._lock:
            while self._q and len(items) < max_count:
                items.append(self._q.popleft())
        return items

    def get_all(self):
        """Get all items."""
        with self._lock:
            items = list(self._q)
            self._q.clear()
            return items

    def drain_dropped(self) -> int:
        """Atomically read and reset dropped count."""
        with self._lock:
            c = self._dropped_count
            self._dropped_count = 0
            return c

    def peek_all(self):
        """Return all items without dequeuing (for snapshot)."""
        with self._lock:
            return list(self._q)

    def size(self):
        with self._lock:
            return len(self._q)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Per-channel circuit breaker with sliding window crash tracking."""

    def __init__(self, channel: str):
        self.channel = channel
        self._lock = threading.Lock()
        self._crashes = []  # list of crash timestamps
        self._open_since = None
        self._disabled = False

    def record_crash(self) -> bool:
        """Record a crash. Returns True if circuit should open (stop restarting)."""
        now = time.time()
        with self._lock:
            # Clean old entries outside window
            cutoff = now - CIRCUIT_BREAKER_WINDOW
            self._crashes = [t for t in self._crashes if t > cutoff]
            self._crashes.append(now)
            if len(self._crashes) >= CIRCUIT_BREAKER_THRESHOLD:
                self._open_since = now
                self._disabled = True
                return True
        return False

    def is_disabled(self) -> bool:
        with self._lock:
            if not self._disabled:
                return False
            # Check cooldown
            if self._open_since and (time.time() - self._open_since) > CIRCUIT_BREAKER_COOLDOWN:
                self._disabled = False
                self._crashes = []
                self._open_since = None
                return False
            return True

    def reset(self):
        with self._lock:
            self._crashes = []
            self._open_since = None
            self._disabled = False


# ---------------------------------------------------------------------------
# Connection State
# ---------------------------------------------------------------------------

# Global registry of active connections (for pause/resume by session)
_connections: dict = {}  # key: connection_id -> RealtimeConnection
_conn_lock = threading.Lock()

# Heartbeat-aware web SSE delivery check: maximum age (seconds) of the
# last heartbeat before a connection is considered disconnected.
WEB_SSE_HEARTBEAT_MAX_AGE = 45  # 3 × HEARTBEAT_INTERVAL


def has_active_web_sse(session_id: str) -> bool:
    """Return True if any SSE connection for *session_id* has had a
    heartbeat within WEB_SSE_HEARTBEAT_MAX_AGE seconds.

    This is more reliable than has_web_listener() which only checks
    listener registration, not actual delivery.
    """
    now = time.monotonic()
    with _conn_lock:
        for conn in _connections.values():
            if conn.chat_session_id == session_id:
                if (conn.last_heartbeat_time > 0 and
                        (now - conn.last_heartbeat_time) < WEB_SSE_HEARTBEAT_MAX_AGE):
                    return True
    return False


class RealtimeConnection:
    """Per-connection state for the unified SSE stream."""

    def __init__(self, conn_id: str, channels: set, chat_session_id: str = None,
                 agent_id: str = None, after_seq: int = 0,
                 workplace_id: str = None, chat_throttle_ms: int = 100,
                 expires_at: float = None):
        self.conn_id = conn_id
        self.channels = channels
        self.chat_session_id = chat_session_id
        self.agent_id = agent_id
        self.after_seq = after_seq
        self.last_global_seq = after_seq
        self.last_chat_seq = after_seq
        self.workplace_id = workplace_id
        self.chat_throttle_ms = chat_throttle_ms
        self.expires_at = expires_at
        self.paused = False
        self._stop_event = threading.Event()
        # Per-channel pause buffers
        self._pause_buffers: dict[str, BoundedRing] = {}
        self.last_write_ok = True
        # Reference to per-channel rings (set by api_realtime_stream)
        # so api_realtime_resume can flush pause buffers back into them.
        self.rings: dict[str, BoundedRing] | None = None
        # Last heartbeat timestamp (monotonic). Updated by the generator.
        # Used by has_active_web_listener() to verify delivery.
        self.last_heartbeat_time: float = 0.0

    def stop(self):
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def check_expired(self) -> bool:
        if self.expires_at and time.time() > self.expires_at:
            return True
        return False


# ---------------------------------------------------------------------------
# SSE formatting helpers
# ---------------------------------------------------------------------------

def _json_default(obj):
    """Custom JSON default handler: convert non-serializable types to strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    log.warning("_format_sse_event: non-serializable type %s in SSE data", type(obj).__name__)
    return str(obj)


def _format_sse_event(event_name: str, data: dict, seq_id: str = None,
                       global_seq: int = None) -> str:
    """Format a single SSE event with optional id and event fields."""
    lines = []
    if seq_id:
        lines.append(f"id: {seq_id}")
    elif global_seq is not None:
        lines.append(f"id: {global_seq}")
    if event_name:
        lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'), default=_json_default)}")
    return "\n".join(lines) + "\n\n"


def _format_sse_comment(comment: str) -> str:
    """Format an SSE comment line."""
    return f": {comment}\n\n"


# ---------------------------------------------------------------------------
# State snapshot (atomic subscribe)
# ---------------------------------------------------------------------------

# Agent busy-status snapshot, cached briefly so the multiple SSE connections
# a page opens at load share one db.get_agents() read. Staleness is bounded:
# changes after the snapshot arrive via agent_busy_changed events anyway.
_status_snapshot_cache = {'ts': 0.0, 'events': None}
_status_snapshot_lock = threading.Lock()
_STATUS_SNAPSHOT_TTL = 2.0


def _get_status_snapshot_events() -> list:
    from models.db import db
    now = time.monotonic()
    with _status_snapshot_lock:
        cached = _status_snapshot_cache['events']
        if cached is not None and now - _status_snapshot_cache['ts'] < _STATUS_SNAPSHOT_TTL:
            return cached
        events = []
        for agent in db.get_agents():
            events.append(('agent_busy_changed', {
                'agent_id': agent['id'],
                'busy': agent.get('busy', False),
                'session_id': agent.get('current_session_id', ''),
            }))
        _status_snapshot_cache['ts'] = now
        _status_snapshot_cache['events'] = events
        return events


def _build_snapshot(channels: set, agent_id: str = None,
                    session_id: str = None,
                    workplace_id: str = None) -> list:
    """Capture current state snapshot for requested channels."""
    events = []

    if 'status' in channels:
        try:
            events.extend(_get_status_snapshot_events())
        except Exception as e:
            log.warning("realtime snapshot: failed to get agent statuses: %s", e)

    if 'approvals' in channels or session_id:
        from models.db import db
        try:
            pending = db.get_pending_tool_approvals()
            for app in (pending or []):
                events.append(('approval_required', {
                    'approval_id': app.get('id', ''),
                    'agent_id': app.get('agent_id', ''),
                    'source_agent_id': app.get('source_agent_id', ''),
                    'source_agent_name': app.get('source_agent_name', ''),
                    'tool': app.get('tool_name', ''),
                    'args': app.get('tool_args', {}),
                    'approval_info': app.get('approval_info', {}),
                    'reasons': app.get('reasons', []),
                    'score': app.get('score'),
                }))
        except Exception as e:
            log.warning("realtime snapshot: failed to get pending approvals: %s", e)

    if 'update' in channels:
        try:
            from routes.update import update_manager
            status = update_manager.get_status()
            events.append(('update_status', status))
        except Exception as e:
            log.warning("realtime snapshot: failed to get update status: %s", e)

    return events


# ---------------------------------------------------------------------------
# Producer factories (per-channel) — Task isolation
# ---------------------------------------------------------------------------

def _producer_status(ring: BoundedRing, breaker: CircuitBreaker,
                     stop_event: threading.Event):
    """Producer: listen to agent_busy_changed, turn_complete, whatsapp_bridge_status and panel_updated events."""
    from backend.event_stream import event_stream

    def busy_handler(data):
        ring.put(('agent_busy_changed', {
            'agent_id': data.get('agent_id', ''),
            'busy': data.get('busy', False),
            'session_id': data.get('session_id', ''),
        }))

    def turn_handler(data):
        response = data.get('response', '')
        if not response or data.get('is_error'):
            return
        ring.put(('agent_turn_complete', {
            'agent_id': data.get('agent_id', ''),
            'agent_name': data.get('agent_name', ''),
            'response': response,
            'session_id': data.get('session_id', ''),
            'external_user_id': data.get('external_user_id', ''),
        }))

    def wa_bridge_handler(data):
        ring.put(('whatsapp_bridge_status', {
            'agent_id': data.get('agent_id', ''),
            'channel_id': data.get('channel_id', ''),
            'status': data.get('status', ''),
        }))

    def panel_handler(data):
        ring.put(('panel_updated', {
            'agent_id': data.get('agent_id', ''),
        }))

    event_stream.on('agent_busy_changed', busy_handler)
    event_stream.on('turn_complete', turn_handler)
    event_stream.on('whatsapp_bridge_status', wa_bridge_handler)
    event_stream.on('panel_updated', panel_handler)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        event_stream.off('agent_busy_changed', busy_handler)
        event_stream.off('turn_complete', turn_handler)
        event_stream.off('whatsapp_bridge_status', wa_bridge_handler)
        event_stream.off('panel_updated', panel_handler)


def _producer_approval(ring: BoundedRing, breaker: CircuitBreaker,
                       stop_event: threading.Event):
    """Producer: listen to approval_required and approval_resolved events."""
    from backend.event_stream import event_stream

    def approval_handler(data):
        ring.put(('approval_required', {
            'approval_id': data.get('approval_id', ''),
            'agent_id': data.get('agent_id', ''),
            'source_agent_id': data.get('source_agent_id', ''),
            'source_agent_name': data.get('source_agent_name', ''),
            'tool': data.get('tool_name', ''),
            'args': data.get('tool_args', {}),
            'approval_info': data.get('approval_info', {}),
            'reasons': data.get('reasons', []),
            'score': data.get('score'),
        }))

    def resolved_handler(data):
        ring.put(('approval_resolved', {
            'approval_id': data.get('approval_id', ''),
            'decision': data.get('decision', ''),
            'timed_out': data.get('timed_out', False),
        }))

    event_stream.on('approval_required', approval_handler)
    event_stream.on('approval_resolved', resolved_handler)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        event_stream.off('approval_required', approval_handler)
        event_stream.off('approval_resolved', resolved_handler)


def _producer_chat(ring: BoundedRing, breaker: CircuitBreaker,
                   stop_event: threading.Event, session_id: str,
                   after_seq: int = 0):
    """Producer: listen to per-session chat events."""
    from backend.event_stream import event_stream

    _TRANSFORMS = {
        'turn_begin': ('turn_begin', lambda d: {'ts': d.get('ts', 0)}),
        'llm_thinking': ('thinking', lambda d: {'content': d.get('thinking', '')}),
        'tool_call_started': ('tool_call_started', lambda d: {
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'param_types': d.get('param_types', {}),
        }),
        'tool_executed': ('tool_executed', lambda d: {
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'result': d.get('tool_result', {}),
            'error': d.get('has_error', False),
        }),
        'state:changed': ('state:changed', lambda d: {
            key: d[key] for key in ('mode', 'plan_file', 'tasks', 'loaded_skills')
            if key in d
        }),
        'tasks:auto_transition': ('tasks:auto_transition', lambda d: {
            key: d[key] for key in ('task_ids', 'tasks') if key in d
        }),
        'tasks:stale': ('tasks:stale', lambda d: {
            key: d[key] for key in ('task_ids', 'tasks') if key in d
        }),
        'llm_response_chunk': ('response_chunk', lambda d: {
            'content': d.get('content', ''),
            'is_final': d.get('is_final', False),
            'send_as_message': d.get('send_as_message', False),
        }),
        'turn_complete': ('done', lambda d: {
            'thinking_duration': d.get('thinking_duration'),
            'response': d.get('response', ''),
            'slash_command': d.get('slash_command', False),
            'attachment_info': d.get('attachment_info'),
        }),
        'approval_required': ('approval_required', lambda d: {
            'approval_id': d.get('approval_id', ''),
            'agent_id': d.get('agent_id', ''),
            'source_agent_id': d.get('source_agent_id', ''),
            'source_agent_name': d.get('source_agent_name', ''),
            'tool': d.get('tool_name', ''),
            'args': d.get('tool_args', {}),
            'approval_info': d.get('approval_info', {}),
            'reasons': d.get('reasons', []),
            'score': d.get('score'),
        }),
        'approval_resolved': ('approval_resolved', lambda d: {
            'approval_id': d.get('approval_id', ''),
            'decision': d.get('decision', ''),
            'timed_out': d.get('timed_out', False),
        }),
        'llm_retry': ('retry', lambda d: {
            'retry_count': d.get('retry_count', 0),
            'max_retries': d.get('max_retries', 0),
            'error_type': d.get('error_type', ''),
            'message': d.get('user_message', ''),
        }),
        'message_injected': ('message_injected', lambda d: {
            'message': d.get('message', ''),
        }),
        'message_injection_applied': ('message_injection_applied', lambda d: {
            'content': d.get('content', ''),
            'count': d.get('count', 1),
        }),
        'message_received': ('message_received', lambda d: {
            'message': d.get('message', ''),
            'metadata': d.get('metadata', {}),
        }),
        'whatsapp_restriction_warning': ('whatsapp_restriction_warning', lambda d: {
            'content': d.get('content', ''),
            'metadata': d.get('metadata', {}),
        }),
        'session_clear': ('session_clear', lambda d: {
            'session_id': d.get('session_id', ''),
            'agent_id': d.get('agent_id', ''),
        }),
        'turn_split': ('turn_split', lambda d: {}),
        'evonic:agent-state-changed': ('state_changed', lambda d: {
            'agent_id': d.get('agent_id', ''),
            'session_id': d.get('session_id', ''),
        }),
    }

    def make_handler(evt_name, sse_name, transform):
        def handler(data):
            if data.get('session_id') != session_id:
                return
            try:
                payload = transform(data) if transform else data
                if payload is not None:
                    # Use the contiguous per-session chat seq (not the global _seq)
                    # so the browser's gap detector sees a gap-free sequence and
                    # doesn't fire a phantom gap-fill on every event. Matches the
                    # legacy /chat/stream + /chat/events gap-fill endpoint.
                    payload['seq'] = data.get('_chat_seq')
                    ring.put((sse_name, payload))
            except Exception:
                pass
        return handler

    handlers = {}
    for evt_name, (sse_name, transform) in _TRANSFORMS.items():
        h = make_handler(evt_name, sse_name, transform)
        handlers[evt_name] = h
        event_stream.on(evt_name, h)

    event_stream.register_web_listener(session_id)

    # Replay the in-progress session buffer so a client connecting at/after the
    # POST that starts a turn still sees the turn's opening events (turn_begin,
    # early thinking, first tool call). The legacy /chat/stream did this; without
    # it the unified path loses those events and the UI shows only a spinner
    # until a manual refresh. Subscribe-then-replay ordering (after event_stream.on
    # above) means live events arriving during replay are also queued; the client
    # dedups the overlap by the contiguous _chat_seq.
    try:
        buffered = event_stream.get_session_events(session_id, after_seq)
        # On a fresh connect, drop everything up to and including the last
        # completed turn / session_clear so we never replay a finished turn.
        if after_seq == 0:
            last_boundary = -1
            for i, e in enumerate(buffered):
                if e['event'] in ('turn_complete', 'session_clear'):
                    last_boundary = i
            if last_boundary >= 0:
                buffered = buffered[last_boundary + 1:]
        for entry in buffered:
            st = _TRANSFORMS.get(entry['event'])
            if not st:
                continue
            sse_name, transform = st
            payload = transform(entry['data'])
            payload['seq'] = entry.get('chat_seq')
            ring.put((sse_name, payload))
    except Exception:
        pass

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        event_stream.unregister_web_listener(session_id)
        for evt_name, h in handlers.items():
            event_stream.off(evt_name, h)


def _producer_update(ring: BoundedRing, breaker: CircuitBreaker,
                     stop_event: threading.Event):
    """Producer: listen to update manager status changes."""
    from routes.update import update_manager

    listener_q = update_manager.register_listener()

    try:
        while not stop_event.is_set():
            try:
                snapshot = listener_q.get(timeout=1)
                ring.put(('update_status', snapshot))
                if snapshot.get('status') in ('success', 'failed'):
                    ring.put(('update_done', {'status': snapshot['status']}))
            except queue.Empty:
                pass
    finally:
        update_manager.unregister_listener(listener_q)


def _producer_workplace(ring: BoundedRing, breaker: CircuitBreaker,
                        stop_event: threading.Event, workplace_id: str):
    """Producer: listen to workplace connector events for a specific workplace."""
    from backend.event_stream import event_stream

    _WATCHED = ('connector_connected', 'connector_disconnected',
                'connector_paired', 'workplace_status_changed')

    def handler(data):
        if data.get('workplace_id') == workplace_id:
            ring.put((data['_event'], dict(data)))

    for ev in _WATCHED:
        event_stream.on(ev, handler)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        for ev in _WATCHED:
            event_stream.off(ev, handler)


# ---------------------------------------------------------------------------
# Priority-aware event scheduler
# ---------------------------------------------------------------------------

def _priority_round_robin(rings: dict, conn: RealtimeConnection) -> list:
    """Extract events from per-channel rings using weighted round-robin.

    Returns list of (channel, seq, sse_name, payload) tuples.
    """
    result = []
    l2_count = 0

    # First pass: L0 channels (update, status) — 1 event each
    for ch in ('update', 'status'):
        if ch not in rings:
            continue
        item = rings[ch].get()
        if item:
            seq, (sse_name, payload) = item
            result.append((ch, seq, sse_name, payload))

    # L1 channels (approvals) — 1 event
    if 'approvals' in rings:
        item = rings['approvals'].get()
        if item:
            seq, (sse_name, payload) = item
            result.append(('approvals', seq, sse_name, payload))

    # L2 channels (chat, workplace) — up to L2_WEIGHT events each
    for ch in ('chat', 'workplace'):
        if ch not in rings:
            continue
        for _ in range(L2_WEIGHT):
            item = rings[ch].get()
            if item:
                seq, (sse_name, payload) = item
                result.append((ch, seq, sse_name, payload))
                l2_count += 1
            else:
                break

    return result


# ---------------------------------------------------------------------------
# Differential push for chat
# ---------------------------------------------------------------------------

class ChatThrottle:
    """Batches thinking chunks: push first chunk immediately, then batch
    every throttle_ms, then push final event."""

    def __init__(self, throttle_ms: int = 100):
        self.throttle_ms = throttle_ms
        self._batch = []
        self._first_sent = False
        self._last_flush = 0
        # Highest chat seq among batched chunks — stamped on the merged event so
        # the client's _lastSeq advances over the chunks folded into the batch,
        # avoiding a spurious gap-fill (and duplicate CoT) per batch boundary.
        self._batch_seq = None

    def _merged_thinking(self):
        """Build the merged 'thinking' event from the current batch and reset it."""
        batched_content = ''.join(self._batch)
        self._batch = []
        seq = self._batch_seq
        self._batch_seq = None
        if not batched_content:
            return None
        ev = {'content': batched_content}
        if seq is not None:
            ev['seq'] = seq
        return ('thinking', ev)

    def feed(self, sse_name: str, payload: dict):
        """Feed a chat event. Returns list of events to emit now (may be empty)."""
        now_ms = time.monotonic() * 1000

        # 'thinking' chunks get batched
        if sse_name == 'thinking':
            if not self._first_sent:
                self._first_sent = True
                self._last_flush = now_ms
                return [('thinking', payload)]

            self._batch.append(payload.get('content', ''))
            if payload.get('seq') is not None:
                self._batch_seq = payload.get('seq')

            if (now_ms - self._last_flush) >= self.throttle_ms:
                self._last_flush = now_ms
                merged = self._merged_thinking()
                return [merged] if merged else []
            return []

        # Non-thinking event: flush any pending batch first
        result = []
        merged = self._merged_thinking()
        if merged:
            result.append(merged)

        result.append((sse_name, payload))
        self._first_sent = False
        return result

    def flush(self):
        """Flush any remaining batched content. Returns list of events."""
        merged = self._merged_thinking()
        return [merged] if merged else []


# ---------------------------------------------------------------------------
# Main SSE endpoint
# ---------------------------------------------------------------------------

@realtime_bp.route('/api/realtime/stream', methods=['GET'])
def api_realtime_stream():
    """Unified multiplexed SSE endpoint.

    Consolidates 5 separate EventSource connections into 1:
      - /api/agents/status/stream       -> channels=status
      - /api/approvals/stream           -> channels=approvals
      - /api/system/update/stream       -> channels=update
      - /api/agents/<id>/chat/stream    -> chat=1&session_id=...&agent_id=...
      - /api/workplaces/<id>/events     -> workplace=<id>
    """
    # Parse query parameters
    channels_str = request.args.get('channels', '')
    channels = set(filter(None, [ch.strip() for ch in channels_str.split(',')]))
    chat_enabled = request.args.get('chat') == '1'
    session_id = request.args.get('session_id', '').strip() or None
    agent_id = request.args.get('agent_id', '').strip() or None
    after_seq = request.args.get('after', 0, type=int)
    workplace_id = request.args.get('workplace', '').strip() or None
    chat_throttle_ms = request.args.get('chat_throttle', 100, type=int)

    # Validate chat parameters
    if chat_enabled and (not session_id or not agent_id):
        return Response(
            json.dumps({'error': 'session_id and agent_id required when chat=1'}),
            status=400,
            mimetype='application/json'
        )

    # Release thread-local DB connection (SSE thread is long-lived)
    from models.db import db
    db.close()

    # Build channel set
    all_channels = set(channels)
    if chat_enabled:
        all_channels.add('chat')
    if workplace_id:
        all_channels.add('workplace')

    if not all_channels:
        return Response(
            json.dumps({'error': 'At least one channel must be requested'}),
            status=400,
            mimetype='application/json'
        )

    # SSE connection limiting — max 5 concurrent per user/IP (FINDING-004)
    from flask import session as _flask_session
    from models.api_rate_limit import sse_register, sse_unregister, SSE_MAX_CONCURRENT
    _sse_id = (
        f"user:{_flask_session.get('_user_id', 'admin')}"
        if _flask_session.get('authenticated')
        else f"ip:{request.remote_addr or '0.0.0.0'}"
    )
    _sse_allowed, _sse_count = sse_register(_sse_id)
    if not _sse_allowed:
        return Response(
            json.dumps({
                'error': 'too_many_sse_connections',
                'message': f'Maximum {SSE_MAX_CONCURRENT} concurrent SSE connections allowed.',
                'retry_after': 30,
            }),
            status=429,
            headers={'Retry-After': '30'},
            mimetype='application/json'
        )

    # Thundering herd mitigation — check approximate connection count
    with _conn_lock:
        conn_count = len(_connections)
    max_conn = int(os.environ.get('WORKER_CONNECTIONS', 512))
    if max_conn > 0 and conn_count >= max_conn * 0.8:
        sse_unregister(_sse_id)
        return Response(
            json.dumps({'error': 'Server busy, please retry later'}),
            status=503,
            headers={'Retry-After': '10'},
            mimetype='application/json'
        )

    # Generate a connection ID
    conn_id = f"{id(request)}:{time.monotonic()}"

    # Token expiry: 24h from now
    expires_at = time.time() + 86400

    conn = RealtimeConnection(
        conn_id=conn_id,
        channels=all_channels,
        chat_session_id=session_id,
        agent_id=agent_id,
        after_seq=after_seq,
        workplace_id=workplace_id,
        chat_throttle_ms=chat_throttle_ms,
        expires_at=expires_at,
    )

    # Register connection
    with _conn_lock:
        _connections[conn_id] = conn

    # Build per-channel bounded rings
    rings: dict[str, BoundedRing] = {}
    for ch in all_channels:
        strategy = RING_STRATEGIES.get(ch, 'drop_oldest')
        size = RING_SIZES.get(ch, 32)
        rings[ch] = BoundedRing(ch, size, strategy)

    # Build circuit breakers
    breakers: dict[str, CircuitBreaker] = {}
    for ch in all_channels:
        breakers[ch] = CircuitBreaker(ch)

    # Store rings ref on the connection so api_realtime_resume can flush
    # pause buffers back into the rings after resume.
    conn.rings = rings

    # Start producer threads (task isolation)
    producers = {}
    stop_event = conn._stop_event  # shared stop signal

    _PRODUCERS = {
        'status': (_producer_status, {}),
        'approvals': (_producer_approval, {}),
        'update': (_producer_update, {}),
    }

    for ch in all_channels:
        if ch in _PRODUCERS:
            fn, kwargs = _PRODUCERS[ch]
            t = threading.Thread(
                target=_start_producer,
                args=(ch, fn, rings[ch], breakers[ch], stop_event, kwargs),
                daemon=True,
                name=f"realtime-producer-{ch}-{conn_id[:12]}"
            )
            producers[ch] = t
            t.start()
        elif ch == 'chat' and session_id:
            t = threading.Thread(
                target=_start_producer,
                args=(ch, _producer_chat, rings[ch], breakers[ch],
                      stop_event, {'session_id': session_id, 'after_seq': after_seq}),
                daemon=True,
                name=f"realtime-producer-{ch}-{conn_id[:12]}"
            )
            producers[ch] = t
            t.start()
        elif ch == 'workplace' and workplace_id:
            t = threading.Thread(
                target=_start_producer,
                args=(ch, _producer_workplace, rings[ch], breakers[ch],
                      stop_event, {'workplace_id': workplace_id}),
                daemon=True,
                name=f"realtime-producer-{ch}-{conn_id[:12]}"
            )
            producers[ch] = t
            t.start()

    # Chat throttler for differential push
    chat_throttle = ChatThrottle(chat_throttle_ms) if 'chat' in all_channels else None

    # Global sequence counter
    global_seq = after_seq
    chat_seq = after_seq

    # Build state snapshot
    snapshot_events = _build_snapshot(all_channels, agent_id, session_id, workplace_id)

    # Setup TCP_NODELAY for time-sensitive channels (approval, status, update)
    # This is done during the first write in the generator

    # SSE generator with priority scheduler
    @stream_with_context
    def generate():
        nonlocal global_seq, chat_seq

        # Set TCP keepalive and SIGPIPE handling on the socket
        try:
            # Ignore SIGPIPE at process level if not already done
            try:
                signal.signal(signal.SIGPIPE, signal.SIG_IGN)
            except (ValueError, OSError):
                pass  # can only be set in main thread
        except Exception:
            pass

        try:
            # --- Phase 1: Emit retry with jitter ---
            retry_ms = random.randint(3000, 8000)
            yield f"retry: {retry_ms}\n"

            # --- Phase 2: Push state snapshot ---
            for event_name, data in snapshot_events:
                global_seq += 1
                yield _format_sse_event(event_name, data,
                                        global_seq=global_seq)

            # --- Phase 3: Forward live events with priority scheduler ---
            last_heartbeat = time.monotonic()
            heartbeat_failures = 0

            while not conn.is_stopped():
                # Check token expiry
                if conn.check_expired():
                    global_seq += 1
                    yield _format_sse_event('auth_expired',
                                            {'message': 'Token expired, please reconnect'},
                                            global_seq=global_seq)
                    conn.stop()
                    break

                # Heartbeat
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    try:
                        yield "event: heartbeat\ndata: {}\n\n"
                        heartbeat_failures = 0
                        conn.last_heartbeat_time = now
                    except (BrokenPipeError, OSError):
                        heartbeat_failures += 1
                        if heartbeat_failures >= HEARTBEAT_MAX_FAILURES:
                            conn.stop()
                            break
                        time.sleep(1)
                        continue
                    last_heartbeat = now

                # Priority-aware extraction from rings
                events = _priority_round_robin(rings, conn)

                if not events:
                    # No events — short sleep to avoid busy-wait
                    time.sleep(0.05)
                    continue

                for channel, seq, sse_name, payload in events:
                    if conn.is_stopped():
                        break

                    global_seq += 1

                    # Build composite id for per-channel resume
                    if channel == 'chat':
                        chat_seq += 1
                        seq_id = f"chat:{chat_seq}"
                    else:
                        seq_id = f"{channel}:{global_seq}"

                    # Differential push for chat
                    if channel == 'chat' and chat_throttle:
                        throttled = chat_throttle.feed(sse_name, payload)
                        if not throttled:
                            continue
                        for t_name, t_payload in throttled:
                            global_seq += 1
                            chat_seq += 1
                            yield _format_sse_event(t_name, t_payload,
                                                    seq_id=f"chat:{chat_seq}",
                                                    global_seq=global_seq)
                    else:
                        # Check if paused — buffer chat/workplace events
                        if conn.paused and channel in ('chat', 'workplace'):
                            buf = conn._pause_buffers.get(channel)
                            if buf is None:
                                max_buf = PAUSE_BUFFER.get(channel, 32)
                                buf = BoundedRing(channel, max_buf, 'drop_oldest')
                                conn._pause_buffers[channel] = buf
                            buf.put((sse_name, payload))
                            continue

                        try:
                            yield _format_sse_event(sse_name, payload,
                                                    seq_id=seq_id,
                                                    global_seq=global_seq)
                            conn.last_write_ok = True
                        except (BrokenPipeError, OSError) as e:
                            log.warning("realtime %s: write failed: %s", conn_id, e)
                            conn.stop()
                            break

                # Check for dropped events per channel
                for ch_name, ring in rings.items():
                    dropped = ring.drain_dropped()
                    if dropped > 0:
                        try:
                            yield _format_sse_comment(f"x-sse-dropped {ch_name}:{dropped}")
                        except (BrokenPipeError, OSError):
                            conn.stop()
                            break

            # Flush chat throttler on disconnect
            if chat_throttle:
                for t_name, t_payload in chat_throttle.flush():
                    try:
                        global_seq += 1
                        chat_seq += 1
                        yield _format_sse_event(t_name, t_payload,
                                                seq_id=f"chat:{chat_seq}",
                                                global_seq=global_seq)
                    except (BrokenPipeError, OSError):
                        break

        except GeneratorExit:
            pass
        finally:
            # Cleanup: stop all producers
            conn.stop()
            for ch, t in producers.items():
                t.join(timeout=2)

            # Remove connection from registry
            with _conn_lock:
                _connections.pop(conn_id, None)

            # Unregister SSE connection (FINDING-004)
            sse_unregister(_sse_id)

            # Check for circuit-breaker channel_disabled events
            for ch_name in all_channels:
                if breakers.get(ch_name) and breakers[ch_name].is_disabled():
                    pass  # Already disabled

            log.debug("realtime %s: connection closed", conn_id[:20])

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


def _start_producer(channel: str, producer_fn, ring: BoundedRing,
                    breaker: CircuitBreaker, stop_event: threading.Event,
                    kwargs: dict):
    """Run a producer with task isolation and circuit breaker logic."""
    while not stop_event.is_set():
        if breaker.is_disabled():
            log.warning("realtime: channel %s disabled by circuit breaker", channel)
            return
        try:
            producer_fn(ring, breaker, stop_event, **kwargs)
            break  # producer returned normally
        except Exception as e:
            log.error("realtime: producer %s crashed: %s", channel, e, exc_info=True)
            should_stop = breaker.record_crash()
            if should_stop:
                log.error("realtime: channel %s circuit breaker open — stopping", channel)
                return
            # Wait before retry
            stop_event.wait(1)


# ---------------------------------------------------------------------------
# Pause/Resume endpoint (internal — called by client)
# ---------------------------------------------------------------------------

@realtime_bp.route('/api/realtime/pause', methods=['POST'])
def api_realtime_pause():
    """Pause chat+workplace event delivery for this session."""
    data = request.get_json() or {}
    session_id = data.get('session_id', '').strip()
    if not session_id:
        return Response(json.dumps({'error': 'session_id required'}), status=400,
                        mimetype='application/json')

    with _conn_lock:
        for conn in list(_connections.values()):
            if conn.chat_session_id == session_id:
                conn.pause()
    return Response(json.dumps({'ok': True}), mimetype='application/json')


@realtime_bp.route('/api/realtime/resume', methods=['POST'])
def api_realtime_resume():
    """Resume chat+workplace event delivery and flush paused buffer."""
    data = request.get_json() or {}
    session_id = data.get('session_id', '').strip()
    if not session_id:
        return Response(json.dumps({'error': 'session_id required'}), status=400,
                        mimetype='application/json')

    with _conn_lock:
        for conn in list(_connections.values()):
            if conn.chat_session_id == session_id:
                conn.resume()
                # Flush pause buffers — drain events that accumulated
                # while paused and re-insert them into the per-channel
                # rings so the generator yields them on the next pass.
                if conn.rings:
                    for ch in ('chat', 'workplace'):
                        buf = conn._pause_buffers.get(ch)
                        if buf:
                            ring = conn.rings.get(ch)
                            if ring:
                                for _seq, item in buf.get_all():
                                    ring.put(item)
    return Response(json.dumps({'ok': True}), mimetype='application/json')


# ---------------------------------------------------------------------------
# Deprecated old-SSE endpoint wrappers (keep functional, log deprecation)
# ---------------------------------------------------------------------------

_deprecated_logged: set = set()

def _warn_deprecated_once(msg: str):
    if msg not in _deprecated_logged:
        log.warning("DEPRECATED: %s", msg)
        _deprecated_logged.add(msg)
