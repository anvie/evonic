"""
runtime.py — AgentRuntime orchestrator.

Owns: message queue, worker threads, session locks, skill caches, handle_message,
session lifecycle, agent state management. Delegates heavy lifting to context,
llm_loop, and summarizer.
"""

import logging
import os
import signal
import sys
import time
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Callable, Any, Dict, Generator, List, Optional, TypeVar

T = TypeVar('T')

from models.db import db
from models.chatlog import chatlog_manager
from config import AGENT_TIMEOUT_RETRIES as MAX_TIMEOUT_RETRIES, AGENT_QUEUE_WORKERS

from backend.agent_runtime import context as _ctx
from backend.agent_runtime import llm_loop as _loop
from backend.agent_runtime import summarizer as _sum
from backend.agent_runtime.concurrency import ConcurrencyManager
from backend.agent_state import AgentState
from backend.agent_runtime.memory_manager import get_memories_for_context
from backend.channels.registry import channel_manager
from backend.event_stream import event_stream
from backend.plugin_manager import get_busy_message
from backend.slash_commands import parse_command, execute_command
from backend.agent_runtime.prefetch import TurnPrefetcher
import atexit
import re
from config import AGENT_MAX_TOOL_RESULT_CHARS as MAX_TOOL_RESULT_CHARS

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOGS_DIR = os.path.join(_BASE_DIR, 'logs')

_logger = logging.getLogger(__name__)


# --- Configuration constants ---
CLEANUP_INTERVAL_SECONDS = 300       # Interval between idle session cleanup sweeps (5 minutes)
CLEANUP_TTL_SECONDS = 3600          # TTL for session state entries before cleanup (1 hour)
WORKER_JOIN_TIMEOUT_SECONDS = 5.0   # Max time to wait for worker threads to finish on shutdown
WORKER_JOIN_MIN_TIMEOUT = 0.1       # Minimum timeout per worker join iteration (seconds)
DEFAULT_BUFFER_SECONDS = 2          # Default message buffering delay when agent has no config (seconds)
SESSION_BUFFER_CLEANUP_DELAY = 30.0 # Delay before cleaning up SSE session buffers (seconds)


def _llm_log_path(agent_id: str) -> str:
    return os.path.join(_LOGS_DIR, 'agents', agent_id, 'llm.log')


def _db_retry(
    fn: Callable[..., T],
    *args: Any,
    retries: int = 2,
    delay: float = 0.5,
    label: str = "DB operation",
    **kwargs: Any,
) -> T:
    """Retry a DB operation with short delays on transient failures."""
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < retries:
                _logger.warning("%s failed (attempt %d/%d), retrying in %.1fs: %s",
                                label, attempt + 1, retries + 1, delay, e)
                time.sleep(delay)
            else:
                _logger.error("%s failed after %d attempts: %s", label, retries + 1, e)
                raise


@dataclass
class SessionContext:
    """Groups the session-scoped identifiers that flow through the processing pipeline.

    Replaces the repetitive (session_id, external_user_id, channel_id,
    session_db_agent_id) parameter quartet in _process_and_respond,
    _do_process, and _do_process_inner.

    session_db_agent_id: when set, all DB session reads/writes use this
        agent's per-agent DB instead of the processing agent's DB.  Used
        for cross-agent sessions where agent A processes a session owned by
        agent B.
    """
    session_id: str
    external_user_id: str
    channel_id: Optional[str] = None
    session_db_agent_id: Optional[str] = None


class _QueueTask:
    """A unit of work for the message processing queue."""
    __slots__ = ('agent', 'ctx', 'send_via_channel', 'result', 'event')

    def __init__(self, agent: dict, ctx: SessionContext,
                 send_via_channel: bool = False):
        self.agent = agent
        self.ctx = ctx
        self.send_via_channel = send_via_channel
        self.result: Optional[dict] = None
        self.event = threading.Event()


# ─────────────────────────────────────────────────────────────────────────────
# State containers — group related class-level state to reduce namespace pollution
#
# THREAD-SAFETY & LOCK ORDERING
# ─────────────────────────────────────────────────────────────────────────────
#
# The runtime is multi-threaded: worker threads process messages concurrently,
# a cleanup timer runs periodically, and background tasks execute in a shared
# ThreadPoolExecutor.  To prevent data races and deadlocks the following rules
# apply:
#
# 1. LOCK ORDERING (always acquire in this order to prevent deadlocks):
#      (a) _session_store._locks_guard
#      (b) _session_store._stop_flags_guard
#      (c) _session_store._inject_queues_guard
#      (d) _session_store._busy_guard
#      (e) _agent_tracker._guard
#      (f) _cleanup_tracker._guard
#      (g) _llm_serializer._summarize_guard
#      (h) _llm_serializer._llm_lock
#      (i) _shutdown_mgr._lock
#      (j) instance._buffer_lock
#
# 2. GUARD-LOCK PATTERN:  Each mutable dict has a dedicated "guard" lock.
#    The guard protects structuring operations (get-or-create, pop, clear).
#    The contained objects (per-session Locks, Events, Queues) are themselves
#    thread-safe primitives and do NOT need the guard for normal use.
#
# 3. NESTING: When multiple guards must be held simultaneously, always
#    acquire them in the order listed above.  Never hold a per-session lock
#    while acquiring its parent guard lock.
#
# 4. TIMEOUTS: Lock acquisitions are blocking (no timeout).  Critical
#    sections are kept short (< 1ms typical) to minimise contention.
#
# 5. INVARIANTS:
#    • Every session_id present in _cleanup_tracker._ttl MUST also have
#      entries in _session_store (or be in the process of being cleaned up).
#    • _agent_tracker._busy[agent_id] exists only while an agent is
#      actively processing a turn; cleared on completion or TTL expiry.
#    • _shutdown_mgr._event, once set, is never cleared (shutdown is final).
#
# ─────────────────────────────────────────────────────────────────────────────

class _SessionStore:
    """Per-session mutable state: locks, stop flags, inject queues, busy flags.

    Thread-safety:
        Each dict is accessed from multiple worker threads.  A dedicated
        guard lock protects the structuring operations (get-or-create, pop)
        on each dict so that concurrent workers never see a partially-mutated
        mapping.

        Lock ordering within this class (always acquire in this order):
            1. _locks_guard
            2. _stop_flags_guard
            3. _inject_queues_guard
            4. _busy_guard

        Deadlock avoidance: never hold two guard locks from this class at
        the same time unless following the order above.  The cleanup method
        acquires each guard individually (not nested) when removing stale
        entries, so no cross-guard deadlock is possible.

        Invariants:
            • All four dicts share the same set of session_id keys at any
              instant (entries are added and removed together).
            • Per-session Locks in _locks are never shared across sessions.
    """

    def __init__(self) -> None:
        # ── Per-session processing locks ──────────────────────────────────
        # Purpose: Prevent concurrent processing of messages for the same
        #          session_id by different worker threads.
        # Protects: self._locks dict (session_id -> threading.Lock).
        # Acquired by: _get_session_lock() during get-or-create.
        # Released: immediately after the per-session Lock is returned.
        # Deadlock risk: NONE — _locks_guard is never held while acquiring
        #                a per-session Lock (the lock is returned first).
        # Invariant: every session_id that has an active entry in the
        #           runtime MUST have a corresponding Lock in _locks.
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

        # ── Per-session stop flags ────────────────────────────────────────
        # Purpose: Allow external callers to signal a session's agent loop
        #          to stop after the current LLM call completes.
        # Protects: self._stop_flags dict (session_id -> threading.Event).
        # Acquired by: _get_stop_event() during get-or-create, and during
        #              cleanup in _cleanup_idle_sessions().
        # Released: immediately after the Event is returned.
        # Deadlock risk: NONE — guard is held only for dict mutation.
        #                The returned Event is used outside the lock.
        # Invariant: calling request_stop(sid) will always find or create
        #           an Event; set() is idempotent and thread-safe.
        self._stop_flags: Dict[str, threading.Event] = {}
        self._stop_flags_guard = threading.Lock()

        # ── Per-session injection queues ──────────────────────────────────
        # Purpose: Queue for injecting messages into an active session from
        #          external sources (e.g. tool callbacks, cross-agent calls).
        # Protects: self._inject_queues dict (session_id -> queue.Queue).
        # Acquired by: _get_inject_queue() during get-or-create, and during
        #              cleanup in _cleanup_idle_sessions().
        # Released: immediately after the Queue is returned.
        # Deadlock risk: NONE — queue.Queue is itself thread-safe; the
        #                guard only protects dict structure.
        # Invariant: queues are never removed while a session is active;
        #           they are cleaned up only during idle-session sweeps.
        self._inject_queues: Dict[str, queue.Queue] = {}
        self._inject_queues_guard = threading.Lock()

        # ── Per-session busy flags ────────────────────────────────────────
        # Purpose: Track whether a session is currently being processed.
        #          Enables rapid rejection / queuing of duplicate messages.
        # Protects: self._busy dict (session_id -> bool).
        # Acquired by: _set_busy(), _is_busy(), and during cleanup.
        # Released: immediately after read or write.
        # Deadlock risk: NONE — held only for single get/set operations.
        # Invariant: _busy[sid] == True only while a worker thread is
        #           actively processing a turn for that session.
        self._busy: Dict[str, bool] = {}
        self._busy_guard = threading.Lock()


class _AgentTracker:
    """Track which agents are currently busy processing a session.

    Thread-safety:
        The _guard lock protects all reads and writes to the _busy dict,
        which is mutated by worker threads when agents start or finish
        processing turns.

        Acquired by: _set_agent_busy(), _clear_agent_busy(),
                     is_agent_busy(), get_busy_agents().
        Released: immediately after the dict operation (short critical
                  section — < 1ms).

        Deadlock risk: NONE — _guard is never nested with any other lock.
        It is acquired independently each time.

        TTL staleness: entries older than the TTL (default 600s) are
        treated as stale and auto-expired.  This protects against hung
        threads that never clear their busy flag.

        Invariants:
            • An agent_id appears in _busy only while it is actively
              processing an LLM turn.
            • Each entry has {session_id: str, started_at: float}.
            • At most one entry per agent_id (set overwrites).
    """

    def __init__(self) -> None:
        # agent_id -> {session_id: str, started_at: float}
        # Guarded by _guard — prevents races between agent_busy set/clear
        # calls coming from different worker threads.
        self._busy: Dict[str, dict] = {}
        self._guard = threading.Lock()


