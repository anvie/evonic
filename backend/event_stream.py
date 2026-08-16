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
        self._log_buffer: List[str] = []
        self._log_timer: Optional[threading.Timer] = None
        self._LOG_FLUSH_INTERVAL = 2.0
        self._LOG_BUFFER_LIMIT = 50
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='event')
        self._log_file: str = None  # resolved lazily to avoid import-time circular deps
        # Raw in-process sequence is retained for plugin compatibility only.
        self._seq_counter = itertools.count(1)

    def _get_log_file(self) -> str:
        if self._log_file is None:
            from config import EVENT_LOG_FILE
            self._log_file = EVENT_LOG_FILE
            os.makedirs(os.path.dirname(self._log_file), exist_ok=True)
        return self._log_file

    def _write_log(self, line: str):
        """Buffer a log line; flush when buffer is full or timer fires."""
        try:
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            formatted = f"[{ts}] {line}\n"
            with self._log_lock:
                self._log_buffer.append(formatted)
                if len(self._log_buffer) >= self._LOG_BUFFER_LIMIT:
                    self._do_flush()
                elif self._log_timer is None:
                    self._log_timer = threading.Timer(
                        self._LOG_FLUSH_INTERVAL, self._flush_log
                    )
                    self._log_timer.daemon = True
                    self._log_timer.start()
        except Exception as e:
            _logger.error("Failed to buffer log: %s", e)

    def _do_flush(self):
        """Flush buffered lines to disk. Caller must hold _log_lock."""
        if not self._log_buffer:
            return
        lines = list(self._log_buffer)
        self._log_buffer.clear()
        if self._log_timer:
            self._log_timer.cancel()
            self._log_timer = None
        try:
            log_file = self._get_log_file()
            with open(log_file, 'a', encoding='utf-8') as f:
                f.writelines(lines)
        except Exception as e:
            _logger.error("Failed to flush log: %s", e)

    def _flush_log(self):
        """Timer callback — acquire lock and flush."""
        with self._log_lock:
            self._do_flush()

    def flush_log(self):
        """Public flush — call on shutdown to drain remaining buffer."""
        with self._log_lock:
            self._do_flush()

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
        # Journal synchronously before asynchronous plugin listeners.  This is
        # what gives browser replay a stable total order even though raw plugin
        # callbacks still run concurrently.
        try:
            from backend.realtime_store import record_internal_event
            record_internal_event(event_name, data)
        except Exception as exc:
            _logger.error("Failed to journal realtime event '%s': %s", event_name, exc)
        preview = ', '.join(f'{k}={str(v)[:120]}' for k, v in data.items() if not k.startswith('_'))
        self._write_log(f"[seq={seq}] {event_name} | {preview}")
        with self._lock:
            listeners = list(self._listeners.get(event_name, []))
        for cb in listeners:
            self._executor.submit(self._safe_call, event_name, cb, data)

    def _safe_call(self, event_name: str, cb: Callable, data: dict):
        try:
            cb(data)
        except Exception as e:
            self._write_log(f"ERROR listener on '{event_name}': {e}")
            _logger.error("Listener error on '%s': %s", event_name, e)


event_stream = EventStream()
