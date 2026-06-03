"""
Lightweight event bus for agent runtime events.

Usage:
    from backend.event_stream import event_stream

    # Subscribe
    event_stream.on('processing_started', my_handler)

    # Emit
    event_stream.emit('processing_started', {'agent_id': ..., ...})

    # Unsubscribe
    event_stream.off('processing_started', my_handler)

Handlers are called asynchronously in a thread pool and must not block.
Events are logged to logs/events.log (configurable via EVENT_LOG_FILE in .env).
"""

import bisect
import collections
import itertools
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

_logger = logging.getLogger(__name__)


class EventStream:
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='event')
        self._log_file: str = None  # resolved lazily to avoid import-time circular deps
        # Sequence numbering and ring buffers for gap-fill recovery
        self._seq_counter = itertools.count(1)
        self._buffer_lock = threading.Lock()
        self._global_buffer: collections.deque = collections.deque(maxlen=1000)
        self._session_buffers: Dict[str, collections.deque] = {}
        self._web_listeners: Dict[str, int] = {}

    def _get_log_file(self) -> str:
        if self._log_file is None:
            from config import EVENT_LOG_FILE
            self._log_file = EVENT_LOG_FILE
            os.makedirs(os.path.dirname(self._log_file), exist_ok=True)
        return self._log_file

    def _write_log(self, line: str):
        try:
            log_file = self._get_log_file()
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            with self._log_lock:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"[{ts}] {line}\n")
        except Exception as e:
            _logger.error("Failed to write log: %s", e)

    def on(self, event_name: str, callback: Callable):
        """Subscribe a callback to an event."""
        with self._lock:
            self._listeners.setdefault(event_name, []).append(callback)

    def off(self, event_name: str, callback: Callable):
        """Unsubscribe a callback from an event."""
        with self._lock:
            if event_name in self._listeners:
                self._listeners[event_name] = [
                    cb for cb in self._listeners[event_name] if cb != callback
                ]

    def emit(self, event_name: str, data: dict):
        """Emit an event to all subscribers (non-blocking)."""
        seq = next(self._seq_counter)
        data['_seq'] = seq
        data['_event'] = event_name
        # Store in ring buffers for gap-fill queries
        session_id = data.get('session_id')
        entry = {'seq': seq, 'event': event_name, 'data': data}
        with self._buffer_lock:
            self._global_buffer.append(entry)
            if session_id:
                if session_id not in self._session_buffers:
                    self._session_buffers[session_id] = collections.deque(maxlen=500)
                self._session_buffers[session_id].append(entry)
        preview = ', '.join(f'{k}={str(v)[:120]}' for k, v in data.items() if not k.startswith('_'))
        self._write_log(f"[seq={seq}] {event_name} | {preview}")
        with self._lock:
            listeners = list(self._listeners.get(event_name, []))
        for cb in listeners:
            self._executor.submit(self._safe_call, event_name, cb, data)

    def get_events_in_range(self, session_id: str, after_seq: int, up_to_seq: int) -> list:
        """Return buffered events for session_id where after_seq < seq <= up_to_seq."""
        with self._buffer_lock:
            buf = self._session_buffers.get(session_id, collections.deque())
            if not buf:
                return []
            events = list(buf)
            seqs = [e['seq'] for e in events]
            lo = bisect.bisect_right(seqs, after_seq)
            hi = bisect.bisect_right(seqs, up_to_seq)
            return events[lo:hi]

    def get_session_events(self, session_id: str, after_seq: int = 0) -> list:
        """Return all buffered events for session_id with seq > after_seq."""
        with self._buffer_lock:
            buf = self._session_buffers.get(session_id, collections.deque())
            return [e for e in buf if e['seq'] > after_seq]

    def cleanup_session_buffer(self, session_id: str):
        """Remove per-session buffer (called after turn completes)."""
        with self._buffer_lock:
            self._session_buffers.pop(session_id, None)

    def register_web_listener(self, session_id: str):
        with self._lock:
            self._web_listeners[session_id] = self._web_listeners.get(session_id, 0) + 1

    def unregister_web_listener(self, session_id: str):
        with self._lock:
            count = self._web_listeners.get(session_id, 0) - 1
            if count <= 0:
                self._web_listeners.pop(session_id, None)
            else:
                self._web_listeners[session_id] = count

    def has_web_listener(self, session_id: str) -> bool:
        with self._lock:
            return self._web_listeners.get(session_id, 0) > 0

    def _safe_call(self, event_name: str, cb: Callable, data: dict):
        try:
            cb(data)
        except Exception as e:
            self._write_log(f"ERROR listener on '{event_name}': {e}")
            _logger.error("Listener error on '%s': %s", event_name, e)


event_stream = EventStream()