class _CleanupTracker:
    """TTL-based idle-session cleanup: session timestamps + periodic timer.

    Thread-safety:
        The _guard lock serializes access to the _ttl dict and the _timer
        reference.  Multiple worker threads call _touch_session concurrently,
        and the periodic cleanup timer reads the dict on a separate thread.

        Acquired by: _touch_session() (write), _cleanup_idle_sessions()
                     (read + write), graceful_shutdown() (write to clear timer).
        Released: immediately after the dict/timer operation.

        Deadlock risk: LOW — the cleanup method acquires _guard to read
        stale session IDs, then re-acquires it individually for each
        removal.  It also acquires each _SessionStore guard lock
        independently (not nested with _guard) when popping stale entries,
        following the documented lock ordering.

        Timer lifecycle invariant:
            • _timer is None initially (no cleanup scheduled).
            • _touch_session lazily starts the timer on first use.
            • After each cleanup sweep, a new timer is scheduled.
            • graceful_shutdown cancels and nullifies the timer.

        Invariant: every session_id in _ttl corresponds to a session
                   that has been touched at least once since startup.
    """

    def __init__(self) -> None:
        # session_id -> last_active_ts (float, time.time())
        # Guarded by _guard — worker threads update timestamps concurrently
        # while the cleanup timer thread iterates and prunes entries.
        self._ttl: Dict[str, float] = {}
        self._guard = threading.Lock()
        self._interval = CLEANUP_INTERVAL_SECONDS
        self._ttl_seconds = CLEANUP_TTL_SECONDS
        # Periodic Timer that fires _cleanup_idle_sessions().
        # Guarded by _guard — read/write must be synchronised with workers.
        self._timer: Optional[threading.Timer] = None


class _LLMSerializer:
    """Serialize LLM access: summarization guard, global semaphore, concurrency manager.

    Thread-safety:
        Two independent mechanisms manage different aspects of LLM concurrency:

        ┌────────────────────┬──────────────────────────────────────────────────────┐
        │ Mechanism          │ Purpose                                              │
        ├────────────────────┼──────────────────────────────────────────────────────┤
        │ _summarize_guard   │ Ensures only ONE summarization runs at a time.       │
        │                    │ Summarizations are expensive and may conflict with   │
        │                    │ active LLM turns for the same session.               │
        ├────────────────────┼──────────────────────────────────────────────────────┤
        │ _llm_lock          │ BoundedSemaphore that limits concurrent LLM API      │
        │ (BoundedSemaphore) │ calls globally.  Controlled by the DB setting        │
        │                    │ max_concurrent_llm_global (default 1).               │
        │                    │ Set higher for providers that support parallel reqs. │
        └────────────────────┴──────────────────────────────────────────────────────┘

        Lock ordering: _summarize_guard MUST be acquired before _llm_lock
        if both are needed in the same code path.  In practice they are
        used independently.

        Deadlock risk: NONE — neither is ever held while acquiring
        the other.  Both are short-held (< duration of the operation).

        _summarize_active invariant: the set contains session_ids that
        currently have a summarization task in flight.  It is modified
        only while holding _summarize_guard.

        _concurrency_mgr: per-agent/per-model turn concurrency manager.
        Not locked here — managed internally by the ConcurrencyManager.
    """

    def __init__(self) -> None:
        # session_ids with active summarization tasks.
        # Guarded by _summarize_guard — prevents concurrent summarization
        # runs from overlapping (at most one summarization at a time).
        self._summarize_active: set = set()
        self._summarize_guard = threading.Lock()

        # Global LLM concurrency limiter — replaces the old threading.Lock()
        # (binary, max 1) with a BoundedSemaphore controlled by the DB setting
        # max_concurrent_llm_global (default 1, preserving existing behaviour).
        # Acquired before each LLM call, released after response.
        # BoundedSemaphore raises ValueError on over-release — defensive.
        limit = self._load_llm_global_limit()
        self._llm_lock = threading.BoundedSemaphore(limit)

        # Per-agent/per-model turn concurrency manager (set in __init__).
        # Managed internally — no external locking required.
        self._concurrency_mgr = None

    # ── Private helpers ─────────────────────────────────────────────────

    def _load_llm_global_limit(self) -> int:
        """Read max_concurrent_llm_global setting (default 1)."""
        try:
            from models.db import db
            return max(1, int(db.get_setting('max_concurrent_llm_global', '1')))
        except Exception:
            return 1

    # ── Public API ─────────────────────────────────────────────────────

    def refresh_llm_global_limit(self) -> None:
        """Re-read max_concurrent_llm_global and recreate the BoundedSemaphore.

        Threads already blocked on the old semaphore will drain naturally.
        """
        new_limit = self._load_llm_global_limit()
        _logger.info("_llm_lock: refreshing global semaphore limit → %d", new_limit)
        self._llm_lock = threading.BoundedSemaphore(new_limit)


class _ShutdownManager:
    """Graceful shutdown coordination: event, lock, signal registration flag.

    Thread-safety:
        _lock guards the idempotent shutdown sequence so that concurrent
        calls to graceful_shutdown() do not double-fire the event or
        repeat cleanup steps.

        Acquired by: graceful_shutdown() (check-then-set pattern).
        Released: immediately after setting the event.

        Deadlock risk: NONE — _lock is never nested with any other lock.
        It is acquired only at the very start of graceful_shutdown().

        Invariants:
            • _event, once set, is NEVER cleared (shutdown is irreversible).
            • _signal_registered is written exactly once (guarded by the
              same flag check in _register_signal_handlers).
            • After _event is set, all worker threads will exit their loops
              after completing their current task.
    """

    def __init__(self) -> None:
        # Signalled when graceful shutdown is requested.
        # Workers poll this to decide whether to exit their loop.
        # Once set, this is never cleared.
        self._event = threading.Event()

        # Protects the idempotent check-set in graceful_shutdown().
        # Ensures only one thread actually triggers the shutdown sequence.
        self._lock = threading.Lock()

        # Write-once flag: prevents duplicate signal handler registration.
        # Not guarded by a lock — registration happens during init on the
        # main thread before any workers start.
        self._signal_registered = False


class AgentRuntime:
    # State containers — reduce class-level attributes from 23 to 6
    _session_store = _SessionStore()
    _agent_tracker = _AgentTracker()
    _cleanup_tracker = _CleanupTracker()
    _llm_serializer = _LLMSerializer()
    _shutdown_mgr = _ShutdownManager()
    # Shared pool for background tasks (summarization, etc.) — prevents thread orphaning.
    # max_workers=4 ensures a single hung/stuck task won't starve all background work.
    # Background tasks are fire-and-forget — submitted tasks should have their own
    # internal timeouts to prevent indefinite worker occupation.
    _bg_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='agent-bg')

    @classmethod
    def graceful_shutdown(cls) -> None:
        """Signal all workers to stop after current task, drain queue, join threads,
        shut down the background executor, and cancel the periodic cleanup timer."""
        with cls._shutdown_mgr._lock:
            if cls._shutdown_mgr._event.is_set():
                return  # Already shutting down
            cls._shutdown_mgr._event.set()

        _logger.info("Graceful shutdown initiated — draining message queue...")

        # Cancel periodic cleanup timer
        with cls._cleanup_tracker._guard:
            timer = cls._cleanup_tracker._timer
            cls._cleanup_tracker._timer = None
        if timer is not None:
            timer.cancel()

        # Wait for all registered workers to finish
        # (workers list is per-instance; we rely on the atexit handler
        #  being registered once but workers being tracked per-instance)
        # We don't hold a class-level workers list here — instances call
        # _join_workers() themselves. The executor is shared, so we shut it down.
        _logger.info("Shutting down background executor...")
        cls._bg_executor.shutdown(wait=False, cancel_futures=True)

        # Cancel the cleanup timer (already done above, but defensive)
        _logger.info("Graceful shutdown complete.")

    @classmethod
    def _signal_handler(cls, signum: int, frame: Optional[Any]) -> None:
        """Handle SIGTERM/SIGINT by triggering graceful shutdown, then sys.exit
        to allow atexit handlers (including Docker container cleanup) to run."""
        sig_name = signal.Signals(signum).name
        _logger.info("\nReceived %s, initiating graceful shutdown...", sig_name)
        cls.graceful_shutdown()
        # Use sys.exit to allow normal Python shutdown — this triggers
        # atexit handlers (including Docker container cleanup) and
        # thread cleanup, whereas os.kill hard-terminates the process.
        sys.exit(0)

    @classmethod
    def _register_signal_handlers(cls) -> None:
        """Register SIGTERM/SIGINT handlers and atexit callback (once only)."""
        if cls._shutdown_mgr._signal_registered:
            return
        cls._shutdown_mgr._signal_registered = True
        try:
            signal.signal(signal.SIGTERM, cls._signal_handler)
            signal.signal(signal.SIGINT, cls._signal_handler)
        except (OSError, ValueError):
            # signal() can only be called from main thread — non-fatal
            pass

    @classmethod
    def _cleanup_idle_sessions(cls) -> None:
        """Remove stale per-session state entries that haven't been active recently."""
        now = time.time()
        stale = []
        with cls._cleanup_tracker._guard:
            for sid, ts in cls._cleanup_tracker._ttl.items():
                if now - ts > cls._cleanup_tracker._ttl_seconds:
                    stale.append(sid)

        if stale:
            _logger.info("Cleaned up stale state for %d session(s)", len(stale))
            for sid in stale:
                with cls._cleanup_tracker._guard:
                    cls._cleanup_tracker._ttl.pop(sid, None)
                with cls._session_store._locks_guard:
                    cls._session_store._locks.pop(sid, None)
                with cls._session_store._stop_flags_guard:
                    cls._session_store._stop_flags.pop(sid, None)
                with cls._session_store._inject_queues_guard:
                    cls._session_store._inject_queues.pop(sid, None)
                with cls._session_store._busy_guard:
                    cls._session_store._busy.pop(sid, None)

        # Schedule next cleanup
        cls._cleanup_tracker._timer = threading.Timer(cls._cleanup_tracker._interval, cls._cleanup_idle_sessions)
        cls._cleanup_tracker._timer.daemon = True
        cls._cleanup_tracker._timer.start()

    @classmethod
    def _touch_session(cls, session_id: str) -> None:
        """Mark session as active (called on every turn)."""
        with cls._cleanup_tracker._guard:
            cls._cleanup_tracker._ttl[session_id] = time.time()
            if cls._cleanup_tracker._timer is None:
                cls._cleanup_tracker._timer = threading.Timer(cls._cleanup_tracker._interval, cls._cleanup_idle_sessions)
                cls._cleanup_tracker._timer.daemon = True
                cls._cleanup_tracker._timer.start()

    def __init__(self):
        self._message_queue: queue.Queue[_QueueTask] = queue.Queue()
        # Guarded by _buffer_lock — handle_message and request_stop may
        # concurrently create or cancel timers for the same session.
        self._buffer_timers: Dict[str, threading.Timer] = {}
        self._buffer_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        # Read worker count from DB (user-configurable), fall back to config default
        try:
            from models.db import db as _db
            _db_workers = _db.get_setting('agent_queue_workers')
            initial_workers = max(1, min(32, int(_db_workers))) if _db_workers else AGENT_QUEUE_WORKERS
        except Exception:
            initial_workers = AGENT_QUEUE_WORKERS
        for i in range(initial_workers):
            t = threading.Thread(target=self._worker, name=f'agent-worker-{i}', daemon=True)
            t.start()
            self._workers.append(t)
        _logger.info("Started %d queue worker(s)", initial_workers)
        AgentRuntime._llm_serializer._concurrency_mgr = ConcurrencyManager()
        self._session_skill_mds: Dict[str, Dict[str, str]] = {}    # session_id -> {skill_id: system_md}
        self._session_skill_tools: Dict[str, Dict[str, list]] = {}  # session_id -> {skill_id: [tool_defs]}
        self._prefetcher = TurnPrefetcher()  # pre-loads messages for next turn
        # Register signal handlers + atexit for graceful shutdown (once only)
        AgentRuntime._register_signal_handlers()
        atexit.register(self._atexit_shutdown)
        # Schedule periodic cleanup of stale buffer timers
        self._buffer_timer_stats = {"created": 0, "cancelled": 0, "leaked": 0}
        self._stale_timer_cleanup()

    def _atexit_shutdown(self) -> None:
        AgentRuntime.graceful_shutdown()
        # Join this instance's worker threads with timeout
        deadline = time.time() + WORKER_JOIN_TIMEOUT_SECONDS
        for t in self._workers:
            remaining = max(WORKER_JOIN_MIN_TIMEOUT, deadline - time.time())
            t.join(timeout=remaining)
        # Cancel any pending buffer timers (defensive: don't let one failure
        # prevent cancelling the rest)
        with self._buffer_lock:
            for timer in self._buffer_timers.values():
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._buffer_timers.clear()

    def resize_workers(self, desired: int) -> dict:
        """Dynamically adjust worker thread count.

        Spawns new workers immediately if desired > current.
        Shrinking requires restart (threads block on queue.get).
        Returns {"previous": int, "current": int, "note": str|None}.
        """
        desired = max(1, min(32, desired))
        current = len(self._workers)
        note = None
        if desired > current:
            for i in range(current, desired):
                t = threading.Thread(target=self._worker, name=f'agent-worker-{i}', daemon=True)
                t.start()
                self._workers.append(t)
            _logger.info("Resized agent workers from %d to %d", current, desired)
        elif desired < current:
            note = "Decrease takes effect after restart"
            _logger.info("Agent workers decrease requested (%d -> %d); takes effect after restart", current, desired)
        return {"previous": current, "current": len(self._workers), "note": note}

    def _worker(self) -> None:
        while True:
            task = self._message_queue.get()
            try:
                result = self._process_and_respond(task.agent, task.ctx)
                task.result = result

                # Send response via channel (only for buffered tasks — caller handles non-buffered)
                _resp = result.get('response', '')
                if task.send_via_channel and _resp and _resp != "(No response)" and task.ctx.channel_id:
                    instance = channel_manager._active.get(task.ctx.channel_id)
                    if instance and instance.is_running:
                        try:
                            instance.send_message(task.ctx.external_user_id, result['response'])
                        except Exception as e:
                            _logger.error("Channel send error for session %s: %s", task.ctx.session_id, e)
            except Exception as e:
                _logger.error("Worker error for session %s: %s", task.ctx.session_id, e, exc_info=True)
                task.result = {
                    "response": "An unexpected error occurred. Please try again.",
                    "error": True,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "error_traceback": traceback.format_exc(),
                    "tool_trace": [],
                    "timeline": [],
                    "context": {
                        "agent_id": task.agent.get("id"),
                        "session_id": task.ctx.session_id,
                        "external_user_id": task.ctx.external_user_id,
                        "channel_id": task.ctx.channel_id,
                    }
                }
            finally:
                task.event.set()
                self._message_queue.task_done()
            # After finishing a task, check if we should shut down
            if AgentRuntime._shutdown_mgr._event.is_set():
                _logger.info("Worker %s exiting (shutdown)", threading.current_thread().name)
                break

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """Get or create a per-session lock to prevent concurrent processing."""
        with self._session_store._locks_guard:
            if session_id not in self._session_store._locks:
                self._session_store._locks[session_id] = threading.Lock()
            return self._session_store._locks[session_id]

    def _get_stop_event(self, session_id: str) -> threading.Event:
        """Get or create the stop Event for a session."""
        with self._session_store._stop_flags_guard:
            if session_id not in self._session_store._stop_flags:
                self._session_store._stop_flags[session_id] = threading.Event()
            return self._session_store._stop_flags[session_id]

    def _get_inject_queue(self, session_id: str) -> queue.Queue:
        """Get or create the injection queue for a session."""
        with self._session_store._inject_queues_guard:
            if session_id not in self._session_store._inject_queues:
                self._session_store._inject_queues[session_id] = queue.Queue()
            return self._session_store._inject_queues[session_id]

    def _set_busy(self, session_id: str, busy: bool) -> None:
        with self._session_store._busy_guard:
            self._session_store._busy[session_id] = busy

    def _is_busy(self, session_id: str) -> bool:
        with self._session_store._busy_guard:
            return self._session_store._busy.get(session_id, False)

    def _set_agent_busy(self, agent_id: str, session_id: str) -> None:
        with self._agent_tracker._guard:
            self._agent_tracker._busy[agent_id] = {'session_id': session_id, 'started_at': time.time()}

    def _clear_agent_busy(self, agent_id: str) -> None:
        with self._agent_tracker._guard:
            self._agent_tracker._busy.pop(agent_id, None)

    def is_agent_busy(self, agent_id: str, ttl: int = 600) -> bool:
        """Return True if agent is currently processing an LLM turn.

        A TTL guard treats entries older than `ttl` seconds as stale (e.g. a
        thread that hung and never cleared its flag).  Default is 10 minutes.
        """
        with self._agent_tracker._guard:
            entry = self._agent_tracker._busy.get(agent_id)
        if not entry:
            return False
        if time.time() - entry['started_at'] > ttl:
            # Auto-expire stale entry
            self._clear_agent_busy(agent_id)
            return False
        return True

    def get_busy_agents(self, ttl: int = 600) -> dict:
        """Return a snapshot of all currently busy agents (respects TTL)."""
        now = time.time()
        with self._agent_tracker._guard:
            snapshot = dict(self._agent_tracker._busy)
        result = {}
        stale = []
        for agent_id, entry in snapshot.items():
            elapsed = now - entry['started_at']
            if elapsed > ttl:
                stale.append(agent_id)
            else:
                result[agent_id] = {
                    'session_id': entry['session_id'],
                    'started_at': entry['started_at'],
                    'elapsed': round(elapsed, 1),
                }
        for agent_id in stale:
            self._clear_agent_busy(agent_id)
        return result

    @contextmanager
    def _buffer_timer(self, session_id: str, buffer_seconds: float,
                      callback: Callable, *args: Any) -> "Generator[threading.Timer, None, None]":
        """Context manager that guarantees timer cleanup even on exception.

        Cancels and removes the timer in all cases: normal exit, early return,
        or exception.  This prevents the timer leak that could occur if a
        timer is created and stored in _buffer_timers but an exception
        prevents it from being started or cancelled.
        """
        timer = threading.Timer(buffer_seconds, callback, args=args)
        timer.daemon = True
        self._buffer_timers[session_id] = timer
        self._buffer_timer_stats["created"] += 1
        try:
            yield timer
        finally:
            with self._buffer_lock:
                removed = self._buffer_timers.pop(session_id, None)
            if removed is not None:
                removed.cancel()
            self._buffer_timer_stats["cancelled"] += 1

    def _stale_timer_cleanup(self) -> None:
        """Periodic cleanup of stale buffer timers."""
        try:
            with self._buffer_lock:
                stale_ids = [sid for sid, t in list(self._buffer_timers.items()) if not t.is_alive()]
                for sid in stale_ids:
                    self._buffer_timers.pop(sid, None)
                    self._buffer_timer_stats["leaked"] += 1
        except Exception:
            pass
        # Schedule next cleanup
        t = threading.Timer(30.0, self._stale_timer_cleanup)
        t.daemon = True
        t.start()

    def request_stop(self, session_id: str) -> None:
        """Signal the agent loop for this session to stop after the current LLM call.
        Also cancels any pending buffer timer so no new task is enqueued.
        Kills any running tool subprocess immediately via process_tracker."""
        with self._buffer_lock:
            timer = self._buffer_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()
        self._get_stop_event(session_id).set()
        # Kill any running tool subprocess for this session
        from backend.tools.lib.process_tracker import process_tracker
        process_tracker.kill(session_id)

    def handle_message(self, agent_id: str, external_user_id: str,
                       message: str, channel_id: Optional[str] = None,
                       image_url: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None,
                       skip_buffer: bool = False) -> Dict[str, Any]:
        """Process an incoming user message. Always queued for processing.

        - With buffer: debounce rapid messages, queue when timer fires.
        - Without buffer: queue immediately and wait for result.

        Args:
            image_url: Optional base64 data URL or http URL for vision-enabled agents.
            metadata: Optional extra metadata merged into the saved message record.
            skip_buffer: If True, bypass message buffering even if the agent has
                message_buffer_seconds set. Used by API routes that need a synchronous
                response (e.g. /chat/completions).
        """
        # Normalize external_user_id — system-internal messages (e.g. restart
        # greeting) may arrive with None when no external user is associated.
        if external_user_id is None:
            external_user_id = '__system__'

        _logger.info(
            "[handle_message] agent=%s sender=%s msg_preview=%.80r",
            agent_id, external_user_id, message,
        )

        agent = db.get_agent(agent_id)
        db_agent_id = agent_id  # Default: agent's own per-agent chat DB
        if not agent:
            # Check for in-memory sub-agent (spawned by a parent agent)
            from backend.subagent_manager import subagent_manager
            agent = subagent_manager.get(agent_id)
            if agent:
                db_agent_id = agent.get('parent_id', agent_id)
        if not agent:
            return {"response": "Agent not found.", "tool_trace": []}

        # Block disabled agents (super agent is always allowed; sub-agents inherit parent's enabled state)
        if not agent.get('is_super') and not agent.get('enabled', True):
            return {"response": "This agent is currently disabled.", "tool_trace": []}

        # Determine if this is a sub-agent (uses parent's per-agent chat DB)
        is_subagent = agent.get('is_subagent', False)

        # Get or create session (sub-agents store their own ID but use parent's DB)
        session_id = _db_retry(db.get_or_create_session, agent_id, external_user_id,
                               channel_id, db_agent_id=db_agent_id if is_subagent else None,
                               label="get/create session")

        # Sub-agents always start fresh — clear any stale messages from a
        # previous spawn that reused the same session slug.
        if is_subagent and metadata and metadata.get('subagent_spawn'):
            db.clear_session(session_id, agent_id=db_agent_id)

        # Slash command interception — execute before saving message or sending to LLM
        parsed = parse_command(message)
        if parsed:
            cmd_name, cmd_args = parsed
            response = execute_command(
                cmd_name, cmd_args, session_id, agent_id,
                external_user_id, channel_id,
            )
            if response is not None:
                # Command was recognized — save command echo and response, then return
                _db_retry(db.add_chat_message, session_id, 'user', message,
                          agent_id=db_agent_id, metadata={"slash_command": True},
                          label="save command message")
                _db_retry(db.add_chat_message, session_id, 'assistant', response,
                          agent_id=db_agent_id, metadata={"slash_command": True},
                          label="save command response")
                _cl = chatlog_manager.get(db_agent_id, session_id)
                _cl.append({'type': 'user', 'session_id': session_id, 'content': message,
                             'sender_id': external_user_id,
                             'metadata': {'slash_command': True}})
                _cl.append({'type': 'system', 'session_id': session_id, 'content': response,
                             'metadata': {'slash_command': True}})
                # Signal the client to clear the chat UI when the clear command was used
                extra = {"clear_ui": True} if cmd_name == "clear" else {}
                extra["slash_command"] = True  # flag so frontend skips thinking bubble
                self._prefetcher.invalidate(session_id)
                return {"response": response, "tool_trace": [], "timeline": [], **extra}
            # Unknown command — fall through to normal LLM processing

        # Save user message (store image reference and any extra metadata)
        meta = {"image_url": image_url} if image_url else {}
        if metadata:
            meta.update(metadata)
        # Enrich metadata for agent-originated messages
        if external_user_id.startswith("__agent__") and not meta.get('agent_message'):
            sender_id = external_user_id[len("__agent__"):]
            sender_agent = db.get_agent(sender_id)
            meta['agent_message'] = True
            meta['from_agent_id'] = sender_id
            meta['from_agent_name'] = sender_agent.get('name', sender_id) if sender_agent else sender_id
        _db_retry(db.add_chat_message, session_id, 'user', message or "[Image]",
                  agent_id=db_agent_id, metadata=meta if meta else None, label="save user message")
        _cl_user = chatlog_manager.get(db_agent_id, session_id)
        _cl_user_entry = {'type': 'user', 'session_id': session_id,
                           'content': message or '[Image]', 'sender_id': external_user_id}
        if meta:
            _cl_user_entry['metadata'] = meta
        _cl_user.append(_cl_user_entry)

        # Invalidate any prefetched context for this session — a new user
        # message arrived, so the cached messages are stale.
        self._prefetcher.invalidate(session_id)

        # Emit message_received event
        event_stream.emit('message_received', {
            'agent_id': agent_id,
            'agent_name': agent.get('name', ''),
            'session_id': session_id,
            'external_user_id': external_user_id,
            'channel_id': channel_id,
            'message': message,
            'image_url': image_url,
        })

        # Busy-ack: if the agent-level concurrency gate is saturated, send an
        # immediate acknowledgment so the user knows their message was received.
        # The message is already saved to DB above and will be processed once the
        # gate frees up. The ack must NOT enter LLM context (busy_ack flag).
        _concurrency_mgr = self._llm_serializer._concurrency_mgr
        if _concurrency_mgr.is_agent_at_capacity(agent_id) and not self._is_busy(session_id):
            _agent_name = agent.get('name', agent_id)
            _cap = _concurrency_mgr.get_agent_capacity_details(agent_id)
            _logger.info(
                "[CONCURRENCY_LIMITED] agent=%s (%s) session=%s user=%s "
                "active=%d/%d — sending busy ack, message queued for later processing",
                _agent_name, agent_id, session_id, external_user_id,
                _cap["active"], _cap["max"],
            )
            _ack_text = (
                f"Agent is at maximum concurrent capacity ({_cap['active']}/{_cap['max']} slots in use). "
                "Your message has been queued and will be processed as soon as a slot becomes available. "
                "Please wait."
            )
            _ack_meta = {"busy_ack": True, "concurrency_limited": True,
                         "concurrency_active": _cap["active"], "concurrency_max": _cap["max"]}
            _db_retry(db.add_chat_message, session_id, 'assistant', _ack_text,
                      agent_id=db_agent_id, metadata=_ack_meta,
                      label="save busy ack")
            chatlog_manager.get(db_agent_id, session_id).append({
                'type': 'final',
                'session_id': session_id,
                'content': _ack_text,
                'metadata': _ack_meta,
            })
            event_stream.emit('concurrency_limited', {
                'agent_id': agent_id,
                'agent_name': _agent_name,
                'session_id': session_id,
                'external_user_id': external_user_id,
                'channel_id': channel_id,
                'response': _ack_text,
                'concurrency_active': _cap["active"],
                'concurrency_max': _cap["max"],
                'tool_trace': [],
                'thinking_duration': 0,
                'busy_ack': True,
            })
            if channel_id:
                try:
                    instance = channel_manager._active.get(channel_id)
                    if instance and instance.is_running:
                        instance.send_message(external_user_id, _ack_text)
                except Exception:
                    pass
            # Do NOT return — fall through so the message is queued for processing
            # once the agent gate becomes available.

        # Cross-session focus guard: if agent has focus mode active and is busy
        # in a DIFFERENT session, reject this message with a contextual explanation.
        # Check focus first (requires DB read) only when agent-level busy is confirmed.
        if agent.get('enable_agent_state') and self.is_agent_busy(agent_id):
            with self._agent_tracker._guard:
                busy_entry = self._agent_tracker._busy.get(agent_id)
            if busy_entry and busy_entry['session_id'] != session_id:
                ms = self._restore_agent_state(agent_id)
                if ms and ms.focus:
                    busy_msg = self._handle_busy_rejection(
                        agent_id, ms, session_id, external_user_id, channel_id, message)
                    return {"response": busy_msg, "tool_trace": [], "timeline": []}

        # Mid-loop injection: if session is currently processing, inject message
        # into the active loop instead of blocking/queuing a new task.
        # Message is already saved to DB above, so DB order is preserved.
        if self._is_busy(session_id):
            _logger.info(
                "[handle_message] agent=%s session=%s — session busy, injecting into active loop.",
                agent_id, session_id,
            )
            self._get_inject_queue(session_id).put({
                'role': 'user',
                'content': message or '[Image]',
            })
            event_stream.emit('message_injected', {
                'agent_id': agent_id,
                'agent_name': agent.get('name', ''),
                'session_id': session_id,
                'external_user_id': external_user_id,
                'channel_id': channel_id,
                'message': message,
            })
            return {"response": None, "injected": True, "tool_trace": [], "timeline": []}

        # Message buffering: debounce rapid messages, then queue
        # Skip when skip_buffer=True (e.g. API routes need synchronous response)
        buffer_seconds = agent.get('message_buffer_seconds', DEFAULT_BUFFER_SECONDS) or 0
        if buffer_seconds > 0 and not skip_buffer:
            _logger.info(
                "[handle_message] agent=%s session=%s — buffering for %ss.",
                agent_id, session_id, buffer_seconds,
            )
            task = _QueueTask(agent, SessionContext(session_id, external_user_id, channel_id,
                                                    session_db_agent_id=db_agent_id if is_subagent else None),
                              send_via_channel=True)
            timer = threading.Timer(buffer_seconds, self._enqueue_buffered, args=(task,))
            timer.daemon = True
            with self._buffer_lock:
                if session_id in self._buffer_timers:
                    self._buffer_timers[session_id].cancel()
                self._buffer_timers[session_id] = timer
            try:
                timer.start()
            except Exception:
                # If start() fails, cancel the timer and remove it from the dict
                with self._buffer_lock:
                    self._buffer_timers.pop(session_id, None)
                raise
            return {"response": None, "buffered": True, "tool_trace": [], "timeline": []}

        # Inter-agent messages: fire-and-forget (don't block the sender's worker thread).
        # The sub-agent/target processes asynchronously and results are delivered via
        # _on_final_answer auto-forward, not via the return value.
        if external_user_id and external_user_id.startswith('__agent__'):
            _logger.info(
                "[handle_message] agent=%s session=%s — inter-agent message from %s, queued async (fire-and-forget).",
                agent_id, session_id, external_user_id,
            )
            task = _QueueTask(agent, SessionContext(session_id, external_user_id, channel_id,
                                                    session_db_agent_id=db_agent_id if is_subagent else None),
                              send_via_channel=False)
            self._message_queue.put(task)
            return {"response": None, "async": True, "tool_trace": [], "timeline": []}

        # No buffering — queue immediately and wait for result
        task = _QueueTask(agent, SessionContext(session_id, external_user_id, channel_id,
                                                session_db_agent_id=db_agent_id if is_subagent else None),
                          send_via_channel=False)
        self._message_queue.put(task)
        task.event.wait()
        return task.result

    def _enqueue_buffered(self, task: '_QueueTask') -> None:
        """Queue a buffered task, cleaning up its timer even on failure."""
        try:
            with self._buffer_lock:
                self._buffer_timers.pop(task.ctx.session_id, None)
        except Exception:
            pass  # Timer may already be gone; the context manager handles cleanup
        try:
            self._message_queue.put(task)
        except Exception:
            _logger.error("Failed to enqueue buffered task for session %s: %s",
                          task.ctx.session_id, traceback.format_exc())

    def _process_and_respond(self, agent: dict, ctx: SessionContext) -> dict:
        """Build messages from DB, call LLM, trigger summarization, return response.
        Uses per-agent/per-model concurrency gate then per-session lock."""
        agent_id = agent['id']
        # Sub-agents don't exist in the agents DB — use parent's ID for model lookup
        db_agent_id = agent.get('_db_agent_id', agent_id)
        AgentRuntime._touch_session(ctx.session_id)
        try:
            model = db.get_agent_default_model(db_agent_id)
            model_id = model.get('id') if model else None
        except Exception as e:
            _logger.warning("Failed to get default model for agent %s, proceeding without model gating: %s", agent_id, e)
            model_id = None
        with self._llm_serializer._concurrency_mgr.turn_gate(agent_id, model_id):
            session_lock = self._get_session_lock(ctx.session_id)
            with session_lock:
                return self._do_process(agent, ctx)

    def _do_process(self, agent: dict, ctx: SessionContext) -> dict:
        """Internal: build messages and call LLM (must hold session lock)."""
        agent_id = agent['id']
        self._set_busy(ctx.session_id, True)
        self._set_agent_busy(agent_id, ctx.session_id)
        event_stream.emit('agent_busy_changed', {
            'agent_id': agent_id,
            'busy': True,
            'session_id': ctx.session_id,
        })
        _turn_start = time.time()
        _turn_complete_emitted = False
        try:
            result = self._do_process_inner(agent, ctx)
            _turn_complete_emitted = True
            return result
        except Exception as exc:
            # _do_process_inner threw before it could emit turn_complete — do it now
            # so the SSE stream receives 'done' and the thinking bubble closes.
            _logger.error("Unhandled exception in _do_process_inner for session %s: %s\n%s",
                            ctx.session_id, exc, traceback.format_exc())
            if not _turn_complete_emitted:
                _err_dur = round(time.time() - _turn_start, 1)
                _err_msg = 'An unexpected error occurred while processing your request.'
                # Write to chatlog so reconnecting clients (poll-based) also see the turn ended.
                try:
                    chatlog = chatlog_manager.get(ctx.session_db_agent_id or agent_id, ctx.session_id)
                    chatlog.append({'type': 'error', 'session_id': ctx.session_id,
                                    'content': _err_msg, 'metadata': {'error': True, 'thinking_duration': _err_dur}})
                    chatlog.append({'type': 'turn_end', 'session_id': ctx.session_id, 'thinking_duration': _err_dur})
                except Exception:
                    pass
                event_stream.emit('turn_complete', {
                    'agent_id': agent_id,
                    'agent_name': agent.get('name', ''),
                    'session_id': ctx.session_id,
                    'external_user_id': ctx.external_user_id,
                    'channel_id': ctx.channel_id,
                    'response': _err_msg,
                    'tool_trace': [],
                    'is_error': True,
                    'thinking_duration': _err_dur,
                })
                self._bg_executor.submit(
                    lambda sid=ctx.session_id: (time.sleep(SESSION_BUFFER_CLEANUP_DELAY), event_stream.cleanup_session_buffer(sid)),
                )
            result = {
                "response": "An unexpected error occurred. Please try again.",
                "error": True,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "error_traceback": traceback.format_exc(),
                "tool_trace": [],
                "timeline": [],
                "context": {
                    "agent_id": agent_id,
                    "session_id": ctx.session_id,
                    "external_user_id": ctx.external_user_id,
                    "channel_id": ctx.channel_id,
                }
            }
            return result
        finally:
            self._set_busy(ctx.session_id, False)
            self._clear_agent_busy(agent_id)
            event_stream.emit('agent_busy_changed', {
                'agent_id': agent_id,
                'busy': False,
                'session_id': ctx.session_id,
            })
            # Drain any messages that arrived in the injection queue just as the loop
            # was finishing (race between _is_busy check and loop exit). They are
            # already saved to DB — enqueue a new task so they are processed normally.
            inject_q = self._get_inject_queue(ctx.session_id)
            orphaned = []
            while True:
                try:
                    orphaned.append(inject_q.get_nowait())
                except queue.Empty:
                    break
            if orphaned:
                _logger.warning("%d orphaned injected message(s) for %s — re-processing as new turn",
                                 len(orphaned), ctx.session_id)
                task = _QueueTask(agent, ctx, send_via_channel=bool(ctx.channel_id))
                self._message_queue.put(task)

    def _check_evonet_offline(self, agent: dict, ctx: SessionContext):
        """Return a completed turn result dict if the agent's Cloud Workplace is offline,
        otherwise return None so normal processing continues."""
        workplace_id = agent.get('workplace_id')
        if not workplace_id:
            return None
        try:
            workplace = db.get_workplace(workplace_id)
            if not workplace or workplace.get('type') != 'cloud':
                return None
            from backend.workplaces.manager import workplace_manager
            status = workplace_manager.get_status(workplace_id)
            _logger.debug("Evonet status check for workplace=%s: %s", workplace_id, status.get('status'))
            if status.get('status') == 'connected':
                return None
        except Exception:
            _logger.warning("Failed to check Evonet status for workplace=%s, letting request through", workplace_id, exc_info=True)
            return None  # if we can't determine, let it proceed normally

        workplace_name = workplace.get('name', 'Cloud Workplace')
        reply = (
            f"⚠️ **{workplace_name}** is currently offline.\n\n"
            "Connection to Evonet device lost. "
            "Make sure Evonet is running on the target device, then try again."
        )

        db_agent_id = ctx.session_db_agent_id or agent['id']
        _db_retry(db.add_chat_message, ctx.session_id, 'assistant', reply,
                  agent_id=db_agent_id, metadata={'evonet_offline': True},
                  label="save evonet offline reply")
        chatlog_manager.get(db_agent_id, ctx.session_id).append({
            'type': 'final', 'session_id': ctx.session_id,
            'content': reply,
            'metadata': {'evonet_offline': True},
        })
        if ctx.channel_id:
            try:
                instance = channel_manager._active.get(ctx.channel_id)
                if instance and instance.is_running:
                    instance.send_message(ctx.external_user_id, reply)
            except Exception:
                pass

        event_stream.emit('turn_complete', {
            'agent_id': agent['id'],
            'agent_name': agent.get('name', ''),
            'session_id': ctx.session_id,
            'external_user_id': ctx.external_user_id,
            'channel_id': ctx.channel_id,
            'response': reply,
            'tool_trace': [],
            'is_error': True,
            'thinking_duration': 0,
        })
        return {'response': reply, 'tool_trace': [], 'error': True}

    def _do_process_inner(self, agent: dict, ctx: SessionContext) -> dict:
        """Internal: build messages and call LLM (must hold session lock).

        ctx.session_db_agent_id: when set, all DB session reads/writes go to this
        agent's per-agent DB instead of the processing agent's DB.  Used for
        cross-agent sessions where agent A processes a session that lives in agent
        B's database.
        """
        agent_id = agent['id']
        db_agent_id = ctx.session_db_agent_id or agent_id

        # Clear any stale stop flag so a previous /stop doesn't kill this new request
        self._get_stop_event(ctx.session_id).clear()

        # Send typing indicator now that processing is actually starting
        if ctx.channel_id:
            instance = channel_manager._active.get(ctx.channel_id)
            if instance and instance.is_running:
                try:
                    instance.send_typing(ctx.external_user_id)
                except Exception:
                    pass

        # Emit processing_started event (plugins and internal listeners can react here)
        event_stream.emit('processing_started', {
            'agent_id': agent_id,
            'agent_name': agent.get('name', ''),
            'session_id': ctx.session_id,
            'external_user_id': ctx.external_user_id,
            'channel_id': ctx.channel_id,
        })

        # Early rejection: if the agent has a cloud Workplace and Evonet is offline,
        # reply immediately without hitting the LLM.
        _early_reply = self._check_evonet_offline(agent, ctx)
        if _early_reply:
            return _early_reply

        # Build messages for LLM (summary-aware)
        # Try prefetched context from the previous turn's background warmup
        # (only when disable_turn_prefetch is not set).
        _prefetch = None
        if not agent.get('disable_turn_prefetch', 0):
            _prefetch = self._prefetcher.try_get(ctx.session_id)
        if _prefetch and _prefetch.agent_id == agent_id:
            system_prompt = _prefetch.system_prompt
            tools = _prefetch.tools
            # Use prefetched messages as the base, then append fresh user message below
            messages = _prefetch.messages
            # Skip the heavy build phase — tools and system_prompt are already ready
            _tools_prebuilt = tools
            _agent_ctx_prebuilt = _prefetch.agent_context
            _used_prefetch = True
            _logger.debug("Turn prefetch HIT for session %s — skipping context build", ctx.session_id)
        else:
            system_prompt = _ctx.build_system_prompt(agent)
            tools = _ctx.build_tools(agent)
            _used_prefetch = False
            messages = [{"role": "system", "content": system_prompt}]

        def _is_legacy_agent_state_msg(msg: dict) -> bool:
            """Skip legacy agent-state system messages stored in chat_messages (pre-migration)."""
            meta = msg.get('metadata') or {}
            return msg.get('role') == 'system' and meta.get('agent_state')

        def _is_ui_only_msg(msg: dict) -> bool:
            """Skip messages that appear in UI but must not enter LLM context."""
            meta = msg.get('metadata') or {}
            return bool(meta.get('busy_ack') or meta.get('busy_rejection') or meta.get('evonet_offline'))

        def _is_slash_command_msg(msg: dict) -> bool:
            """Skip slash command user messages and their assistant responses.

            Slash commands are handled directly by the command executor and must
            never enter LLM context. If they leak into context the LLM may try to
            process them (e.g., re-issuing /restart via its restart tool).
            """
            meta = msg.get('metadata') or {}
            return bool(meta.get('slash_command'))

        def _apply_vision(msg: dict) -> dict:
            """Apply vision formatting for user messages with image_url if agent supports it."""
            if msg.get('role') != 'user' or not agent.get('vision_enabled'):
                return msg
            img = msg.pop('_image_url', None)
            if not img:
                return msg
            parts = []
            if msg.get('content') and msg['content'] != '[Image]':
                parts.append({"type": "text", "text": msg['content']})
            parts.append({"type": "image_url", "image_url": {"url": img}})
            if not parts or parts[0].get('type') != 'text':
                parts.insert(0, {"type": "text", "text": "What is in this image?"})
            return {**msg, 'content': parts}

        summary_record = db.get_summary(ctx.session_id, agent_id=db_agent_id)
        chatlog = chatlog_manager.get(db_agent_id, ctx.session_id)

        if _used_prefetch:
            # Prefetch already contains the full conversation history (system prompt +
            # all prior turns). Appending the full JSONL tail again would duplicate
            # every tool_call_id, causing the API to reject with "tool must follow
            # tool_calls". Only append the current (new) user message.
            _cur_user = chatlog.get_last_entry(types=frozenset({'user'}))
            if _cur_user and not (_cur_user.get('metadata') or {}).get('slash_command'):
                _cur_content = _cur_user.get('content', '')
                # Guard against the rare race where prefetch ran after the user message
                # was saved and already includes it as the last message.
                if not messages or messages[-1].get('role') != 'user' or messages[-1].get('content') != _cur_content:
                    _cur_msg: Dict[str, Any] = {'role': 'user', 'content': _cur_content}
                    _img = (_cur_user.get('metadata') or {}).get('image_url')
                    if _img:
                        _cur_msg['_image_url'] = _img
                    messages.append(_apply_vision(_cur_msg))
        else:
            # Prefer JSONL-based context if the log has entries for this session
            _jsonl_entries = chatlog.get_entries_for_llm(
                after_ts=summary_record.get('last_message_ts') if summary_record else None,
            )
            # NOTE: The second condition handles an edge case where _jsonl_entries is empty
            # but the chatlog still has entries for this session. This happens when ALL
            # messages after the summary are themselves covered by the summary (after_ts
            # filter returns nothing). In this scenario, conv_msgs will be [] which is
            # intentional — sufficient context is already provided by the summary + the
            # current user message. This is NOT a bug; we still take the JSONL path
            # (instead of falling back to SQLite) because the session has been migrated.
            _use_jsonl = bool(_jsonl_entries) or chatlog.get_last_entry() is not None

            if summary_record:
                messages.append({
                    "role": "system",
                    "content": f"## Prior conversation summary\n{summary_record['summary']}"
                })

            if _use_jsonl:
                # Use JSONL-based context (new path)
                conv_msgs = _jsonl_entries
                # When no summary exists, skip leading non-user messages so the
                # conversation starts with a user turn.  When a summary IS present,
                # keep leading assistant messages (unsummarized continuation) but
                # still skip orphaned tool responses (no preceding tool_calls).
                tail_start = 0
                if not summary_record:
                    while tail_start < len(conv_msgs) and conv_msgs[tail_start].get('role') != 'user':
                        tail_start += 1
                else:
                    while tail_start < len(conv_msgs) and conv_msgs[tail_start].get('role') == 'tool':
                        tail_start += 1
                for msg in conv_msgs[tail_start:]:
                    # Skip slash command messages — they are handled directly by the
                    # command executor and must never enter LLM context. Both the user
                    # command echo and the assistant response carry metadata.slash_command.
                    role = msg.get('role', '')
                    if role == 'user' and (msg.get('content') or '').startswith('/'):
                        continue
                    if (msg.get('metadata') or {}).get('slash_command'):
                        continue
                    messages.append(_apply_vision(msg))
            else:
                # Fall back to SQLite (pre-migration sessions with no JSONL data)
                if summary_record:
                    raw_tail = db.get_messages_after(ctx.session_id, summary_record['last_message_id'],
                                                      agent_id=db_agent_id)
                    # Keep unsummarized continuation but skip orphaned tool
                    # responses that lack a preceding assistant tool_calls message.
                    tail_start = 0
                    while tail_start < len(raw_tail) and raw_tail[tail_start].get('role') == 'tool':
                        tail_start += 1
                    for msg in raw_tail[tail_start:]:
                        if not _is_legacy_agent_state_msg(msg) and not _is_ui_only_msg(msg) and not _is_slash_command_msg(msg):
                            messages.append(_ctx.build_message_entry(msg, agent))
                else:
                    history = db.get_session_messages(ctx.session_id, limit=50, agent_id=db_agent_id)
                    for msg in history:
                        if not _is_legacy_agent_state_msg(msg) and not _is_ui_only_msg(msg) and not _is_slash_command_msg(msg):
                            messages.append(_ctx.build_message_entry(msg, agent))

        # Ensure messages don't end with assistant role (causes prefill error with some APIs)
        while len(messages) > 1 and messages[-1].get('role') == 'assistant':
            messages.pop()

        # Inject long-term memories (position 1, right after system prompt)
        memory_section = get_memories_for_context(db_agent_id, messages)
        if memory_section:
            messages.insert(1, {"role": "system", "content": memory_section})

        # Inject inter-agent session context so the agent is aware of the situation
        if ctx.external_user_id.startswith("__agent__"):
            _other_id = ctx.external_user_id[len("__agent__"):]
            _other_agent = db.get_agent(_other_id)
            _other_name = _other_agent.get('name', _other_id) if _other_agent else _other_id
            if ctx.session_db_agent_id and ctx.session_db_agent_id != agent_id:
                # This agent (A) is processing a session owned by another agent (B)
                _db_owner = db.get_agent(ctx.session_db_agent_id)
                _db_owner_name = _db_owner.get('name', ctx.session_db_agent_id) if _db_owner else ctx.session_db_agent_id
                _context_note = (
                    "## Inter-Agent Session (Cross-Agent Processing)\n"
                    f"You are processing a shared session owned by **{_db_owner_name}** "
                    f"(id: `{ctx.session_db_agent_id}`). The full conversation history is "
                    "visible above — use it as context for your response.\n"
                    "This is NOT a session with a human user. "
                    "If you need human input, use the `escalate_to_user` tool."
                )
            else:
                _context_note = (
                    "## Inter-Agent Session\n"
                    f"You are currently in a private session with another agent: **{_other_name}** "
                    f"(id: `{_other_id}`).\n"
                    "This is NOT a session with a human user. "
                    "If you receive an approval request or need human input, "
                    "use the `escalate_to_user` tool to forward the request to your human user session."
                )
            messages.insert(1, {"role": "system", "content": _context_note})

        # Inject channel-specific system instructions (e.g., no-markdown for Telegram)
        if ctx.channel_id:
            chan_inst = channel_manager.get_channel_instance(ctx.channel_id)
            if chan_inst:
                chan_instr = chan_inst.get_system_instructions()
                if chan_instr:
                    messages.insert(1, {"role": "system", "content": chan_instr})

        # Build tool definitions (use prefetched if available)
        if _used_prefetch:
            tools = _tools_prebuilt
            assigned_tool_ids = _agent_ctx_prebuilt.get('assigned_tool_ids', [])
            agent_context = dict(_agent_ctx_prebuilt)  # shallow copy to allow mutations
        else:
            tools = _ctx.build_tools(agent)

            # Build agent context for tool backends
            assigned_tool_ids = db.get_agent_tools(db_agent_id)

            # Resolve workspace: workplace config takes priority over agent.workspace.
            # For cloud workplaces, never fall back to the agent's /workspace path —
            # Evonet runs on the remote device and has its own working directory.
            _workplace_id = agent.get('workplace_id') or None
            if _workplace_id:
                try:
                    import json as _json
                    _workplace = db.get_workplace(_workplace_id)
                    _workplace_cfg = _json.loads(_workplace.get('config', '{}')) if _workplace else {}
                    if _workplace and _workplace.get('type') == 'cloud':
                        _workspace = _workplace_cfg.get('workspace_path') or None
                    else:
                        _workspace = _workplace_cfg.get('workspace_path') or agent.get('workspace') or None
                except Exception:
                    _workspace = agent.get('workspace') or None
            else:
                _workspace = agent.get('workspace') or None

            agent_context = {
                'id': agent_id,
                'name': agent.get('name', ''),
                'agent_name': agent.get('name', ''),
                'agent_model': agent.get('model'),
                'user_id': ctx.external_user_id,
                'channel_id': ctx.channel_id,
                'session_id': ctx.session_id,
                'assigned_tool_ids': assigned_tool_ids,
                'workspace': _workspace,
                'workplace_id': _workplace_id,
                'is_super': bool(agent.get('is_super')),
                'is_subagent': bool(agent.get('is_subagent')),
                'parent_id': agent.get('parent_id'),
                'agent_messaging_enabled': bool(agent.get('agent_messaging_enabled')),
                'sandbox_enabled': agent.get('sandbox_enabled', 1),
                'safety_checker_enabled': agent.get('safety_checker_enabled', 1),
                'disable_parallel_tool_execution': agent.get('disable_parallel_tool_execution', 0),
                'disable_turn_prefetch': agent.get('disable_turn_prefetch', 0),
                'variables': db.get_agent_variables_dict(db_agent_id),
            }
        # Propagate agent_message_depth and from_agent_id from incoming message metadata
        if ctx.external_user_id.startswith("__agent__"):
            _last_user = chatlog.get_last_entry(types=frozenset({'user'}))
            if _last_user:
                _meta = _last_user.get('metadata') or {}
                if isinstance(_meta, dict):
                    if _meta.get('agent_message_depth') is not None:
                        agent_context['agent_message_depth'] = _meta['agent_message_depth']
                    if _meta.get('from_agent_id'):
                        agent_context['from_agent_id'] = _meta['from_agent_id']
            else:
                # Fall back to SQLite for pre-migration sessions
                _recent = db.get_session_messages(ctx.session_id, limit=5, agent_id=db_agent_id)
                for _m in reversed(_recent):
                    if _m.get('role') == 'user':
                        _meta = _m.get('metadata') or {}
                        if isinstance(_meta, dict):
                            if _meta.get('agent_message_depth') is not None:
                                agent_context['agent_message_depth'] = _meta['agent_message_depth']
                            if _meta.get('from_agent_id'):
                                agent_context['from_agent_id'] = _meta['from_agent_id']
                        break

        # Agent state: restore or create, then check for user approval
        if agent.get('enable_agent_state'):
            ms = self._restore_agent_state(db_agent_id, session_id=ctx.session_id)
            is_new_session = ms is None
            if is_new_session:
                # Classify task complexity to decide initial mode
                from backend.task_classifier import classify_task
                _user_text = ""
                for _msg in reversed(messages):
                    if _msg.get('role') == 'user':
                        _c = _msg.get('content', '')
                        if isinstance(_c, list):
                            _user_text = next(
                                (p.get('text', '') for p in _c
                                 if isinstance(p, dict) and p.get('type') == 'text'), '')
                        else:
                            _user_text = _c
                        break
                if classify_task(_user_text) == "trivial":
                    ms = AgentState(mode="execute", auto_trivial=True)
                else:
                    ms = AgentState()
            # Hybrid approval: only check if state was restored (agent already presented a plan).
            # Skip for new sessions so the first user message never auto-approves a non-existent plan.
            if not is_new_session and ms.mode == 'plan':
                last_user = next(
                    (m['content'] for m in reversed(messages)
                     if m.get('role') == 'user' and m.get('content')),
                    None
                )
                if last_user:
                    last_user_text = (
                        next((p['text'] for p in last_user if isinstance(p, dict) and p.get('type') == 'text'), '')
                        if isinstance(last_user, list) else last_user
                    )
                    if self._is_approval(last_user_text):
                        ms.set_mode('execute', reason='user approved')
                    elif last_user_text.startswith('[System/Task]'):
                        # System-triggered task (e.g. from a plugin): reset to fresh plan mode
                        # so the agent can start a new plan cycle for this task
                        # instead of being stuck in a stale plan from a previous task.
                        ms = AgentState()
            agent_context['agent_state'] = ms

        # Call LLM with tool loop
        _inner_turn_start = time.time()

        # Keep sub-agent alive during the LLM loop — the cleanup timer
        # runs every 60s and would expire idle sub-agents after 10 min,
        # but a long-running tool loop never calls subagent_manager.get()
        # so last_active_at is never refreshed.
        _sa_heartbeat = None
        if agent.get('is_subagent'):
            from backend.subagent_manager import subagent_manager as _sam
            _sam._touch(agent_id)  # immediate touch before loop starts

            _sa_stop = threading.Event()
            def _heartbeat():
                while not _sa_stop.wait(30):
                    _sam._touch(agent_id)
            _sa_heartbeat = threading.Thread(target=_heartbeat, daemon=True)
            _sa_heartbeat.start()

        try:
            response_raw, tool_trace, timeline = _loop.run_tool_loop(
                agent=agent,
                agent_context=agent_context,
                messages=messages,
                tools=tools,
                session_id=ctx.session_id,
                llm_lock=self._llm_serializer._llm_lock,
                stop_event=self._get_stop_event(ctx.session_id),
                session_skill_mds=self._session_skill_mds,
                session_skill_tools=self._session_skill_tools,
                llm_log_path=_llm_log_path(db_agent_id),
                inject_queue=self._get_inject_queue(ctx.session_id),
                session_db_agent_id=db_agent_id,
            )
        finally:
            if _sa_heartbeat:
                _sa_stop.set()
                _sa_heartbeat.join(timeout=2)

        # Handle error vs normal response from run_tool_loop
        if isinstance(response_raw, dict):
            response_text = response_raw.get('text', '')
            is_error = response_raw.get('error', False)
        else:
            response_text = response_raw
            is_error = False

        # Trigger background summarization via shared executor (skip for cross-agent sessions — the session
        # owner's own turns will handle summarization when needed)
        threshold = agent.get('summarize_threshold', 3)
        if threshold and threshold > 0 and db_agent_id == agent_id:
            self._bg_executor.submit(
                _sum.maybe_summarize,
                agent, ctx.session_id,
                self._llm_serializer._summarize_guard, self._llm_serializer._summarize_active, self._llm_serializer._llm_lock,
            )

        # Schedule background warmup for the next turn: pre-read the chatlog
        # so the OS filesystem cache is hot when Turn N+1 loads messages.
        # Skip when disable_turn_prefetch is set on the agent.
        if not agent.get('disable_turn_prefetch', 0):
            self._prefetcher.submit(agent, ctx, messages, tools,
                                    system_prompt, agent_context)

        result = {"response": response_text, "tool_trace": tool_trace, "timeline": timeline}
        if is_error:
            result["error"] = True

        # Emit turn_complete event
        event_stream.emit('turn_complete', {
            'agent_id': agent_id,
            'agent_name': agent.get('name', ''),
            'session_id': ctx.session_id,
            'external_user_id': ctx.external_user_id,
            'channel_id': ctx.channel_id,
            'response': response_text,
            'tool_trace': tool_trace,
            'is_error': is_error,
            'thinking_duration': round(time.time() - _inner_turn_start, 1),
        })
        # Clean up per-session buffer after a delay to allow gap-fill requests to complete.
        # Use executor to avoid timer leak (old timer never cancelled).
        self._bg_executor.submit(
            lambda sid=ctx.session_id: (time.sleep(SESSION_BUFFER_CLEANUP_DELAY), event_stream.cleanup_session_buffer(sid)),
        )

        return result

    def _run_tool_loop(
        self,
        agent: Dict[str, Any],
        agent_context: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        session_id: str,
    ) -> Any:
        """Thin wrapper for direct calls and test compatibility."""
        return _loop.run_tool_loop(
            agent=agent,
            agent_context=agent_context,
            messages=messages,
            tools=tools,
            session_id=session_id,
            llm_lock=self._llm_serializer._llm_lock,
            stop_event=self._get_stop_event(session_id),
            session_skill_mds=self._session_skill_mds,
            session_skill_tools=self._session_skill_tools,
            llm_log_path=_llm_log_path(agent['id']),
            inject_queue=self._get_inject_queue(session_id),
        )

    def process_in_session(self, processing_agent_id: str, session_id: str,
                           session_db_agent_id: str, external_user_id: str,
                           channel_id: Optional[str] = None) -> None:
        """Enqueue an agent to process a session it does not own.

        Used for cross-agent approval escalation: agent A processes a turn inside
        agent B's session so A sees the full conversation context.

        Args:
            processing_agent_id: The agent that will run the LLM loop.
            session_id: The session to process (lives in session_db_agent_id's DB).
            session_db_agent_id: Owner of the session DB (all DB ops use this agent's DB).
            external_user_id: The external_user_id recorded on the session.
            channel_id: Optional channel for the session.
        """
        agent = db.get_agent(processing_agent_id)
        if not agent:
            return
        if not agent.get('is_super') and not agent.get('enabled', True):
            return
        task = _QueueTask(
            agent=agent,
            ctx=SessionContext(session_id, external_user_id, channel_id, session_db_agent_id),
        )
        self._message_queue.put(task)

    def get_compiled_context(self, agent_id: str) -> dict:
        """Return the compiled system prompt and tool definitions for an agent."""
        return _ctx.get_compiled_context(agent_id)

    def _build_message_entry(self, msg: dict, agent: dict) -> dict:
        """Convert a DB message row into an LLM message dict."""
        entry = {"role": msg["role"]}
        msg_image = None
        if msg.get("metadata") and isinstance(msg["metadata"], dict):
            msg_image = msg["metadata"].get("image_url")
        if msg_image and agent.get("vision_enabled"):
            parts = []
            if msg.get("content") and msg["content"] != "[Image]":
                parts.append({"type": "text", "text": msg["content"]})
            parts.append({"type": "image_url", "image_url": {"url": msg_image}})
            if not parts[0].get("text") if parts else True:
                parts.insert(0, {"type": "text", "text": "What is in this image?"})
            entry["content"] = parts
        elif msg.get("content"):
            content = msg["content"]
            if msg.get("role") == "tool" and len(content) > MAX_TOOL_RESULT_CHARS:
                remaining = len(content) - MAX_TOOL_RESULT_CHARS
                content = (content[:MAX_TOOL_RESULT_CHARS] +
                           f"\n...[truncated — {remaining} chars omitted; full result saved]")
            entry["content"] = content
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg["tool_call_id"]
        return entry

    def _restore_agent_state(self, agent_id: str, session_id: str = None) -> Optional['AgentState']:
        """Restore agent state, merging per-session and global fields.

        When session_id is provided:
          - Per-session fields (mode/tasks/plan_file/states/auto_trivial) come from session_state.
          - Global fields (focus/focus_reason) come from agent_state (__agent__).
          - They are merged into a single AgentState object.

        When session_id is None (busy rejection path):
          - Only global fields (focus/focus_reason) are loaded from agent_state.
        """
        import json as _json

        agent_content = db.get_agent_state(agent_id=agent_id)
        agent_data = _json.loads(agent_content) if agent_content else {}

        if session_id:
            session_content = db.get_session_state(session_id, agent_id=agent_id)
            session_data = _json.loads(session_content) if session_content else {}
            # Merge: session fields override global defaults.
            # focus/focus_reason in agent_data act as fallback; session_data
            # does not contain focus fields so global values are preserved.
            merged = {**agent_data, **session_data}
        else:
            # Only need focus/focus_reason for busy rejection
            merged = agent_data

        if merged:
            return AgentState.deserialize(_json.dumps(merged))
        return None

    _APPROVAL_RE = re.compile(
        r'\b(lanjut|ok|oke|approved|approve|setuju|go ahead|proceed|execute|'
        r'yes|ya|yep|sure|confirm|done|boleh|silakan|silahkan|jalankan|mulai|start)\b',
        re.IGNORECASE,
    )

    @classmethod
    def _is_approval(cls, text: str) -> bool:
        """Return True if the text looks like a user approval of a plan."""
        return bool(cls._APPROVAL_RE.search(text.strip()))

    # ── Cross-session focus helpers ──────────────────────────────────────────

    _NOTIFY_YES = None  # Lazy-compiled regex

    @classmethod
    def _is_notify_opt_in(cls, text: str) -> bool:
        if cls._NOTIFY_YES is None:
            cls._NOTIFY_YES = re.compile(
                r'^(ya|yes|ok|oke|perlu|need|boleh|mau|sure|yep|silakan|iya)', re.IGNORECASE)
        return bool(cls._NOTIFY_YES.match((text or '').strip()))

    def _handle_busy_rejection(self, agent_id: str, agent_state: Any,
                                session_id: str, external_user_id: str,
                                channel_id: Optional[str], message: str) -> str:
        """Generate and send a busy rejection response, handle notify opt-in."""
        # Check if user is responding 'yes' to a previous rejection offer
        if self._check_notify_opt_in(agent_id, session_id, external_user_id,
                                      channel_id, message):
            reply = "Okay, I'll let you know once I'm done!"
        else:
            reply = get_busy_message(agent_id, agent_state)
            if not reply:
                reason = agent_state.focus_reason or "something else"
                reply = (f"Sorry, I'm busy with {reason}. "
                         f"Want me to let you know when I'm done?")

        _db_retry(db.add_chat_message, session_id, 'assistant', reply,
                  agent_id=agent_id, metadata={"busy_rejection": True},
                  label="save busy rejection")
        chatlog_manager.get(agent_id, session_id).append({'type': 'final', 'session_id': session_id,
                                                          'content': reply,
                                                          'metadata': {'busy_rejection': True}})
        # Send via channel if applicable
        if channel_id:
            try:
                instance = channel_manager._active.get(channel_id)
                if instance and instance.is_running:
                    instance.send_message(external_user_id, reply)
            except Exception:
                pass
        return reply

    def _check_notify_opt_in(self, agent_id: str, session_id: str,
                              external_user_id: str, channel_id: Optional[str], message: str) -> bool:
        """Return True and create free-notification scheduler if user opted in."""
        if not self._is_notify_opt_in(message):
            return False
        # Only trigger if last assistant message was a busy rejection
        last_msg = db.get_last_assistant_message(session_id, agent_id=agent_id)
        meta = last_msg.get('metadata') or {} if last_msg else {}
        if not last_msg or not (meta.get('busy_rejection') or meta.get('busy_ack')):
            return False
        self._queue_free_notification(agent_id, session_id, external_user_id, channel_id)
        return True

    # Pending free-notifications: agent_id → {session_id, external_user_id, channel_id}
    # Consumed by _on_agent_busy_changed when agent becomes free.
    _free_notify_pending: dict = {}
    _free_notify_lock = threading.Lock()

    @classmethod
    def _queue_free_notification(cls, agent_id: str, session_id: str,
                                  external_user_id: str, channel_id: Optional[str]) -> None:
        """Queue a one-shot notification for when the agent becomes free."""
        with cls._free_notify_lock:
            cls._free_notify_pending[agent_id] = {
                'session_id': session_id,
                'external_user_id': external_user_id,
                'channel_id': channel_id,
            }

    def clear_session(self, agent_id: str, external_user_id: str, channel_id: Optional[str] = None) -> None:
        """Clear chat history for a user's session."""
        session_id = db.get_or_create_session(agent_id, external_user_id, channel_id)
        db.clear_session(session_id, agent_id=agent_id)
        self._session_skill_mds.pop(session_id, None)
        self._session_skill_tools.pop(session_id, None)

    def send_as_bot(self, session_id: str, text: str) -> bool:
        """Admin takeover: save message as assistant and send via channel."""
        session = db.get_session_with_details(session_id)
        if not session:
            return False
        db.add_chat_message(session_id, 'assistant', text, agent_id=session['agent_id'])
        chatlog_manager.get(session['agent_id'], session_id).append(
            {'type': 'final', 'session_id': session_id, 'content': text,
             'metadata': {'admin_takeover': True}})
        # Send via channel if available
        if session.get('channel_id'):
            instance = channel_manager._active.get(session['channel_id'])
            if instance and instance.is_running:
                try:
                    instance.send_message(session['external_user_id'], text)
                except Exception as e:
                    _logger.error("send_as_bot channel error: %s", e)
        return True

    def send_as_user(self, session_id: str, text: str,
                     image_url: str | None = None,
                     metadata: dict | None = None) -> bool:
        """User perspective: save message as user and trigger agent processing."""
        session = db.get_session_with_details(session_id)
        if not session:
            return False
        agent_id = session['agent_id']
        external_user_id = session['external_user_id']
        channel_id = session.get('channel_id')

        # Slash command interception — execute immediately instead of sending to LLM
        parsed = parse_command(text)
        if parsed:
            cmd_name, cmd_args = parsed
            response = execute_command(
                cmd_name, cmd_args, session_id, agent_id,
                external_user_id, channel_id,
            )
            if response is not None:
                # Command was recognized — save command echo and response, then return
                db.add_chat_message(session_id, 'user', text,
                                    agent_id=agent_id, metadata={'slash_command': True})
                db.add_chat_message(session_id, 'assistant', response,
                                    agent_id=agent_id, metadata={'slash_command': True})
                _cl = chatlog_manager.get(agent_id, session_id)
                _cl.append({'type': 'user', 'session_id': session_id, 'content': text,
                            'sender_id': external_user_id,
                            'metadata': {'slash_command': True}})
                _cl.append({'type': 'system', 'session_id': session_id, 'content': response,
                            'metadata': {'slash_command': True}})
                agent = db.get_agent(agent_id)
                # Emit turn_complete so SSE client shows the response
                event_stream.emit('turn_complete', {
                    'agent_id': agent_id,
                    'agent_name': agent.get('name', '') if agent else '',
                    'session_id': session_id,
                    'external_user_id': external_user_id,
                    'channel_id': channel_id,
                    'response': response,
                    'tool_trace': [],
                    'is_error': False,
                    'thinking_duration': 0.0,
                    'slash_command': True,
                })
                # Signal the client to clear the chat UI when the clear command was used
                if cmd_name == 'clear':
                    event_stream.emit('session_clear', {
                        'session_id': session_id,
                        'agent_id': agent_id,
                    })
                self._prefetcher.invalidate(session_id)
                return True
            # Unknown command — fall through to normal LLM processing

        meta = {'user_perspective': True}
        if image_url:
            meta['image_url'] = image_url
        if metadata:
            meta.update(metadata)
        db.add_chat_message(session_id, 'user', text, agent_id=agent_id, metadata=meta)
        chatlog_manager.get(agent_id, session_id).append(
            {'type': 'user', 'session_id': session_id, 'content': text,
             'metadata': meta})

        # Invalidate prefetched context — a new message arrived
        self._prefetcher.invalidate(session_id)

        # Emit message_received event so SSE clients know about it
        agent = db.get_agent(agent_id)
        event_stream.emit('message_received', {
            'agent_id': agent_id,
            'agent_name': agent.get('name', '') if agent else '',
            'session_id': session_id,
            'external_user_id': external_user_id,
            'channel_id': channel_id,
            'message': text,
            'image_url': image_url,
        })

        # Enqueue for agent processing (fire-and-forget)
        if agent and agent.get('enabled', True):
            task = _QueueTask(agent, SessionContext(session_id, external_user_id, channel_id),
                              send_via_channel=False)
            self._message_queue.put(task)

        return True
